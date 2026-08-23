"""Comparison of two scanned drives.

The drives are treated as **peers**. Neither side is authoritative, so a file
existing on only one drive is a gap to be filled, never something to delete.

Classification is metadata-first. Content is only read when metadata genuinely
cannot settle the question:

* different size          -> proven different, no read needed
* same size, same mtime   -> presumed identical (read only under ``--verify full``)
* same size, mtime differs -> **ambiguous**, must be read

That last case is the only one that costs I/O in a normal run, and it is
usually a tiny fraction of the drive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .index import HashIndex
from .resolver import DEFAULT_WORKERS, resolve
from .scanner import FileRecord, ScanResult

# exFAT records mtime to 2-second granularity, so anything finer is noise.
MTIME_TOLERANCE_NS = 2_000_000_000

# FAT-family volumes shift timestamps by a whole hour across DST boundaries.
# Treating that as a real change would flag most of a drive every spring.
DST_OFFSETS_NS = (3_600_000_000_000, 7_200_000_000_000)

VERIFY_LEVELS = ("metadata", "smart", "full")


@dataclass
class Pair:
    """The same logical file on both drives."""

    left: FileRecord
    right: FileRecord
    reason: str = ""

    @property
    def relpath(self) -> str:
        return self.left.relpath

    def to_dict(self) -> dict:
        return {
            "relpath": self.left.relpath,
            "relpath_right": self.right.relpath,
            "reason": self.reason,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }


@dataclass
class CompareResult:
    """Outcome of one audit, in the shape the reports and scripts consume."""

    left: ScanResult
    right: ScanResult
    verify: str = "smart"
    only_left: list[FileRecord] = field(default_factory=list)
    only_right: list[FileRecord] = field(default_factory=list)
    identical: list[Pair] = field(default_factory=list)
    conflicts: list[Pair] = field(default_factory=list)
    dst_artifacts: list[Pair] = field(default_factory=list)
    unverified: list[Pair] = field(default_factory=list)
    name_case_differs: list[Pair] = field(default_factory=list)
    unicode_differs: list[Pair] = field(default_factory=list)
    hash_errors: list[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not (self.only_left or self.only_right or self.conflicts)

    @property
    def bytes_only_left(self) -> int:
        return sum(record.size for record in self.only_left)

    @property
    def bytes_only_right(self) -> int:
        return sum(record.size for record in self.only_right)

    def counts(self) -> dict:
        return {
            "left_files": len(self.left.files),
            "right_files": len(self.right.files),
            "only_left": len(self.only_left),
            "only_right": len(self.only_right),
            "identical": len(self.identical),
            "conflicts": len(self.conflicts),
            "dst_artifacts": len(self.dst_artifacts),
            "unverified": len(self.unverified),
            "name_case_differs": len(self.name_case_differs),
            "unicode_differs": len(self.unicode_differs),
            "case_collisions": len(self.left.case_collisions) + len(self.right.case_collisions),
            "cruft_files": len(self.left.cruft) + len(self.right.cruft),
            "bytes_only_left": self.bytes_only_left,
            "bytes_only_right": self.bytes_only_right,
            "bytes_cruft": self.left.cruft_bytes + self.right.cruft_bytes,
            "scan_errors": len(self.left.errors) + len(self.right.errors),
        }


def classify_mtime(left_ns: int, right_ns: int) -> str:
    """``same``, ``dst`` (whole-hour filesystem artefact), or ``differs``."""
    delta = abs(left_ns - right_ns)
    if delta <= MTIME_TOLERANCE_NS:
        return "same"
    for offset in DST_OFFSETS_NS:
        if abs(delta - offset) <= MTIME_TOLERANCE_NS:
            return "dst"
    return "differs"


def compare(
    left: ScanResult,
    right: ScanResult,
    index: HashIndex,
    verify: str = "smart",
    workers: int = DEFAULT_WORKERS,
    on_progress=None,
) -> CompareResult:
    """Pair up two scans and classify every difference."""
    if verify not in VERIFY_LEVELS:
        raise ValueError(f"verify must be one of {VERIFY_LEVELS}, got {verify!r}")

    result = CompareResult(left=left, right=right, verify=verify)

    left_keys = set(left.files)
    right_keys = set(right.files)

    for key in sorted(left_keys - right_keys):
        result.only_left.append(left.files[key])
    for key in sorted(right_keys - left_keys):
        result.only_right.append(right.files[key])

    ambiguous: list[Pair] = []

    for key in sorted(left_keys & right_keys):
        left_record = left.files[key]
        right_record = right.files[key]
        pair = Pair(left=left_record, right=right_record)

        # The names matched only after case/Unicode folding, so the bytes on
        # disk differ even though the files correspond. Worth surfacing: it
        # breaks naive sync tools and creates accidental duplicates.
        if left_record.relpath != right_record.relpath:
            if left_record.relpath.casefold() == right_record.relpath.casefold():
                result.unicode_differs.append(pair)
            else:
                result.name_case_differs.append(pair)

        if left_record.size != right_record.size:
            pair.reason = "size differs"
            result.conflicts.append(pair)
            continue

        timing = classify_mtime(left_record.mtime_ns, right_record.mtime_ns)

        if timing == "same":
            if verify == "full":
                ambiguous.append(pair)
            else:
                pair.reason = "same size and timestamp"
                result.identical.append(pair)
            continue

        pair.reason = "timestamp differs by a whole hour (DST artefact)" if timing == "dst" else "timestamp differs"
        if verify == "metadata":
            result.unverified.append(pair)
        else:
            ambiguous.append(pair)

    if ambiguous:
        _verify_pairs(ambiguous, result, index, workers, on_progress)

    return result


def _verify_pairs(
    pairs: list[Pair],
    result: CompareResult,
    index: HashIndex,
    workers: int,
    on_progress,
) -> None:
    """Settle ambiguous pairs by reading content.

    Quick hashes first: they reject most genuinely-different files after two
    seeks. Only pairs that survive that are read in full.
    """
    left_records = [pair.left for pair in pairs]
    right_records = [pair.right for pair in pairs]

    result.hash_errors += resolve(
        left_records, result.left.root, result.left.volume, index, "quick", workers, on_progress
    )
    result.hash_errors += resolve(
        right_records, result.right.root, result.right.volume, index, "quick", workers, on_progress
    )

    still_unsettled: list[Pair] = []
    for pair in pairs:
        if not pair.left.quick_hash or not pair.right.quick_hash:
            pair.reason = "could not be read"
            result.unverified.append(pair)
        elif pair.left.quick_hash != pair.right.quick_hash:
            pair.reason = "content differs"
            result.conflicts.append(pair)
        else:
            still_unsettled.append(pair)

    if not still_unsettled:
        return

    result.hash_errors += resolve(
        [pair.left for pair in still_unsettled],
        result.left.root, result.left.volume, index, "full", workers, on_progress,
    )
    result.hash_errors += resolve(
        [pair.right for pair in still_unsettled],
        result.right.root, result.right.volume, index, "full", workers, on_progress,
    )

    for pair in still_unsettled:
        if not pair.left.full_hash or not pair.right.full_hash:
            pair.reason = "could not be read"
            result.unverified.append(pair)
        elif pair.left.full_hash == pair.right.full_hash:
            timing = classify_mtime(pair.left.mtime_ns, pair.right.mtime_ns)
            if timing == "dst":
                pair.reason = "identical content, whole-hour timestamp artefact"
                result.dst_artifacts.append(pair)
            else:
                pair.reason = "identical content, differing timestamp"
                result.identical.append(pair)
        else:
            pair.reason = "content differs"
            result.conflicts.append(pair)
