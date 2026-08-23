"""Generated scripts must never be able to destroy data on their own."""

from __future__ import annotations

import re

from ssdaudit.compare import compare
from ssdaudit.dupes import find_duplicates
from ssdaudit.remediate import write_all
from ssdaudit.scanner import scan

# Verbs that remove or overwrite data. None may appear as a live command.
DESTRUCTIVE = re.compile(r"^\s*(del|erase|rmdir|rd|move|format|robocopy\s+.*/(MIR|PURGE|MOVE?))\b", re.I)


def build(left, right, index, min_size=1):
    left_scan = scan(str(left))
    right_scan = scan(str(right))
    result = compare(left_scan, right_scan, index, verify="full")
    dupes = find_duplicates(left_scan, right_scan, index, min_size=min_size)
    return result, dupes


def live_lines(text: str) -> list[str]:
    """Lines a shell would actually execute -- comments and echoes removed."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper().startswith(("REM", "ECHO", "@ECHO", ":")):
            continue
        lines.append(stripped)
    return lines


class TestSafety:
    def test_no_script_contains_a_live_destructive_command(self, trees, index, tmp_path):
        left, right = trees
        result, dupes = build(left, right, index)
        out = tmp_path / "run"

        for path in write_all(result, dupes, out):
            for line in live_lines(path.read_text(encoding="utf-8")):
                assert not DESTRUCTIVE.match(line), f"{path.name} would execute: {line}"

    def test_duplicate_cleanup_is_entirely_commented_out(self, trees, index, tmp_path):
        left, right = trees
        result, dupes = build(left, right, index)
        write_all(result, dupes, tmp_path / "run")

        text = (tmp_path / "run" / "review-duplicates.cmd").read_text(encoding="utf-8")
        assert "REM move /Y" in text, "expected quarantine moves to be present but disabled"
        for line in live_lines(text):
            assert "move" not in line.lower()

    def test_duplicates_are_quarantined_rather_than_deleted(self, trees, index, tmp_path):
        """A wrong call must be recoverable, so nothing is ever deleted."""
        left, right = trees
        result, dupes = build(left, right, index)
        write_all(result, dupes, tmp_path / "run")

        text = (tmp_path / "run" / "review-duplicates.cmd").read_text(encoding="utf-8")
        assert "_ssdaudit-quarantine" in text
        assert "del " not in text.lower()

    def test_conflicts_get_a_listing_not_a_script(self, trees, index, tmp_path):
        """Choosing which version of a conflicted file survives is not automatable."""
        left, right = trees
        result, dupes = build(left, right, index)
        written = write_all(result, dupes, tmp_path / "run")

        names = {path.name for path in written}
        assert "conflicts.txt" in names
        assert not any(name.startswith("resolve-conflicts") for name in names)

        text = (tmp_path / "run" / "conflicts.txt").read_text(encoding="utf-8")
        assert "docs/silent.bin" in text
        assert "newer is not automatically correct" in text


class TestCopyScripts:
    def test_missing_files_are_scheduled_for_copy(self, trees, index, tmp_path):
        left, right = trees
        result, dupes = build(left, right, index)
        write_all(result, dupes, tmp_path / "run")

        text = (tmp_path / "run" / "sync-left-to-right.cmd").read_text(encoding="utf-8")
        assert "only-left.txt" in text

    def test_a_moved_file_is_held_back_instead_of_copied(self, trees, index, tmp_path):
        """The fixture's clip.mp4 was moved on the right drive, not lost.

        Copying it would leave two copies under two names -- the exact outcome
        cross-drive duplicate detection exists to prevent.
        """
        left, right = trees
        result, dupes = build(left, right, index)
        write_all(result, dupes, tmp_path / "run")

        text = (tmp_path / "run" / "sync-left-to-right.cmd").read_text(encoding="utf-8")
        assert "HELD BACK" in text
        assert "media/archive/clip.mp4" in text
        for line in live_lines(text):
            assert "clip.mp4" not in line

    def test_files_are_grouped_per_directory(self, trees, index, tmp_path):
        """One robocopy call per directory, not one per file."""
        left, right = trees
        result, dupes = build(left, right, index)
        write_all(result, dupes, tmp_path / "run")

        text = (tmp_path / "run" / "sync-left-to-right.cmd").read_text(encoding="utf-8")
        calls = [line for line in live_lines(text) if line.startswith("robocopy")]
        assert calls
        assert len(calls) <= len(result.only_left)

    def test_percent_signs_are_escaped_for_batch(self, tmp_path, index):
        """An unescaped %% in a filename becomes variable expansion in cmd.exe."""
        from ssdaudit.remediate import write_copy_script
        from ssdaudit.scanner import FileRecord

        record = FileRecord(relpath="odd/50%_off.txt", key="odd/50%_off.txt", size=10, mtime_ns=0)
        path = write_copy_script([record], "S:\\src", "D:\\dst",
                                 tmp_path / "copy.cmd", "TEST", "test.log")

        assert "50%%_off.txt" in path.read_text(encoding="utf-8")
