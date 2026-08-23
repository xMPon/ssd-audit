"""Audit history.

Every run appends one line to ``history.jsonl`` and keeps its full output in a
timestamped folder. That turns a pile of one-off reports into something that
answers the question you actually have after the second audit: *are the drives
converging, or am I drifting further apart?*

``diff_runs`` compares two runs' stored results and reports which specific gaps
closed, which appeared, and which conflicts are new.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import data_dir


def audits_dir() -> Path:
    """Where runs are stored.

    Deliberately outside the repository: audit output contains the full file and
    folder names from your drives, and this repo is public.
    """
    return data_dir() / "audits"


def history_path() -> Path:
    return audits_dir() / "history.jsonl"


def append_run(entry: dict, path: Path | None = None) -> Path:
    target = path or history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return target


def load_history(limit: int | None = None, path: Path | None = None) -> list[dict]:
    target = path or history_path()
    if not target.exists():
        return []
    entries = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partially-written line must not break the whole history
    return entries[-limit:] if limit else entries


def run_directory(run_id: str, base: Path | None = None) -> Path:
    return (base or audits_dir()) / run_id


def load_run(run_id: str, base: Path | None = None) -> dict:
    path = run_directory(run_id, base) / "diff.json"
    if not path.exists():
        raise FileNotFoundError(f"no audit found for run id {run_id!r} (looked in {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_run_id(reference: str, base: Path | None = None) -> str:
    """Accept a full run id, or ``latest`` / ``-1`` / ``-2`` counting backwards."""
    entries = load_history()
    ids = [entry["run_id"] for entry in entries if "run_id" in entry]

    if reference == "latest":
        if not ids:
            raise ValueError("no runs recorded yet")
        return ids[-1]

    if reference.startswith("-") and reference[1:].isdigit():
        offset = int(reference[1:])
        if offset > len(ids):
            raise ValueError(f"only {len(ids)} runs recorded; cannot go back {offset}")
        return ids[-offset]

    return reference


def _paths(entries: list[dict]) -> set[str]:
    return {entry["relpath"] for entry in entries}


def diff_runs(older: dict, newer: dict) -> dict:
    """What changed between two audits of the same drive pair."""
    result = {
        "from": older.get("meta", {}).get("run_id"),
        "to": newer.get("meta", {}).get("run_id"),
        "same_drives": (
            older.get("roots", {}).get("left", {}).get("volume")
            == newer.get("roots", {}).get("left", {}).get("volume")
            and older.get("roots", {}).get("right", {}).get("volume")
            == newer.get("roots", {}).get("right", {}).get("volume")
        ),
    }

    for bucket in ("only_left", "only_right", "conflicts"):
        before = _paths(older.get(bucket, []))
        after = _paths(newer.get(bucket, []))
        result[bucket] = {
            "before": len(before),
            "after": len(after),
            "resolved": sorted(before - after),
            "appeared": sorted(after - before),
        }

    older_counts = older.get("counts", {})
    newer_counts = newer.get("counts", {})
    result["counts_delta"] = {
        key: newer_counts.get(key, 0) - older_counts.get(key, 0)
        for key in sorted(set(older_counts) | set(newer_counts))
        if newer_counts.get(key, 0) != older_counts.get(key, 0)
    }
    return result


def format_diff(diff: dict) -> str:
    """Render :func:`diff_runs` for the terminal."""
    lines = [f"Comparing {diff['from']} -> {diff['to']}", ""]

    if not diff["same_drives"]:
        lines += ["  ! These runs cover different volumes; the comparison may be meaningless.", ""]

    labels = {
        "only_left": "Missing from right",
        "only_right": "Missing from left",
        "conflicts": "Content conflicts",
    }
    for bucket, label in labels.items():
        section = diff[bucket]
        delta = section["after"] - section["before"]
        arrow = "no change" if delta == 0 else (f"+{delta}" if delta > 0 else str(delta))
        lines.append(f"  {label}: {section['before']} -> {section['after']}  ({arrow})")
        for path in section["resolved"][:10]:
            lines.append(f"      resolved  {path}")
        if len(section["resolved"]) > 10:
            lines.append(f"      … and {len(section['resolved']) - 10} more resolved")
        for path in section["appeared"][:10]:
            lines.append(f"      appeared  {path}")
        if len(section["appeared"]) > 10:
            lines.append(f"      … and {len(section['appeared']) - 10} more appeared")
        lines.append("")

    return "\n".join(lines)
