"""CLI behaviour: exit codes, drive resolution, and output robustness."""

from __future__ import annotations

import os

import pytest

from ssdaudit.cli import (
    EXIT_DIFFERENCES,
    EXIT_ERROR,
    EXIT_IN_SYNC,
    AuditError,
    main,
    resolve_side,
)
from ssdaudit.config import DriveRef


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate profiles, cache and audit history from the real user directory."""
    monkeypatch.setenv("SSDAUDIT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestExitCodes:
    def test_differences_exit_with_one(self, trees, home, capsys):
        left, right = trees
        assert main(["compare", "--left", str(left), "--right", str(right)]) == EXIT_DIFFERENCES

    def test_matching_drives_exit_with_zero(self, tmp_path, home):
        left = tmp_path / "a"
        right = tmp_path / "b"
        for root in (left, right):
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "file.txt").write_bytes(b"identical")
            os.utime(root / "docs" / "file.txt", (1_700_000_000, 1_700_000_000))

        assert main(["compare", "--left", str(left), "--right", str(right)]) == EXIT_IN_SYNC

    def test_a_bad_path_exits_with_two(self, home, capsys):
        assert main(["compare", "--left", "Z:\\nope", "--right", "Z:\\nope2"]) == EXIT_ERROR
        assert "not a directory" in capsys.readouterr().err

    def test_missing_arguments_are_explained(self, home, capsys):
        assert main(["compare"]) == EXIT_ERROR
        assert "--profile" in capsys.readouterr().err


class TestDriveResolution:
    def test_an_unattached_drive_refuses_rather_than_guessing(self):
        """Auditing whatever now holds a letter would be worse than failing."""
        ref = DriveRef(serial="DEAD-BEEF", label="Nonexistent SSD", subpath="Photos")

        with pytest.raises(AuditError) as error:
            resolve_side(ref, "Left")

        message = str(error.value)
        assert "not attached" in message
        assert "DEAD-BEEF" in message

    def test_a_path_only_reference_still_works(self, tmp_path):
        """Comparing two folders on one drive has no distinct serial to pin to."""
        ref = DriveRef(fallback_path=str(tmp_path))
        assert resolve_side(ref, "Left") == str(tmp_path)

    def test_a_reference_with_nothing_usable_is_rejected(self):
        with pytest.raises(AuditError):
            resolve_side(DriveRef(), "Left")


class TestOutputRobustness:
    def test_filenames_the_console_cannot_encode_do_not_crash(self, tmp_path, home, capsys):
        """A Polish filename on a cp1252 console must not abort the audit."""
        left = tmp_path / "a"
        right = tmp_path / "b"
        (left / "Zdjęcia").mkdir(parents=True)
        (right / "Zdjęcia").mkdir(parents=True)
        (left / "Zdjęcia" / "Zażółć.jpg").write_bytes(b"x" * 100)

        assert main(["compare", "--left", str(left), "--right", str(right)]) == EXIT_DIFFERENCES
        assert "missing from the RIGHT" in capsys.readouterr().out


class TestReporting:
    def test_a_run_writes_every_expected_artefact(self, trees, home, tmp_path):
        left, right = trees
        out = tmp_path / "runs"
        main(["compare", "--left", str(left), "--right", str(right), "--out", str(out)])

        run = next(path for path in out.iterdir() if path.is_dir())
        produced = {item.name for item in run.iterdir()}
        assert {
            "summary.md",
            "report.html",
            "diff.json",
            "manifest-left.jsonl",
            "manifest-right.jsonl",
            "sync-left-to-right.cmd",
            "sync-right-to-left.cmd",
            "review-duplicates.cmd",
            "conflicts.txt",
        } <= produced

    def test_the_html_report_is_self_contained(self, trees, home, tmp_path):
        """Artefacts must open offline, with no external requests."""
        left, right = trees
        out = tmp_path / "runs"
        main(["compare", "--left", str(left), "--right", str(right), "--out", str(out)])

        run = next(path for path in out.iterdir() if path.is_dir())
        html = (run / "report.html").read_text(encoding="utf-8")

        assert "<title>" in html
        for remote in ("http://", "https://", "src=\"//"):
            assert remote not in html

    def test_history_records_the_run(self, trees, home, tmp_path):
        from ssdaudit.history import load_history

        left, right = trees
        main(["compare", "--left", str(left), "--right", str(right)])

        entries = load_history()
        assert len(entries) == 1
        assert entries[0]["counts"]["only_left"] > 0
