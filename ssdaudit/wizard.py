"""Interactive setup.

Choosing what to compare is the decision most likely to be got wrong, and
getting it wrong wastes a long scan on the wrong data. So the wizard makes every
choice explicit, shows exactly what was selected, and asks for confirmation
before reading anything.

Nothing here touches a drive beyond listing folder names.
"""

from __future__ import annotations

import os
import sys

from .config import IgnoreRules
from .picker import choose_folders
from .progress import format_bytes
from .volumes import Volume, list_volumes, volume_for_path

RULE = "=" * 74


def require_terminal() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "the interactive wizard needs a terminal.\n"
            "  Use the non-interactive form instead:\n"
            "    ssdaudit compare --left <PATH> --right <PATH>"
        )


def heading(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


def volume_table(volumes: list[Volume]) -> None:
    """Show every attached drive with enough detail to tell them apart."""
    print(f"\n   {'#':>2}  {'Drive':<7} {'Label':<20} {'Format':<8} "
          f"{'Capacity':>10} {'Used':>10} {'Free':>10}  Type")
    print("  " + "-" * 86)
    for index, volume in enumerate(volumes, start=1):
        print(
            f"   {index:>2}  {volume.mount:<7} {(volume.label or '(no label)'):<20} "
            f"{volume.fs_type:<8} "
            f"{format_bytes(volume.total_bytes):>10} "
            f"{format_bytes(volume.used_bytes):>10} "
            f"{format_bytes(volume.free_bytes):>10}  {volume.drive_type}"
        )


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise KeyboardInterrupt
    return answer or default


def choose_drive(role: str, volumes: list[Volume], exclude: str = "") -> str:
    """Pick one drive by number, or type any path.

    Typing a path is always allowed: comparing two folders on a single drive,
    or a network share, is a legitimate thing to want and shouldn't require a
    separate command.
    """
    while True:
        answer = ask(
            f"\n  Choose the {role} side "
            f"[1-{len(volumes)}, or type a folder path, q to quit]: "
        )

        if answer.lower() == "q":
            raise KeyboardInterrupt

        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(volumes):
                chosen = volumes[index - 1]
                if chosen.mount == exclude:
                    print(f"  {chosen.mount} is already the other side. "
                          "Pick a different drive, or type a folder path on it.")
                    continue
                return chosen.mount
            print(f"  Enter a number between 1 and {len(volumes)}.")
            continue

        if answer:
            path = os.path.abspath(os.path.expandvars(os.path.expanduser(answer)))
            if os.path.isdir(path):
                return path
            print(f"  Not a folder: {path}")
            continue

        print("  Enter a number or a path.")


def describe_side(role: str, path: str) -> str:
    volume = volume_for_path(path)
    if volume is None:
        return f"  {role:<6} {path}"
    label = volume.label or "(no label)"
    return (f"  {role:<6} {path}\n"
            f"         {label} | {volume.fs_type} | "
            f"{format_bytes(volume.used_bytes)} used of "
            f"{format_bytes(volume.total_bytes)} | serial {volume.serial}")


def choose_scope(left: str, right: str, rules: IgnoreRules) -> list[str]:
    """Whole drive, or a chosen set of folders."""
    print("\n  What should be compared?")
    print("    1. The whole drive")
    print("    2. Choose specific folders")

    while True:
        answer = ask("\n  Choice [1]: ", "1")
        if answer == "1":
            return []
        if answer == "2":
            break
        print("  Enter 1 or 2.")

    print(f"\n  Folders are chosen relative to the LEFT side ({left})")
    print("  and looked for at the same relative path on the right.")
    folders = choose_folders(left, rules)

    if folders:
        print("\n  Selected:")
        for folder in folders:
            target = os.path.join(right, folder.replace("/", os.sep))
            status = "found on both" if os.path.isdir(target) else "NOT on the right side"
            print(f"    {folder}   [{status}]")
    return folders


def choose_verify() -> str:
    print("\n  How thoroughly should files be checked?")
    print("    1. metadata  - compare size and timestamp only. No file contents read.")
    print("    2. smart     - read only the files metadata cannot settle.  (recommended)")
    print("    3. full      - read every file. Catches silent corruption; slowest.")

    options = {"1": "metadata", "2": "smart", "3": "full"}
    while True:
        answer = ask("\n  Choice [2]: ", "2")
        if answer in options:
            return options[answer]
        if answer in options.values():
            return answer
        print("  Enter 1, 2 or 3.")


def choose_duplicates() -> bool:
    print("\n  Also look for duplicate files?")
    print("    Finds content stored twice on one drive, and files that exist on")
    print("    both drives under different names. Requires reading file contents.")
    answer = ask("\n  Look for duplicates? [Y/n]: ", "y")
    return answer.lower().startswith("y")


def confirm_plan(left: str, right: str, folders: list[str], verify: str, dupes: bool) -> bool:
    heading("Ready to compare")
    print(describe_side("LEFT", left))
    print(describe_side("RIGHT", right))
    print(f"\n  Scope      {', '.join(folders) if folders else 'entire drive'}")
    print(f"  Verify     {verify}")
    print(f"  Duplicates {'yes' if dupes else 'skipped'}")
    print("\n  Both drives are read only. Nothing will be copied, moved or deleted.")

    answer = ask("\n  Start? [Y/n]: ", "y")
    return answer.lower().startswith("y")


def run_wizard(rules: IgnoreRules) -> dict | None:
    """Collect a full comparison setup. Returns None if the user backs out."""
    require_terminal()
    heading("ssd-audit - set up a comparison")

    volumes = list_volumes()
    if not volumes:
        raise RuntimeError("no volumes could be identified on this system")

    volume_table(volumes)
    print("\n  Drives are matched by serial number, so a profile saved here keeps")
    print("  working after Windows gives the drive a different letter.")

    left = choose_drive("FIRST (left)", volumes)
    right = choose_drive("SECOND (right)", volumes, exclude=left)

    folders = choose_scope(left, right, rules)
    verify = choose_verify()
    dupes = choose_duplicates()

    if not confirm_plan(left, right, folders, verify, dupes):
        print("\n  Cancelled. Nothing was read.\n")
        return None

    return {"left": left, "right": right, "folders": folders,
            "verify": verify, "dupes": dupes}
