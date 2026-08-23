"""Interactive folder selection.

A numbered toggle list read through ``input()`` rather than a raw-terminal TUI.
That choice is deliberate: it works over SSH, inside the VS Code terminal, and
anywhere ``msvcrt``/``termios`` would misbehave, and it needs no dependency.
"""

from __future__ import annotations

import os
import sys

from .config import IgnoreRules
from .paths import long_path

MENU = """
Commands:
  <numbers>  toggle selection      (e.g. "1 3 5" or "2-6")
  a / n      select all / none
  > <n>      descend into folder n
  <          go back up
  l          list current selection
  q          cancel
  <Enter>    confirm
"""


def _subdirectories(path: str, rules: IgnoreRules) -> list[str]:
    try:
        entries = os.scandir(long_path(path))
    except OSError:
        return []
    names = []
    with entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and not rules.skip_dir(entry.name):
                    names.append(entry.name)
            except OSError:
                continue
    return sorted(names, key=str.casefold)


def _parse_selection(token: str, count: int) -> list[int]:
    """Understand ``3``, ``1 4 7`` and ``2-6``."""
    chosen: list[int] = []
    for part in token.replace(",", " ").split():
        if "-" in part and not part.startswith("-"):
            start, _, end = part.partition("-")
            if start.isdigit() and end.isdigit():
                chosen.extend(range(int(start), int(end) + 1))
                continue
        if part.isdigit():
            chosen.append(int(part))
    return [index for index in chosen if 1 <= index <= count]


def choose_folders(root: str, rules: IgnoreRules | None = None, preselected=None) -> list[str]:
    """Let the user pick folders beneath *root*, returning relative paths.

    An empty result means "compare the whole drive".
    """
    rules = rules or IgnoreRules()
    selected: set[str] = set(preselected or [])
    prefix = ""

    if not sys.stdin.isatty():
        raise RuntimeError("folder picker needs an interactive terminal; use --folder instead")

    while True:
        current = os.path.join(root, prefix.replace("/", os.sep)) if prefix else root
        names = _subdirectories(current, rules)

        print()
        print(f"  {root}{os.sep}{prefix}" if prefix else f"  {root}")
        print("  " + "-" * 60)

        if not names:
            print("  (no sub-folders here)")
        for index, name in enumerate(names, start=1):
            child = f"{prefix}/{name}" if prefix else name
            mark = "*" if child in selected else " "
            covered = any(child.startswith(f"{item}/") for item in selected)
            note = "  (already covered by a parent selection)" if covered else ""
            print(f"  [{mark}] {index:>3}. {name}{note}")

        print(MENU)
        print(f"  Selected: {len(selected)} folder(s)")

        try:
            answer = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return sorted(selected)

        if answer == "":
            return sorted(selected)
        if answer == "q":
            return []
        if answer == "a":
            selected.update(f"{prefix}/{name}" if prefix else name for name in names)
            continue
        if answer == "n":
            selected.clear()
            continue
        if answer == "l":
            print("\n  Currently selected:")
            for item in sorted(selected) or ["  (nothing - the whole drive will be compared)"]:
                print(f"    {item}")
            continue
        if answer == "<":
            prefix = prefix.rpartition("/")[0]
            continue
        if answer.startswith(">"):
            target = _parse_selection(answer[1:], len(names))
            if target:
                name = names[target[0] - 1]
                prefix = f"{prefix}/{name}" if prefix else name
            continue

        for index in _parse_selection(answer, len(names)):
            child = f"{prefix}/{names[index - 1]}" if prefix else names[index - 1]
            selected.symmetric_difference_update({child})
