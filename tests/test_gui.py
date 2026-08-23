"""Desktop interface.

The behaviour worth protecting here is that the app never chooses drives for
you: auditing the wrong pair is the expensive mistake, and a convenient default
is how it happens.
"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


pytestmark = pytest.mark.skipif(
    not _display_available(), reason="no display available for tkinter"
)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("SSDAUDIT_HOME", str(tmp_path / "home"))
    from ssdaudit.gui import AuditApp

    application = AuditApp()
    application.update()
    yield application
    application.destroy()


class TestNoAutomaticSelection:
    def test_neither_side_is_preselected(self, app):
        assert app.left.selection() == ""
        assert app.right.selection() == ""

    def test_start_is_disabled_until_both_sides_are_chosen(self, app, tmp_path):
        assert str(app.start_button["state"]) == "disabled"

        app.left.path.set(str(tmp_path))
        app._validate()
        assert str(app.start_button["state"]) == "disabled", "one side is not enough"

    def test_refreshing_drives_clears_any_selection(self, app, tmp_path):
        app.left.path.set(str(tmp_path))
        app._validate()

        app.refresh_drives()
        assert app.left.selection() == ""
        assert str(app.start_button["state"]) == "disabled"


class TestValidation:
    def test_the_same_location_on_both_sides_is_refused(self, app, tmp_path):
        app.left.path.set(str(tmp_path))
        app.right.path.set(str(tmp_path))
        app._validate()

        assert str(app.start_button["state"]) == "disabled"
        assert "same location" in app.status["text"]

    def test_two_different_folders_enable_start(self, app, tmp_path):
        left = tmp_path / "one"
        right = tmp_path / "two"
        left.mkdir()
        right.mkdir()

        app.left.path.set(str(left))
        app.right.path.set(str(right))
        app._validate()

        assert str(app.start_button["state"]) == "normal"

    def test_case_differences_do_not_defeat_the_same_location_check(self, app, tmp_path):
        """Windows paths are case-insensitive, so casing must not smuggle a match through."""
        app.left.path.set(str(tmp_path).upper())
        app.right.path.set(str(tmp_path).lower())
        app._validate()

        assert str(app.start_button["state"]) == "disabled"


class TestRun:
    def test_a_full_comparison_reports_results_in_the_window(self, app, trees):
        left, right = trees
        app.left.path.set(str(left))
        app.right.path.set(str(right))
        app._validate()

        app.start()
        deadline = time.time() + 60
        while time.time() < deadline and not app.last_run_dir:
            app.update()
            time.sleep(0.02)

        assert app.last_run_dir, "the run did not finish in time"
        text = app.log.get("1.0", "end")
        assert "missing from the RIGHT side" in text
        assert "Nothing will be modified" in text
        assert str(app.open_report["state"]) == "normal"

    def test_a_failure_is_surfaced_rather_than_swallowed(self, app, monkeypatch, tmp_path):
        left = tmp_path / "one"
        right = tmp_path / "two"
        left.mkdir()
        right.mkdir()

        def explode(*_args, **_kwargs):
            raise OSError("drive disconnected")

        monkeypatch.setattr("ssdaudit.gui.scan", explode)
        monkeypatch.setattr("ssdaudit.gui.messagebox.showerror", lambda *a, **k: None)

        app.left.path.set(str(left))
        app.right.path.set(str(right))
        app._validate()
        app.start()

        deadline = time.time() + 30
        while time.time() < deadline and "ERROR" not in app.log.get("1.0", "end"):
            app.update()
            time.sleep(0.02)

        assert "drive disconnected" in app.log.get("1.0", "end")
        assert str(app.start_button["state"]) == "normal", "must be retryable after a failure"
