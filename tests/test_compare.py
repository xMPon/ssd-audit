"""Classification behaviour -- the correctness core of the tool."""

from __future__ import annotations

import pytest

from ssdaudit import hashing
from ssdaudit.compare import classify_mtime, compare
from ssdaudit.scanner import scan


def run(left, right, index, verify="smart"):
    return compare(scan(str(left)), scan(str(right)), index, verify=verify)


def relpaths(records):
    return {record.relpath for record in records}


def pairpaths(pairs):
    return {pair.relpath for pair in pairs}


class TestMtimeClassification:
    def test_within_exfat_granularity_is_same(self):
        assert classify_mtime(0, 1_500_000_000) == "same"

    def test_whole_hour_drift_is_a_dst_artefact(self):
        assert classify_mtime(0, 3_600_000_000_000) == "dst"

    def test_arbitrary_drift_is_a_real_difference(self):
        assert classify_mtime(0, 500_000_000_000) == "differs"


class TestMissingFiles:
    def test_finds_files_present_on_only_one_side(self, trees, index):
        left, right = trees
        result = run(left, right, index)

        assert "docs/only-left.txt" in relpaths(result.only_left)
        assert "docs/only-right.txt" in relpaths(result.only_right)

    def test_a_one_sided_file_is_never_a_deletion_candidate(self, trees, index):
        """Peer model: presence on one drive only is a gap, not staleness."""
        left, right = trees
        result = run(left, right, index)

        assert not hasattr(result, "stale")
        assert result.only_left and result.only_right


class TestConflicts:
    def test_different_size_is_a_conflict(self, trees, index):
        left, right = trees
        result = run(left, right, index)
        assert "docs/size.txt" in pairpaths(result.conflicts)

    def test_same_size_different_content_is_caught(self, trees, index):
        """The case metadata alone cannot see -- this is why smart mode hashes."""
        left, right = trees
        result = run(left, right, index)
        assert "docs/silent.bin" in pairpaths(result.conflicts)

    def test_metadata_mode_cannot_settle_it_and_says_so(self, trees, index):
        left, right = trees
        result = run(left, right, index, verify="metadata")

        assert "docs/silent.bin" not in pairpaths(result.conflicts)
        assert "docs/silent.bin" in pairpaths(result.unverified)


class TestFalsePositives:
    def test_sub_second_timestamp_drift_is_not_a_difference(self, trees, index):
        left, right = trees
        result = run(left, right, index)
        assert "docs/granularity.txt" in pairpaths(result.identical)

    def test_touched_but_unmodified_file_resolves_to_identical(self, trees, index):
        left, right = trees
        result = run(left, right, index)

        assert "docs/touched.txt" in pairpaths(result.identical)
        assert "docs/touched.txt" not in pairpaths(result.conflicts)

    def test_dst_drift_is_reported_separately_not_as_a_conflict(self, trees, index):
        left, right = trees
        result = run(left, right, index)

        assert "docs/dst.txt" in pairpaths(result.dst_artifacts)
        assert "docs/dst.txt" not in pairpaths(result.conflicts)

    def test_unicode_form_mismatch_matches_rather_than_duplicating(self, unicode_trees, index):
        """NFD vs NFC names are the same file; treating them otherwise copies it twice."""
        left, right = unicode_trees
        result = run(left, right, index)

        assert not result.only_left
        assert not result.only_right


class TestScanning:
    def test_system_directories_are_pruned(self, trees, index):
        left, right = trees
        result = run(left, right, index)
        assert not any("System Volume Information" in path for path in relpaths(result.only_left))

    def test_cruft_is_counted_separately_not_compared(self, trees, index):
        left, right = trees
        result = run(left, right, index)

        cruft = {record.relpath for record in result.left.cruft}
        assert "docs/.DS_Store" in cruft
        assert "docs/._resource" in cruft
        assert "docs/.DS_Store" not in relpaths(result.only_left)
        assert result.counts()["bytes_cruft"] > 0


class TestCachePerformance:
    def test_second_run_reads_nothing_from_disk(self, trees, index):
        """The cache is the whole performance argument, so it is measured."""
        left, right = trees

        run(left, right, index)

        hashing.counter().reset()
        run(left, right, index)

        assert hashing.counter().files == 0

    def test_cache_is_invalidated_when_a_file_changes(self, trees, index):
        left, right = trees
        run(left, right, index)

        target = left / "docs/touched.txt"
        target.write_bytes(b"now different!!")

        hashing.counter().reset()
        result = run(left, right, index)

        assert hashing.counter().files > 0
        assert "docs/touched.txt" in pairpaths(result.conflicts)
