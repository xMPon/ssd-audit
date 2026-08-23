"""Directory walking.

Uses ``os.scandir`` and prunes ignored directories *before* descending into
them, which is where nearly all the time is saved on a whole-drive scan --
skipping ``System Volume Information`` or a ``node_modules`` tree costs one
string comparison instead of a hundred thousand stat calls.

Errors on individual entries are collected rather than raised. A drive with one
unreadable folder should still produce a complete audit of everything else, with
the failure reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import IgnoreRules
from .paths import long_path, match_key, to_posix, unicode_form
from .volumes import volume_id


@dataclass(slots=True)
class FileRecord:
    """One file, as seen during a walk. No content is read at this stage."""

    relpath: str
    key: str
    size: int
    mtime_ns: int
    quick_hash: str = ""
    full_hash: str = ""

    def to_dict(self) -> dict:
        data = {
            "relpath": self.relpath,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }
        if self.quick_hash:
            data["quick_hash"] = self.quick_hash
        if self.full_hash:
            data["full_hash"] = self.full_hash
        return data


@dataclass
class ScanResult:
    """Everything one side of the comparison contributed."""

    root: str
    volume: str
    files: dict[str, FileRecord] = field(default_factory=dict)
    cruft: list[FileRecord] = field(default_factory=list)
    case_collisions: list[tuple[str, str]] = field(default_factory=list)
    nfd_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dirs_scanned: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(record.size for record in self.files.values())

    @property
    def cruft_bytes(self) -> int:
        return sum(record.size for record in self.cruft)

    def abspath(self, relpath: str) -> str:
        return os.path.join(self.root, relpath.replace("/", os.sep))


def scan(
    root: str,
    rules: IgnoreRules | None = None,
    folders: list[str] | None = None,
    on_progress=None,
) -> ScanResult:
    """Walk *root*, optionally restricted to *folders* beneath it.

    Restricting to a subset of folders is what makes "audit just this one
    directory" as cheap as it should be -- unlisted trees are never opened.
    """
    rules = rules or IgnoreRules()
    result = ScanResult(root=os.path.abspath(root), volume=volume_id(root))

    starts = []
    if folders:
        for folder in folders:
            relative = to_posix(folder)
            starts.append((os.path.join(result.root, relative.replace("/", os.sep)), relative))
    else:
        starts.append((result.root, ""))

    for start_path, start_rel in starts:
        if not os.path.isdir(long_path(start_path)):
            result.errors.append(f"not a directory: {start_path}")
            continue
        _walk(start_path, start_rel, rules, result, on_progress)

    return result


def _walk(start_path: str, start_rel: str, rules: IgnoreRules, result: ScanResult, on_progress) -> None:
    stack = [(start_path, start_rel)]

    while stack:
        current_path, current_rel = stack.pop()
        result.dirs_scanned += 1
        if on_progress and result.dirs_scanned % 500 == 0:
            on_progress(result)

        try:
            entries = list(os.scandir(long_path(current_path)))
        except (PermissionError, OSError) as error:
            result.errors.append(f"{current_path}: {error}")
            continue

        for entry in entries:
            name = entry.name
            child_rel = f"{current_rel}/{name}" if current_rel else name

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as error:
                result.errors.append(f"{child_rel}: {error}")
                continue

            if is_dir:
                if rules.skip_dir(name):
                    continue
                stack.append((os.path.join(current_path, name), child_rel))
                continue

            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                result.errors.append(f"{child_rel}: {error}")
                continue

            record = FileRecord(
                relpath=child_rel,
                key=match_key(child_rel),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )

            if rules.is_cruft(name):
                result.cruft.append(record)
                continue

            if rules.skip_file(child_rel, name):
                continue

            if not name.isascii() and unicode_form(child_rel) == "NFD":
                result.nfd_paths.append(child_rel)

            existing = result.files.get(record.key)
            if existing is not None:
                # Only reachable on a case-sensitive volume; on exFAT/NTFS these
                # cannot coexist, which is exactly why copying between the two
                # would silently lose one of them.
                result.case_collisions.append((existing.relpath, record.relpath))
                continue

            result.files[record.key] = record
