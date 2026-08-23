"""Duplicate detection, within a drive and across the pair."""

from __future__ import annotations

from collections import Counter

from ssdaudit.dupes import find_duplicates
from ssdaudit.scanner import scan


def run(left, right, index, **kwargs):
    kwargs.setdefault("min_size", 1)
    return find_duplicates(scan(str(left)), scan(str(right)), index, **kwargs)


def flatten(groups):
    return {path for group in groups for path in group.paths()}


class TestWithinDrive:
    def test_finds_the_same_file_stored_twice(self, trees, index):
        left, right = trees
        dupes = run(left, right, index)

        paths = flatten(dupes.within_left)
        assert "left:pics/holiday.jpg" in paths
        assert "left:pics/backup/holiday-copy.jpg" in paths

    def test_reports_reclaimable_space_for_one_kept_copy(self, trees, index):
        left, right = trees
        dupes = run(left, right, index)

        group = next(g for g in dupes.within_left if "holiday.jpg" in " ".join(g.paths()))
        assert group.wasted_bytes == group.size * (len(group.entries) - 1)

    def test_shallowest_path_is_offered_as_the_one_to_keep(self, trees, index):
        left, right = trees
        dupes = run(left, right, index)

        group = next(g for g in dupes.within_left if "holiday.jpg" in " ".join(g.paths()))
        assert group.entries[0][1].relpath == "pics/holiday.jpg"


class TestAcrossDrives:
    def test_detects_a_file_moved_to_a_different_folder(self, trees, index):
        left, right = trees
        dupes = run(left, right, index)

        paths = flatten(dupes.cross)
        assert "left:media/clip.mp4" in paths
        assert "right:media/archive/clip.mp4" in paths

    def test_a_file_at_the_same_path_on_both_drives_is_not_a_duplicate(self, trees, index):
        """That is just a synced file; calling it a duplicate would be nonsense."""
        left, right = trees
        dupes = run(left, right, index)

        assert not any("docs/same.txt" in " ".join(g.paths()) for g in dupes.cross)


class TestFunnel:
    def test_min_size_suppresses_trivial_matches(self, trees, index):
        left, right = trees
        big = run(left, right, index, min_size=1_000_000)

        assert not big.within_left
        assert not big.cross

    def test_scope_limits_the_work(self, trees, index):
        left, right = trees
        left_only = run(left, right, index, scope="left")

        assert left_only.within_left
        assert not left_only.within_right

    def test_a_file_with_a_unique_size_is_never_opened(self, trees, index):
        """Stage one rejects on size alone, so those files must cost no I/O at all."""
        left, right = trees
        left_scan = scan(str(left))
        right_scan = scan(str(right))

        sizes = Counter(
            record.size
            for record in list(left_scan.files.values()) + list(right_scan.files.values())
        )
        unique = [record for record in left_scan.files.values() if sizes[record.size] == 1]
        assert unique, "fixture should contain files with unique sizes"

        find_duplicates(left_scan, right_scan, index, min_size=1)

        for record in unique:
            assert record.quick_hash == "", f"{record.relpath} was read despite a unique size"
            assert record.full_hash == ""
