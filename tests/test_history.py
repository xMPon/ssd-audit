"""The audit ledger and run-to-run comparison."""

from __future__ import annotations

import json

from ssdaudit.history import append_run, diff_runs, format_diff, load_history


def make_run(run_id, only_left=(), only_right=(), conflicts=(), volume_left="V1", volume_right="V2"):
    return {
        "meta": {"run_id": run_id},
        "counts": {
            "only_left": len(only_left),
            "only_right": len(only_right),
            "conflicts": len(conflicts),
        },
        "roots": {
            "left": {"path": "L:\\", "volume": volume_left},
            "right": {"path": "R:\\", "volume": volume_right},
        },
        "only_left": [{"relpath": p} for p in only_left],
        "only_right": [{"relpath": p} for p in only_right],
        "conflicts": [{"relpath": p} for p in conflicts],
    }


class TestLedger:
    def test_runs_append_without_overwriting(self, tmp_path):
        path = tmp_path / "history.jsonl"
        append_run({"run_id": "a", "counts": {}}, path)
        append_run({"run_id": "b", "counts": {}}, path)

        entries = load_history(path=path)
        assert [entry["run_id"] for entry in entries] == ["a", "b"]

    def test_a_corrupt_line_does_not_destroy_the_history(self, tmp_path):
        """A run interrupted mid-write must not make every earlier audit unreadable."""
        path = tmp_path / "history.jsonl"
        append_run({"run_id": "a", "counts": {}}, path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"run_id": "truncated"\n')
        append_run({"run_id": "c", "counts": {}}, path)

        entries = load_history(path=path)
        assert [entry["run_id"] for entry in entries] == ["a", "c"]

    def test_limit_returns_the_most_recent_runs(self, tmp_path):
        path = tmp_path / "history.jsonl"
        for name in "abcde":
            append_run({"run_id": name, "counts": {}}, path)

        assert [e["run_id"] for e in load_history(limit=2, path=path)] == ["d", "e"]


class TestRunComparison:
    def test_reports_which_gaps_were_closed(self, tmp_path):
        older = make_run("r1", only_left=["a.txt", "b.txt", "c.txt"])
        newer = make_run("r2", only_left=["c.txt"])

        diff = diff_runs(older, newer)

        assert diff["only_left"]["before"] == 3
        assert diff["only_left"]["after"] == 1
        assert diff["only_left"]["resolved"] == ["a.txt", "b.txt"]
        assert diff["only_left"]["appeared"] == []

    def test_reports_newly_appeared_differences(self, tmp_path):
        older = make_run("r1", conflicts=["x.dat"])
        newer = make_run("r2", conflicts=["x.dat", "y.dat"])

        diff = diff_runs(older, newer)
        assert diff["conflicts"]["appeared"] == ["y.dat"]

    def test_warns_when_the_runs_cover_different_drives(self):
        """Comparing audits of unrelated drives produces meaningless numbers."""
        older = make_run("r1", volume_left="V1")
        newer = make_run("r2", volume_left="DIFFERENT")

        diff = diff_runs(older, newer)
        assert diff["same_drives"] is False
        assert "different volumes" in format_diff(diff)

    def test_rendered_output_names_both_runs(self):
        diff = diff_runs(make_run("r1", only_left=["a"]), make_run("r2"))
        text = format_diff(diff)

        assert "r1 -> r2" in text
        assert "resolved  a" in text
