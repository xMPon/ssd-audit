"""Duplicate detection, within each drive and across the pair.

A three-stage funnel, each stage cheaper than the one it feeds:

1. **Group by size.** Two files with different sizes cannot be duplicates, and
   most sizes are unique, so this eliminates the bulk for free.
2. **Quick hash** the survivors -- two seeks each.
3. **Full hash** only what still collides.

Cross-drive duplicates matter for a different reason than wasted space: a file
you *moved* to a new folder on one drive looks like "missing from the other
drive" to a path-based comparison. Without this step the audit would tell you to
copy it back, leaving you with two copies under two names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .index import HashIndex
from .resolver import DEFAULT_WORKERS, resolve
from .scanner import FileRecord, ScanResult

# Below this, duplicates are usually noise -- empty markers, tiny configs,
# thousands of identical __init__.py files -- and drown the real findings.
DEFAULT_MIN_SIZE = 4096


@dataclass
class DupeGroup:
    """Files with byte-identical content."""

    digest: str
    size: int
    entries: list[tuple[str, FileRecord]] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """Space reclaimable by keeping exactly one copy."""
        return self.size * (len(self.entries) - 1)

    def paths(self) -> list[str]:
        return [f"{side}:{record.relpath}" for side, record in self.entries]

    def to_dict(self) -> dict:
        return {
            "digest": self.digest,
            "size": self.size,
            "wasted_bytes": self.wasted_bytes,
            "entries": [
                {"side": side, "relpath": record.relpath, "mtime_ns": record.mtime_ns}
                for side, record in self.entries
            ],
        }


@dataclass
class DupeResult:
    within_left: list[DupeGroup] = field(default_factory=list)
    within_right: list[DupeGroup] = field(default_factory=list)
    cross: list[DupeGroup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "dupe_groups_left": len(self.within_left),
            "dupe_groups_right": len(self.within_right),
            "dupe_groups_cross": len(self.cross),
            "bytes_wasted_left": sum(group.wasted_bytes for group in self.within_left),
            "bytes_wasted_right": sum(group.wasted_bytes for group in self.within_right),
        }


def find_duplicates(
    left: ScanResult,
    right: ScanResult,
    index: HashIndex,
    scope: str = "both",
    min_size: int = DEFAULT_MIN_SIZE,
    workers: int = DEFAULT_WORKERS,
    on_progress=None,
) -> DupeResult:
    """Find duplicate content. *scope* is ``left``, ``right``, ``cross`` or ``both``."""
    result = DupeResult()

    sides: list[tuple[str, ScanResult]] = []
    if scope in ("left", "both", "cross"):
        sides.append(("left", left))
    if scope in ("right", "both", "cross"):
        sides.append(("right", right))

    by_size: dict[int, list[tuple[str, FileRecord]]] = defaultdict(list)
    for side, scan in sides:
        for record in scan.files.values():
            if record.size >= min_size:
                by_size[record.size].append((side, record))

    candidates = [entry for group in by_size.values() if len(group) > 1 for entry in group]
    if not candidates:
        return result

    roots = {"left": (left.root, left.volume), "right": (right.root, right.volume)}

    result.errors += _hash_tier(candidates, roots, index, "quick", workers, on_progress)

    by_quick: dict[tuple[int, str], list[tuple[str, FileRecord]]] = defaultdict(list)
    for side, record in candidates:
        if record.quick_hash:
            by_quick[(record.size, record.quick_hash)].append((side, record))

    survivors = [entry for group in by_quick.values() if len(group) > 1 for entry in group]
    if not survivors:
        return result

    result.errors += _hash_tier(survivors, roots, index, "full", workers, on_progress)

    by_full: dict[str, list[tuple[str, FileRecord]]] = defaultdict(list)
    for side, record in survivors:
        if record.full_hash:
            by_full[record.full_hash].append((side, record))

    for digest, entries in by_full.items():
        if len(entries) < 2:
            continue
        size = entries[0][1].size
        left_entries = [item for item in entries if item[0] == "left"]
        right_entries = [item for item in entries if item[0] == "right"]

        if scope in ("left", "both") and len(left_entries) > 1:
            result.within_left.append(DupeGroup(digest, size, sorted(left_entries, key=_sort_key)))
        if scope in ("right", "both") and len(right_entries) > 1:
            result.within_right.append(DupeGroup(digest, size, sorted(right_entries, key=_sort_key)))

        if scope in ("cross", "both") and left_entries and right_entries:
            # Same path on both drives is just a synced file, not a duplicate.
            # Only differing paths mean a file was moved or renamed on one side.
            left_keys = {record.key for _, record in left_entries}
            right_keys = {record.key for _, record in right_entries}
            if not (left_keys & right_keys):
                result.cross.append(DupeGroup(digest, size, sorted(entries, key=_sort_key)))

    for bucket in (result.within_left, result.within_right, result.cross):
        bucket.sort(key=lambda group: group.wasted_bytes, reverse=True)

    return result


def _sort_key(entry: tuple[str, FileRecord]) -> tuple:
    """Shallowest path first -- the most likely candidate to keep."""
    side, record = entry
    return (record.relpath.count("/"), len(record.relpath), side, record.relpath)


def _hash_tier(entries, roots, index, tier, workers, on_progress) -> list[str]:
    """Hash a mixed left/right candidate set, one drive at a time."""
    errors: list[str] = []
    for side in ("left", "right"):
        records = [record for entry_side, record in entries if entry_side == side]
        if not records:
            continue
        root, volume = roots[side]
        errors += resolve(records, root, volume, index, tier, workers, on_progress)
    return errors
