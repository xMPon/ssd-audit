"""Remediation script generation.

This tool never writes to the drives it audits. What it produces instead is a
set of scripts you can read, edit and run yourself.

The safety rules are deliberate, and they differ per category:

* **Missing files** are safe to copy, so those scripts are ready to run.
* **Duplicates** are emitted commented out, and as a *move to quarantine* rather
  than a delete -- so a mistake is recoverable.
* **Conflicts are never scripted at all.** Two files sharing a path with
  different content means one version is about to be lost, and only you know
  which. They get a listing to review, not a command to run.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .compare import CompareResult
from .dupes import DupeResult
from .report import format_bytes, format_time

# cmd.exe truncates past 8191 characters; leave generous headroom for the
# fixed flags and both quoted directory paths.
MAX_COMMAND_LENGTH = 6000

ROBOCOPY_FLAGS = "/COPY:DAT /DCOPY:DAT /R:1 /W:1 /NP /NJH /NJS /TEE"


def _escape(text: str) -> str:
    """Batch files treat ``%`` as variable expansion, even inside quotes."""
    return text.replace("%", "%%")


def _banner(title: str, lines: list[str]) -> list[str]:
    out = [
        "@echo off",
        "setlocal",
        "chcp 65001 >nul",
        "",
        f"echo ================================================================",
        f"echo  {title}",
        "echo ================================================================",
    ]
    for line in lines:
        out.append(f"echo  {line}")
    out += [
        "echo.",
        "echo  Review this script before continuing. Nothing has run yet.",
        "echo.",
        "pause",
        "",
    ]
    return out


def write_copy_script(
    records,
    source_root: str,
    dest_root: str,
    path: Path,
    direction: str,
    log_name: str,
    already_present: dict[str, list[str]] | None = None,
    dupes_checked: bool = True,
) -> Path:
    """Emit robocopy commands that fill gaps from *source_root* into *dest_root*.

    Files are grouped by parent directory so one robocopy call moves many files;
    invoking robocopy per file would be dramatically slower.

    *already_present* maps a source path to the places the same content already
    sits on the destination drive under a different name. Those are held back:
    a file you merely *moved* looks identical to a file that is *missing*, and
    copying it would leave you with two copies under two names -- the exact mess
    this tool exists to find.
    """
    already_present = already_present or {}
    skipped = [record for record in records if record.relpath in already_present]
    records = [record for record in records if record.relpath not in already_present]

    total_bytes = sum(record.size for record in records)
    lines = _banner(
        f"COPY {direction}",
        [
            f"From: {source_root}",
            f"To:   {dest_root}",
            f"{len(records)} files, {format_bytes(total_bytes)}",
        ]
        + ([f"{len(skipped)} file(s) held back - already on the destination under another name"]
           if skipped else [])
        + ([] if dupes_checked else [
            "",
            "WARNING: duplicate detection was skipped for this audit.",
            "A file you merely MOVED cannot be told apart from one that is",
            "missing, so this script may copy files the destination already",
            "holds under a different name. Re-run without --no-dupes to check.",
        ]),
    )

    if skipped:
        lines += [
            "",
            "REM ---------------------------------------------------------------",
            "REM  HELD BACK: the same content is already on the destination drive,",
            "REM  just under a different path. Copying these would create a second",
            "REM  copy rather than filling a gap. Decide which path you want to",
            "REM  keep, then move the file on the destination drive yourself.",
            "REM ---------------------------------------------------------------",
        ]
        for record in skipped:
            lines.append(f"REM   source:      {_escape(record.relpath)}")
            for destination in already_present[record.relpath]:
                lines.append(f"REM   already at:  {_escape(destination)}")
        lines.append("")

    if not records:
        lines += ["echo  Nothing to copy - the drives already match.", "goto :done", ""]
    else:
        lines += [
            "REM robocopy exit codes 0-7 indicate success; 8 and above are failures.",
            "",
        ]
        by_directory: dict[str, list[str]] = {}
        for record in records:
            parent, name = os.path.split(record.relpath)
            by_directory.setdefault(parent, []).append(name)

        for parent in sorted(by_directory):
            source = os.path.join(source_root, parent.replace("/", os.sep)) if parent else source_root
            dest = os.path.join(dest_root, parent.replace("/", os.sep)) if parent else dest_root
            prefix = f'robocopy "{_escape(source)}" "{_escape(dest)}"'
            suffix = f' {ROBOCOPY_FLAGS} /LOG+:"%~dp0{log_name}"'

            batch: list[str] = []
            length = len(prefix) + len(suffix)
            for name in sorted(by_directory[parent]):
                quoted = f' "{_escape(name)}"'
                if batch and length + len(quoted) > MAX_COMMAND_LENGTH:
                    lines.append(prefix + "".join(batch) + suffix)
                    batch, length = [], len(prefix) + len(suffix)
                batch.append(quoted)
                length += len(quoted)
            if batch:
                lines.append(prefix + "".join(batch) + suffix)

    lines += [
        "",
        ":done",
        "echo.",
        "echo  Finished. Check the log next to this script for details.",
        "pause",
        "endlocal",
        "",
    ]
    path.write_text("\r\n".join(lines), encoding="utf-8")
    return path


def write_duplicate_script(dupes: DupeResult, left_root: str, right_root: str, path: Path) -> Path:
    """Emit quarantine moves for duplicate files -- every line commented out.

    Nothing here is deletion. Files are moved to a ``_ssdaudit-quarantine``
    folder on the same drive, so if the audit got something wrong you can put it
    straight back.
    """
    groups = [("left", left_root, dupes.within_left), ("right", right_root, dupes.within_right)]
    total = sum(
        group.wasted_bytes for _, _, bucket in groups for group in bucket
    )

    lines = _banner(
        "DUPLICATE CLEANUP - ALL ACTIONS DISABLED",
        [
            f"Potential space to reclaim: {format_bytes(total)}",
            "Every command below is commented out with REM.",
            "Files are MOVED to a quarantine folder, never deleted.",
            "Uncomment only the lines you have checked yourself.",
        ],
    )

    any_groups = False
    for side, root, bucket in groups:
        if not bucket:
            continue
        any_groups = True
        quarantine = os.path.join(root, "_ssdaudit-quarantine")
        lines += [
            "",
            f"REM ==================== {side.upper()} DRIVE ====================",
            f'REM mkdir "{_escape(quarantine)}"',
        ]
        for index, group in enumerate(bucket, start=1):
            lines += [
                "",
                f"REM --- group {index}: {len(group.entries)} copies of "
                f"{format_bytes(group.size)}, {format_bytes(group.wasted_bytes)} reclaimable",
            ]
            keep = group.entries[0][1]
            lines.append(f"REM     KEEP    {keep.relpath}")
            for _, record in group.entries[1:]:
                source = os.path.join(root, record.relpath.replace("/", os.sep))
                target = os.path.join(quarantine, record.relpath.replace("/", os.sep))
                lines.append(f"REM     REMOVE  {record.relpath}")
                lines.append(f'REM mkdir "{_escape(os.path.dirname(target))}" 2>nul')
                lines.append(f'REM move /Y "{_escape(source)}" "{_escape(target)}"')

    if not any_groups:
        lines.append("echo  No duplicates found.")

    lines += ["", "echo.", "pause", "endlocal", ""]
    path.write_text("\r\n".join(lines), encoding="utf-8")
    return path


def _digest(record) -> str:
    """Show whichever digest was computed.

    A size mismatch is proven without reading anything, and a quick-hash
    mismatch settles it without a full read -- so a conflict often has no full
    hash, and saying "not hashed" would misrepresent that as a gap in the audit.
    """
    if record.full_hash:
        return f"full {record.full_hash[:16]}"
    if record.quick_hash:
        return f"quick {record.quick_hash[1:17]}"
    return "(size differs; content not read)"


def write_conflict_listing(result: CompareResult, path: Path) -> Path:
    """Write conflicts as a plain listing. Deliberately not executable.

    A conflict means the same path holds different content on each drive.
    Copying either way destroys one version, so this produces information to
    decide with -- never a command that decides for you.
    """
    lines = [
        "CONTENT CONFLICTS",
        "=" * 70,
        "",
        "Same path on both drives, different content. There is no safe automatic",
        "resolution: copying either direction overwrites the other version.",
        "",
        f"Left:  {result.left.root}",
        f"Right: {result.right.root}",
        f"Found: {len(result.conflicts)}",
        "",
        "Guidance is in docs/RECONCILIATION.md.",
        "",
    ]

    if not result.conflicts:
        lines.append("None found.")
    for pair in result.conflicts:
        newer = "left" if pair.left.mtime_ns > pair.right.mtime_ns else "right"
        lines += [
            "-" * 70,
            pair.relpath,
            f"  left   {format_bytes(pair.left.size):>12}   {format_time(pair.left.mtime_ns)}"
            f"   {_digest(pair.left)}",
            f"  right  {format_bytes(pair.right.size):>12}   {format_time(pair.right.mtime_ns)}"
            f"   {_digest(pair.right)}",
            f"  reason: {pair.reason}",
            f"  newer:  {newer} (newer is not automatically correct)",
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _relocated(dupes: DupeResult, side: str) -> dict[str, list[str]]:
    """Map a path on *side* to where that same content already sits on the other drive."""
    other = "right" if side == "left" else "left"
    mapping: dict[str, list[str]] = {}
    for group in dupes.cross:
        mine = [record.relpath for entry_side, record in group.entries if entry_side == side]
        theirs = [record.relpath for entry_side, record in group.entries if entry_side == other]
        if not theirs:
            continue
        for relpath in mine:
            mapping.setdefault(relpath, []).extend(theirs)
    return mapping


def write_all(result: CompareResult, dupes: DupeResult | None, directory: Path) -> list[Path]:
    """Generate every remediation artefact for one audit run.

    *dupes* is None when duplicate detection was skipped (``--no-dupes``). The
    scripts are still generated -- they just cannot hold back moved files,
    because nothing has established which files merely moved.
    """
    directory.mkdir(parents=True, exist_ok=True)
    dupes_checked = dupes is not None
    dupes = dupes or DupeResult()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    written = [
        write_copy_script(
            result.only_left, result.left.root, result.right.root,
            directory / "sync-left-to-right.cmd", "LEFT -> RIGHT", f"copy-l2r-{stamp}.log",
            already_present=_relocated(dupes, "left"), dupes_checked=dupes_checked,
        ),
        write_copy_script(
            result.only_right, result.right.root, result.left.root,
            directory / "sync-right-to-left.cmd", "RIGHT -> LEFT", f"copy-r2l-{stamp}.log",
            already_present=_relocated(dupes, "right"), dupes_checked=dupes_checked,
        ),
        write_duplicate_script(dupes, result.left.root, result.right.root,
                               directory / "review-duplicates.cmd"),
        write_conflict_listing(result, directory / "conflicts.txt"),
    ]
    return written
