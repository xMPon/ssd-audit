"""Interactive setup: the choices must be explicit and hard to get wrong."""

from __future__ import annotations

import builtins

import pytest

from ssdaudit.config import IgnoreRules
from ssdaudit.volumes import Volume
from ssdaudit.wizard import choose_drive, choose_verify, confirm_plan, require_terminal, run_wizard


@pytest.fixture
def fake_volumes():
    return [
        Volume("G:\\", "5E71-B5D8", "SD Malaysia", "exFAT", "fixed",
               total_bytes=1_000_204_886_016, free_bytes=554_500_000_000),
        Volume("D:\\", "3C10-3542", "SanDisk 238", "exFAT", "removable",
               total_bytes=255_800_000_000, free_bytes=63_100_000_000),
    ]


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted replies to input(), and report what was consumed."""
    def feed(*responses):
        queue = list(responses)
        monkeypatch.setattr(builtins, "input", lambda *_: queue.pop(0))
        return queue
    return feed


@pytest.fixture(autouse=True)
def pretend_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


class TestDriveChoice:
    def test_a_number_selects_that_drive(self, fake_volumes, answers):
        answers("1")
        assert choose_drive("FIRST", fake_volumes) == "G:\\"

    def test_the_second_drive_cannot_repeat_the_first(self, fake_volumes, answers, capsys):
        """Silently comparing a drive with itself would waste a long scan."""
        answers("1", "2")
        assert choose_drive("SECOND", fake_volumes, exclude="G:\\") == "D:\\"
        assert "already the other side" in capsys.readouterr().out

    def test_a_path_can_be_typed_instead(self, fake_volumes, answers, tmp_path):
        """Comparing two folders on one drive is legitimate and shouldn't need another command."""
        answers(str(tmp_path))
        assert choose_drive("FIRST", fake_volumes) == str(tmp_path)

    def test_a_bad_path_is_rejected_and_reasked(self, fake_volumes, answers, tmp_path, capsys):
        answers("Z:\\does-not-exist", str(tmp_path))
        assert choose_drive("FIRST", fake_volumes) == str(tmp_path)
        assert "Not a folder" in capsys.readouterr().out

    def test_out_of_range_numbers_are_rejected(self, fake_volumes, answers, capsys):
        answers("9", "2")
        assert choose_drive("FIRST", fake_volumes) == "D:\\"
        assert "between 1 and 2" in capsys.readouterr().out

    def test_quitting_raises(self, fake_volumes, answers):
        answers("q")
        with pytest.raises(KeyboardInterrupt):
            choose_drive("FIRST", fake_volumes)


class TestVerifyChoice:
    def test_default_is_smart(self, answers):
        answers("")
        assert choose_verify() == "smart"

    def test_each_level_is_selectable(self, answers):
        for reply, expected in (("1", "metadata"), ("2", "smart"), ("3", "full")):
            answers(reply)
            assert choose_verify() == expected

    def test_a_level_can_be_typed_by_name(self, answers):
        answers("full")
        assert choose_verify() == "full"


class TestConfirmation:
    def test_the_plan_is_shown_before_anything_is_read(self, answers, capsys):
        answers("y")
        confirm_plan("G:\\", "D:\\", ["Work"], "smart", True)

        out = capsys.readouterr().out
        assert "G:\\" in out and "D:\\" in out
        assert "Work" in out
        assert "smart" in out
        assert "read only" in out.lower()

    def test_declining_returns_false(self, answers):
        answers("n")
        assert confirm_plan("G:\\", "D:\\", [], "smart", True) is False

    def test_enter_accepts(self, answers):
        answers("")
        assert confirm_plan("G:\\", "D:\\", [], "smart", True) is True


class TestFullFlow:
    def test_a_complete_run_collects_every_choice(self, monkeypatch, fake_volumes, answers):
        monkeypatch.setattr("ssdaudit.wizard.list_volumes", lambda: fake_volumes)
        # left, right, whole-drive, verify=full, dupes=no, confirm
        answers("1", "2", "1", "3", "n", "y")

        plan = run_wizard(IgnoreRules())

        assert plan == {"left": "G:\\", "right": "D:\\", "folders": [],
                        "verify": "full", "dupes": False}

    def test_backing_out_at_the_end_reads_nothing(self, monkeypatch, fake_volumes, answers):
        monkeypatch.setattr("ssdaudit.wizard.list_volumes", lambda: fake_volumes)
        answers("1", "2", "1", "2", "y", "n")

        assert run_wizard(IgnoreRules()) is None


class TestNonInteractive:
    def test_it_refuses_without_a_terminal_and_says_what_to_use(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(RuntimeError) as error:
            require_terminal()

        assert "--left" in str(error.value)
