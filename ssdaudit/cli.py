"""Command line interface.

Exit codes are meaningful so the tool can be scripted:

* ``0`` -- the drives hold the same files
* ``1`` -- differences were found
* ``2`` -- the audit could not run (drive missing, bad path, unreadable root)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .compare import VERIFY_LEVELS, compare
from .config import (
    DriveRef,
    IgnoreRules,
    Profile,
    load_profiles,
    profiles_path,
    save_profiles,
)
from .dupes import DEFAULT_MIN_SIZE, find_duplicates
from .history import (
    append_run,
    audits_dir,
    diff_runs,
    format_diff,
    load_history,
    load_run,
    resolve_run_id,
    run_directory,
)
from .index import HashIndex, default_cache_path
from .picker import choose_folders
from .progress import HashReporter, Progress, ScanReporter
from .remediate import write_all
from .report import format_bytes, new_run_id, write_audit
from .resolver import DEFAULT_WORKERS
from .scanner import scan
from .volumes import find_by_serial, list_volumes, volume_for_path
from .wizard import run_wizard, volume_table

EXIT_IN_SYNC = 0
EXIT_DIFFERENCES = 1
EXIT_ERROR = 2


class AuditError(Exception):
    """A condition that should stop the run with a clear message."""


# ----------------------------------------------------------------- resolution


def resolve_side(ref: DriveRef, label: str) -> str:
    """Turn a stored drive reference into a path that exists right now.

    Serial first, always. A profile saying ``G:\\Photos`` must not audit whatever
    disk happens to hold ``G:`` today -- that is the entire reason drives are
    recorded by serial.
    """
    if ref.serial:
        volume = find_by_serial(ref.serial)
        if volume is None:
            known = ", ".join(v.describe() for v in list_volumes()) or "none"
            raise AuditError(
                f"{label} drive is not attached.\n"
                f"  Looking for serial {ref.serial}"
                + (f" ({ref.label})" if ref.label else "")
                + f"\n  Currently attached: {known}"
            )
        return os.path.join(volume.mount, ref.subpath.replace("/", os.sep)) if ref.subpath else volume.mount

    if ref.fallback_path:
        return ref.fallback_path

    raise AuditError(f"{label} drive has neither a serial nor a path recorded")


def make_ref(path: str) -> DriveRef:
    """Capture a path as a drive reference, pinned to its volume serial."""
    absolute = os.path.abspath(path)
    volume = volume_for_path(absolute)
    drive, remainder = os.path.splitdrive(absolute)
    if volume is None:
        return DriveRef(fallback_path=absolute)
    return DriveRef(
        serial=volume.serial,
        label=volume.label,
        subpath=remainder.strip(os.sep).replace(os.sep, "/"),
        fallback_path=absolute,
    )


def sides_from_args(args) -> tuple[str, str, Profile | None]:
    """Work out the two roots to compare, from a profile or explicit paths."""
    if getattr(args, "profile", None):
        profiles = load_profiles()
        profile = profiles.get(args.profile)
        if profile is None:
            available = ", ".join(sorted(profiles)) or "none saved"
            raise AuditError(f"no profile named {args.profile!r}. Available: {available}")
        return resolve_side(profile.left, "Left"), resolve_side(profile.right, "Right"), profile

    if not (args.left and args.right):
        raise AuditError("provide either --profile NAME, or both --left PATH and --right PATH")

    for path, label in ((args.left, "--left"), (args.right, "--right")):
        if not os.path.isdir(path):
            raise AuditError(f"{label} is not a directory: {path}")

    return os.path.abspath(args.left), os.path.abspath(args.right), None


def rules_from(args, profile: Profile | None) -> IgnoreRules:
    if profile is not None:
        return profile.rules()
    return IgnoreRules.build(
        presets=args.preset or [],
        exclude=args.exclude or [],
        include=args.include or [],
        keep_cruft=getattr(args, "keep_cruft", False),
    )


# ------------------------------------------------------------------- commands


def cmd_volumes(args) -> int:
    volumes = list_volumes()
    if not volumes:
        print("No volumes could be identified on this platform.")
        return EXIT_ERROR

    print("\n  Attached drives")
    volume_table(volumes)
    print(
        "\n  Drives are matched by serial number, not by letter, so a saved profile\n"
        "  keeps working after Windows remounts a drive somewhere else.\n"
        "\n  Compare two of these with:  ssdaudit run\n"
    )
    return EXIT_IN_SYNC


def cmd_gui(args) -> int:
    """Open the desktop window."""
    try:
        from .gui import main as gui_main
    except ImportError as error:
        raise AuditError(
            f"the desktop interface needs tkinter, which this Python lacks ({error}).\n"
            "  Use the terminal wizard instead:  ssdaudit run"
        )
    return gui_main()


def cmd_run(args) -> int:
    """Interactive setup, then the comparison it describes."""
    rules = rules_from(args, None)
    try:
        plan = run_wizard(rules)
    except KeyboardInterrupt:
        print("\n  Cancelled. Nothing was read.\n")
        return EXIT_ERROR
    except RuntimeError as error:
        raise AuditError(str(error))

    if plan is None:
        return EXIT_ERROR

    return execute_comparison(
        left_root=plan["left"],
        right_root=plan["right"],
        rules=rules,
        folders=plan["folders"],
        verify=plan["verify"],
        want_dupes=plan["dupes"],
        args=args,
        profile=None,
        name="interactive",
    )


def cmd_compare(args) -> int:
    left_root, right_root, profile = sides_from_args(args)
    return execute_comparison(
        left_root=left_root,
        right_root=right_root,
        rules=rules_from(args, profile),
        folders=args.folder or (profile.folders if profile else []),
        verify=args.verify or (profile.verify if profile else "smart"),
        want_dupes=not args.no_dupes,
        args=args,
        profile=profile,
        name=args.profile or "ad-hoc",
    )


def execute_comparison(
    left_root: str,
    right_root: str,
    rules,
    folders: list[str],
    verify: str,
    want_dupes: bool,
    args,
    profile: Profile | None,
    name: str,
) -> int:
    """Run one audit, reporting progress throughout.

    Every phase reports as it goes: a whole-drive scan can run for minutes, and
    silence is indistinguishable from a hang.
    """
    started = time.time()
    started_iso = datetime.now().isoformat(timespec="seconds")
    quiet = getattr(args, "quiet", False)

    print(f"\n  {'LEFT':<6} {left_root}")
    print(f"  {'RIGHT':<6} {right_root}")
    print(f"  {'Scope':<6} {', '.join(folders) if folders else 'entire drive'}")
    print(f"  {'Verify':<6} {verify}")
    print("\n  Reading both drives. Nothing will be modified.\n")

    progress = Progress(enabled=False if quiet else None)

    left = scan(left_root, rules, folders, ScanReporter(progress, "Scanning left"))
    progress.finish(f"  Scanned left:  {len(left.files):>9,} files "
                    f"in {left.dirs_scanned:,} folders")

    right = scan(right_root, rules, folders, ScanReporter(progress, "Scanning right"))
    progress.finish(f"  Scanned right: {len(right.files):>9,} files "
                    f"in {right.dirs_scanned:,} folders")

    cache_path = Path(args.cache) if args.cache else default_cache_path()
    with HashIndex(cache_path) as index:
        result = compare(left, right, index, verify=verify, workers=args.workers,
                         on_progress=HashReporter(progress, "Verifying contents"))
        progress.finish("  Compared:      done")

        duplicates = None
        if want_dupes:
            duplicates = find_duplicates(
                left, right, index,
                scope=args.dupe_scope, min_size=args.min_size, workers=args.workers,
                on_progress=HashReporter(progress, "Hashing for duplicates"),
            )
            progress.finish("  Duplicates:    done")

        cache_stats = index.stats()

    duration = time.time() - started
    run_id = new_run_id(name)
    directory = run_directory(run_id, Path(args.out) if args.out else None)

    meta = {
        "run_id": run_id,
        "profile": name,
        "started": started_iso,
        "duration_s": duration,
        "verify": verify,
        "folders": folders,
        "left_label": (profile.left.label if profile else ""),
        "right_label": (profile.right.label if profile else ""),
        "version": __version__,
    }

    write_audit(result, duplicates, directory, meta)
    write_all(result, duplicates, directory)

    counts = {**result.counts(), **(duplicates.counts() if duplicates else {})}
    append_run({
        **meta,
        "left_volume": left.volume,
        "right_volume": right.volume,
        "left_root": left.root,
        "right_root": right.root,
        "counts": counts,
        "cache": cache_stats,
    })

    _print_verdict(result, duplicates, directory, duration, cache_stats)
    return EXIT_IN_SYNC if result.in_sync else EXIT_DIFFERENCES


def _print_verdict(result, duplicates, directory: Path, duration: float, cache: dict) -> None:
    counts = result.counts()
    print("\n  " + "=" * 60)
    if result.in_sync:
        print("  Both drives hold the same files.")
    else:
        print(f"  {counts['only_left']:,} files missing from the RIGHT drive "
              f"({format_bytes(result.bytes_only_left)})")
        print(f"  {counts['only_right']:,} files missing from the LEFT drive "
              f"({format_bytes(result.bytes_only_right)})")
        print(f"  {counts['conflicts']:,} content conflicts (same path, different content)")

    if counts["dst_artifacts"]:
        print(f"  {counts['dst_artifacts']:,} timestamp-only differences (DST artefact, content identical)")
    if counts["unverified"]:
        print(f"  {counts['unverified']:,} unverified - rerun with --verify smart to settle")
    if counts["cruft_files"]:
        print(f"  {counts['cruft_files']:,} cruft files ({format_bytes(counts['bytes_cruft'])} reclaimable)")

    if duplicates:
        wasted = (sum(g.wasted_bytes for g in duplicates.within_left)
                  + sum(g.wasted_bytes for g in duplicates.within_right))
        if wasted:
            print(f"  {len(duplicates.within_left) + len(duplicates.within_right):,} duplicate groups "
                  f"({format_bytes(wasted)} reclaimable)")
        if duplicates.cross:
            print(f"  {len(duplicates.cross):,} files present on both drives under different paths")

    errors = counts["scan_errors"] + len(result.hash_errors)
    if errors:
        print(f"  {errors:,} files or folders could not be read - see diff.json")

    print("  " + "=" * 60)
    print(f"  Finished in {duration:.1f}s "
          f"(cache: {cache['hits']:,} hits, {cache['misses']:,} misses)")
    print(f"\n  Report:  {directory / 'summary.md'}")
    print(f"  Browse:  {directory / 'report.html'}")
    if not result.in_sync:
        print(f"  Scripts: {directory}")
        print("\n  Review the generated .cmd files before running them. "
              "This tool has not modified either drive.")
    print()


def cmd_dupes(args) -> int:
    left_root, right_root, profile = sides_from_args(args)
    rules = rules_from(args, profile)
    folders = args.folder or (profile.folders if profile else [])

    print(f"\n  Scanning {left_root}...", end="", flush=True)
    left = scan(left_root, rules, folders)
    print(f" {len(left.files):,} files")
    print(f"  Scanning {right_root}...", end="", flush=True)
    right = scan(right_root, rules, folders)
    print(f" {len(right.files):,} files")

    cache_path = Path(args.cache) if args.cache else default_cache_path()
    with HashIndex(cache_path) as index:
        print("  Hashing candidates...", end="", flush=True)
        duplicates = find_duplicates(
            left, right, index, scope=args.dupe_scope,
            min_size=args.min_size, workers=args.workers,
        )
        print(" done\n")

    for title, groups in (
        ("Duplicates on the LEFT drive", duplicates.within_left),
        ("Duplicates on the RIGHT drive", duplicates.within_right),
        ("Same content, different path across drives", duplicates.cross),
    ):
        if not groups:
            continue
        total = sum(group.wasted_bytes for group in groups)
        print(f"  {title} - {len(groups):,} groups, {format_bytes(total)} reclaimable")
        for group in groups[:20]:
            print(f"    {format_bytes(group.size)} x{len(group.entries)}")
            for path in group.paths():
                print(f"      {path}")
        if len(groups) > 20:
            print(f"    ... and {len(groups) - 20:,} more groups")
        print()

    if not (duplicates.within_left or duplicates.within_right or duplicates.cross):
        print("  No duplicates found above the minimum size "
              f"({format_bytes(args.min_size)}).\n")
        return EXIT_IN_SYNC

    print("  Nothing has been deleted. Run a full `compare` to generate a "
          "quarantine script for these.\n")
    return EXIT_DIFFERENCES


def cmd_pick(args) -> int:
    left_root, right_root, profile = sides_from_args(args)
    rules = rules_from(args, profile)

    print("\n  Choosing folders to compare. These paths must exist on BOTH drives.")
    print("  Press Enter with nothing selected to compare the entire drive.")
    folders = choose_folders(left_root, rules, preselected=profile.folders if profile else None)

    if not folders:
        print("\n  No folders selected - the whole drive would be compared.")
    else:
        print(f"\n  Selected {len(folders)} folder(s):")
        for folder in folders:
            marker = "ok " if os.path.isdir(os.path.join(right_root, folder.replace("/", os.sep))) else "MISSING on right"
            print(f"    {folder}   [{marker}]")

    answer = input("\n  Save as a profile? Enter a name (or blank to skip): ").strip()
    if not answer:
        print("\n  Not saved. Run with: "
              f"--left \"{left_root}\" --right \"{right_root}\" "
              + " ".join(f'--folder "{f}"' for f in folders) + "\n")
        return EXIT_IN_SYNC

    profiles = load_profiles()
    profiles[answer] = Profile(
        name=answer,
        left=make_ref(left_root),
        right=make_ref(right_root),
        folders=folders,
        presets=args.preset or [],
        exclude=args.exclude or [],
        include=args.include or [],
        verify=args.verify or "smart",
    )
    path = save_profiles(profiles)
    print(f"\n  Saved profile {answer!r} to {path}")
    print(f"  Run it with:  ssdaudit compare --profile {answer}\n")
    return EXIT_IN_SYNC


def cmd_profiles(args) -> int:
    profiles = load_profiles()

    if args.profile_action in (None, "list"):
        if not profiles:
            print(f"\n  No profiles saved yet ({profiles_path()}).")
            print("  Create one with:  ssdaudit pick --left <PATH> --right <PATH>\n")
            return EXIT_IN_SYNC
        print(f"\n  Profiles ({profiles_path()}):\n")
        for name, profile in sorted(profiles.items()):
            scope = f"{len(profile.folders)} folder(s)" if profile.folders else "entire drive"
            print(f"  {name}")
            print(f"    left   {profile.left.label or '?'}  serial {profile.left.serial or '(path only)'}")
            print(f"    right  {profile.right.label or '?'}  serial {profile.right.serial or '(path only)'}")
            print(f"    scope  {scope}, verify {profile.verify}")
        print()
        return EXIT_IN_SYNC

    if args.profile_action == "show":
        profile = profiles.get(args.name)
        if profile is None:
            raise AuditError(f"no profile named {args.name!r}")
        print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        return EXIT_IN_SYNC

    if args.profile_action == "delete":
        if args.name not in profiles:
            raise AuditError(f"no profile named {args.name!r}")
        del profiles[args.name]
        save_profiles(profiles)
        print(f"  Deleted profile {args.name!r}")
        return EXIT_IN_SYNC

    return EXIT_ERROR


def cmd_history(args) -> int:
    entries = load_history(limit=args.limit)
    if not entries:
        print(f"\n  No audits recorded yet. They will appear in {audits_dir()}\n")
        return EXIT_IN_SYNC

    print(f"\n  Audit history ({audits_dir()}):\n")
    header = f"  {'When':<20} {'Profile':<16} {'Left':>8} {'Right':>8} {'Conflicts':>10} {'Verify':<9}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for entry in entries:
        counts = entry.get("counts", {})
        print(
            f"  {entry.get('started', '')[:19]:<20} "
            f"{entry.get('profile', '')[:15]:<16} "
            f"{counts.get('only_left', 0):>8,} "
            f"{counts.get('only_right', 0):>8,} "
            f"{counts.get('conflicts', 0):>10,} "
            f"{entry.get('verify', ''):<9}"
        )
    print("\n  'Left'/'Right' count files missing from the OTHER drive.")
    print("  Compare two runs with:  ssdaudit compare-runs -2 latest\n")
    return EXIT_IN_SYNC


def cmd_compare_runs(args) -> int:
    base = Path(args.out) if args.out else None
    older = load_run(resolve_run_id(args.older, base), base)
    newer = load_run(resolve_run_id(args.newer, base), base)
    print()
    print(format_diff(diff_runs(older, newer)))
    return EXIT_IN_SYNC


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ssdaudit",
        description="Compare two drives: find what is missing, conflicting or duplicated. "
                    "Read-only - it never modifies either drive.",
    )
    parser.add_argument("--version", action="version", version=f"ssdaudit {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    def add_selection(sub):
        sub.add_argument("--profile", help="use a saved profile")
        sub.add_argument("--left", help="path to the first drive or folder")
        sub.add_argument("--right", help="path to the second drive or folder")
        sub.add_argument("--folder", action="append",
                         help="restrict to this folder (repeatable)")
        sub.add_argument("--preset", action="append", choices=["dev"],
                         help="add an ignore preset")
        sub.add_argument("--exclude", action="append", help="glob to exclude (repeatable)")
        sub.add_argument("--include", action="append", help="glob to include exclusively")
        sub.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help=f"parallel hashing threads (default {DEFAULT_WORKERS})")
        sub.add_argument("--cache", help="override the hash cache location")

    def add_dupe_options(sub):
        sub.add_argument("--dupe-scope", choices=["left", "right", "cross", "both"],
                         default="both", help="where to look for duplicates")
        sub.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE,
                         help=f"ignore files below this size (default {DEFAULT_MIN_SIZE})")

    subparsers.add_parser("gui", help="open the desktop window (start here)")
    subparsers.add_parser("volumes", help="list attached drives and their serials")

    run_parser = subparsers.add_parser(
        "run", help="choose drives interactively, then audit them (start here)")
    add_dupe_options(run_parser)
    run_parser.add_argument("--preset", action="append", choices=["dev"],
                            help="add an ignore preset")
    run_parser.add_argument("--exclude", action="append", help="glob to exclude (repeatable)")
    run_parser.add_argument("--include", action="append", help="glob to include exclusively")
    run_parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                            help=f"parallel hashing threads (default {DEFAULT_WORKERS})")
    run_parser.add_argument("--cache", help="override the hash cache location")
    run_parser.add_argument("--out", help="directory to write audit runs into")
    run_parser.add_argument("--quiet", action="store_true",
                            help="suppress the live progress line")

    compare_parser = subparsers.add_parser(
        "compare", help="audit two drives non-interactively")
    add_selection(compare_parser)
    add_dupe_options(compare_parser)
    compare_parser.add_argument("--verify", choices=VERIFY_LEVELS,
                                help="how hard to work to prove files match (default smart)")
    compare_parser.add_argument("--no-dupes", action="store_true",
                                help="skip duplicate detection")
    compare_parser.add_argument("--keep-cruft", action="store_true",
                                help="compare Thumbs.db/.DS_Store instead of reporting them separately")
    compare_parser.add_argument("--out", help="directory to write audit runs into")
    compare_parser.add_argument("--quiet", action="store_true",
                                help="suppress the live progress line")

    dupes_parser = subparsers.add_parser("dupes", help="find duplicate files only")
    add_selection(dupes_parser)
    add_dupe_options(dupes_parser)

    pick_parser = subparsers.add_parser("pick", help="choose folders interactively")
    add_selection(pick_parser)
    pick_parser.add_argument("--verify", choices=VERIFY_LEVELS)

    profiles_parser = subparsers.add_parser("profiles", help="manage saved profiles")
    profile_subs = profiles_parser.add_subparsers(dest="profile_action")
    profile_subs.add_parser("list")
    show_parser = profile_subs.add_parser("show")
    show_parser.add_argument("name")
    delete_parser = profile_subs.add_parser("delete")
    delete_parser.add_argument("name")

    history_parser = subparsers.add_parser("history", help="show past audits")
    history_parser.add_argument("--limit", type=int, default=20)

    runs_parser = subparsers.add_parser(
        "compare-runs", help="show what changed between two audits")
    runs_parser.add_argument("older", help="run id, 'latest', or '-2' for two runs back")
    runs_parser.add_argument("newer", help="run id, 'latest', or '-1'")
    runs_parser.add_argument("--out", help="directory audits were written to")

    return parser


HANDLERS = {
    "gui": cmd_gui,
    "volumes": cmd_volumes,
    "run": cmd_run,
    "compare": cmd_compare,
    "dupes": cmd_dupes,
    "pick": cmd_pick,
    "profiles": cmd_profiles,
    "history": cmd_history,
    "compare-runs": cmd_compare_runs,
}


def make_output_encodable() -> None:
    """Stop an un-printable filename from aborting the audit.

    Console output on Windows defaults to a legacy code page (cp1252 for a UK
    install), which cannot represent most non-Western-European filenames -- a
    single ``Zdjęcia`` or ``Zażółć.jpg`` on the drive would otherwise raise
    UnicodeEncodeError partway through and take the whole run down. The audit
    itself is unaffected by how a name renders, so replacement characters are a
    far better outcome than a crash.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # already-wrapped or non-reconfigurable stream; not worth failing over


def main(argv: list[str] | None = None) -> int:
    make_output_encodable()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_IN_SYNC

    try:
        return HANDLERS[args.command](args)
    except AuditError as error:
        print(f"\n  Error: {error}\n", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n  Cancelled. Nothing was modified.\n", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError, FileNotFoundError) as error:
        print(f"\n  Error: {error}\n", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
