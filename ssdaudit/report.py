"""Audit output.

Each run produces four things, aimed at different readers:

* ``summary.md``  -- what you actually read
* ``report.html`` -- browsable, filterable, for working through long lists
* ``diff.json``   -- machine-readable, and the input to ``compare-runs``
* ``manifest-*.jsonl`` -- every file seen, so a later run can diff against it
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .compare import CompareResult
from .dupes import DupeResult

# Long lists are truncated in the human-readable outputs; diff.json always
# carries the complete set.
PREVIEW_LIMIT = 200


def format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_time(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


def new_run_id(profile: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{stamp}_{profile}"


def write_audit(
    result: CompareResult,
    dupes: DupeResult | None,
    directory: Path,
    meta: dict,
) -> Path:
    """Write the full output set for one run into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    dupes = dupes or DupeResult()

    _write_manifest(result.left, directory / "manifest-left.jsonl")
    _write_manifest(result.right, directory / "manifest-right.jsonl")

    payload = _build_payload(result, dupes, meta)
    (directory / "diff.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "summary.md").write_text(
        _render_markdown(result, dupes, meta), encoding="utf-8"
    )
    (directory / "report.html").write_text(
        _render_html(result, dupes, meta), encoding="utf-8"
    )
    return directory


def _write_manifest(scan, path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in sorted(scan.files.values(), key=lambda item: item.relpath):
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def _build_payload(result: CompareResult, dupes: DupeResult, meta: dict) -> dict:
    return {
        "meta": meta,
        "counts": {**result.counts(), **dupes.counts()},
        "roots": {
            "left": {"path": result.left.root, "volume": result.left.volume},
            "right": {"path": result.right.root, "volume": result.right.volume},
        },
        "only_left": [record.to_dict() for record in result.only_left],
        "only_right": [record.to_dict() for record in result.only_right],
        "conflicts": [pair.to_dict() for pair in result.conflicts],
        "dst_artifacts": [pair.to_dict() for pair in result.dst_artifacts],
        "unverified": [pair.to_dict() for pair in result.unverified],
        "name_case_differs": [pair.to_dict() for pair in result.name_case_differs],
        "unicode_differs": [pair.to_dict() for pair in result.unicode_differs],
        "case_collisions": {
            "left": result.left.case_collisions,
            "right": result.right.case_collisions,
        },
        "cruft": {
            "left": [record.to_dict() for record in result.left.cruft],
            "right": [record.to_dict() for record in result.right.cruft],
        },
        "duplicates": {
            "within_left": [group.to_dict() for group in dupes.within_left],
            "within_right": [group.to_dict() for group in dupes.within_right],
            "cross": [group.to_dict() for group in dupes.cross],
        },
        "errors": {
            "left_scan": result.left.errors,
            "right_scan": result.right.errors,
            "hashing": result.hash_errors,
            "duplicates": dupes.errors,
        },
    }


def _render_markdown(result: CompareResult, dupes: DupeResult, meta: dict) -> str:
    counts = result.counts()
    left_name = meta.get("left_label") or result.left.root
    right_name = meta.get("right_label") or result.right.root

    lines = [
        f"# Drive audit — {meta.get('profile', 'ad-hoc')}",
        "",
        f"**Run:** {meta.get('run_id')}  ",
        f"**When:** {meta.get('started')}  ",
        f"**Verify mode:** `{result.verify}`  ",
        f"**Duration:** {meta.get('duration_s', 0):.1f}s",
        "",
        "| | Left | Right |",
        "|---|---|---|",
        f"| Path | `{result.left.root}` | `{result.right.root}` |",
        f"| Label | {left_name} | {right_name} |",
        f"| Volume serial | `{result.left.volume}` | `{result.right.volume}` |",
        f"| Files compared | {counts['left_files']:,} | {counts['right_files']:,} |",
        "",
        "## Verdict",
        "",
    ]

    if result.in_sync:
        lines += ["✅ **Both drives hold the same files.** Nothing to reconcile.", ""]
    else:
        lines += [
            f"- **{counts['only_left']:,} files** exist only on the left "
            f"({format_bytes(result.bytes_only_left)}) — missing from the right",
            f"- **{counts['only_right']:,} files** exist only on the right "
            f"({format_bytes(result.bytes_only_right)}) — missing from the left",
            f"- **{counts['conflicts']:,} files** share a path but differ in content "
            "— these need a decision from you",
            "",
        ]

    lines += [
        "## Everything found",
        "",
        "| Category | Count | Size |",
        "|---|---:|---:|",
        f"| Identical on both drives | {counts['identical']:,} | |",
        f"| Only on left | {counts['only_left']:,} | {format_bytes(result.bytes_only_left)} |",
        f"| Only on right | {counts['only_right']:,} | {format_bytes(result.bytes_only_right)} |",
        f"| Content conflicts | {counts['conflicts']:,} | |",
        f"| Timestamp-only (DST artefact) | {counts['dst_artifacts']:,} | |",
        f"| Unverified (metadata mode) | {counts['unverified']:,} | |",
        f"| Filename case differs | {counts['name_case_differs']:,} | |",
        f"| Filename Unicode form differs | {counts['unicode_differs']:,} | |",
        f"| Case collisions | {counts['case_collisions']:,} | |",
        f"| Cruft (Thumbs.db, .DS_Store, ._*) | {counts['cruft_files']:,} | "
        f"{format_bytes(counts['bytes_cruft'])} |",
        f"| Duplicate groups on left | {len(dupes.within_left):,} | "
        f"{format_bytes(sum(g.wasted_bytes for g in dupes.within_left))} reclaimable |",
        f"| Duplicate groups on right | {len(dupes.within_right):,} | "
        f"{format_bytes(sum(g.wasted_bytes for g in dupes.within_right))} reclaimable |",
        f"| Same file, different path across drives | {len(dupes.cross):,} | |",
        f"| Scan errors | {counts['scan_errors']:,} | |",
        "",
    ]

    lines += _md_section(
        "Missing from the right drive",
        [f"`{r.relpath}` — {format_bytes(r.size)}" for r in result.only_left],
    )
    lines += _md_section(
        "Missing from the left drive",
        [f"`{r.relpath}` — {format_bytes(r.size)}" for r in result.only_right],
    )
    lines += _md_section(
        "Content conflicts — same path, different content",
        [
            f"`{p.relpath}` — left {format_bytes(p.left.size)} @ {format_time(p.left.mtime_ns)}, "
            f"right {format_bytes(p.right.size)} @ {format_time(p.right.mtime_ns)} ({p.reason})"
            for p in result.conflicts
        ],
        note="**Never resolved automatically.** See `docs/RECONCILIATION.md`.",
    )

    if dupes.cross:
        lines += _md_section(
            "Same content, different path on each drive",
            [
                f"{format_bytes(g.size)} — " + " ↔ ".join(g.paths())
                for g in dupes.cross
            ],
            note="These are *not* missing files. Copying them would create a third copy.",
        )

    for title, groups in (
        ("Duplicates within the left drive", dupes.within_left),
        ("Duplicates within the right drive", dupes.within_right),
    ):
        if groups:
            lines += _md_section(
                title,
                [
                    f"{format_bytes(g.wasted_bytes)} reclaimable — " + " · ".join(g.paths())
                    for g in groups
                ],
            )

    errors = result.left.errors + result.right.errors + result.hash_errors
    if errors:
        lines += _md_section("Errors", [f"`{e}`" for e in errors])

    return "\n".join(lines) + "\n"


def _md_section(title: str, items: list[str], note: str = "") -> list[str]:
    if not items:
        return []
    lines = [f"## {title} ({len(items):,})", ""]
    if note:
        lines += [note, ""]
    for item in items[:PREVIEW_LIMIT]:
        lines.append(f"- {item}")
    if len(items) > PREVIEW_LIMIT:
        lines.append(f"- _…and {len(items) - PREVIEW_LIMIT:,} more — see `diff.json`_")
    lines.append("")
    return lines


def _render_html(result: CompareResult, dupes: DupeResult, meta: dict) -> str:
    counts = result.counts()

    tabs = [
        ("only-left", f"Missing from right ({counts['only_left']:,})",
         _rows_single(result.only_left)),
        ("only-right", f"Missing from left ({counts['only_right']:,})",
         _rows_single(result.only_right)),
        ("conflicts", f"Conflicts ({counts['conflicts']:,})",
         _rows_pairs(result.conflicts)),
        ("dupes-left", f"Duplicates left ({len(dupes.within_left):,})",
         _rows_groups(dupes.within_left)),
        ("dupes-right", f"Duplicates right ({len(dupes.within_right):,})",
         _rows_groups(dupes.within_right)),
        ("cross", f"Moved/renamed ({len(dupes.cross):,})",
         _rows_groups(dupes.cross)),
        ("cruft", f"Cruft ({counts['cruft_files']:,})",
         _rows_single(result.left.cruft + result.right.cruft)),
    ]

    buttons = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-target="{key}">{html.escape(label)}</button>'
        for i, (key, label, _) in enumerate(tabs)
    )
    panels = "".join(
        f'<div class="panel{" active" if i == 0 else ""}" id="{key}">{body}</div>'
        for i, (key, _, body) in enumerate(tabs)
    )

    verdict = (
        '<p class="ok">Both drives hold the same files.</p>'
        if result.in_sync
        else (
            f'<p class="warn">{counts["only_left"]:,} files missing from the right · '
            f'{counts["only_right"]:,} missing from the left · '
            f'{counts["conflicts"]:,} content conflicts</p>'
        )
    )

    stats = "".join(
        f'<div class="stat"><span class="num">{value}</span><span class="lbl">{label}</span></div>'
        for label, value in [
            ("Files left", f"{counts['left_files']:,}"),
            ("Files right", f"{counts['right_files']:,}"),
            ("Identical", f"{counts['identical']:,}"),
            ("Only left", f"{counts['only_left']:,}"),
            ("Only right", f"{counts['only_right']:,}"),
            ("Conflicts", f"{counts['conflicts']:,}"),
            ("Reclaimable", format_bytes(
                sum(g.wasted_bytes for g in dupes.within_left)
                + sum(g.wasted_bytes for g in dupes.within_right)
            )),
        ]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drive audit — {html.escape(str(meta.get('run_id', '')))}</title>
<style>
:root {{ --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e3e3e3; --card:#f7f7f8;
        --ok:#0a7c42; --warn:#b45309; --bad:#b91c1c; --accent:#2563eb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14161a; --fg:#e8e8ea; --muted:#9a9aa2; --line:#2a2d34; --card:#1c1f25;
          --ok:#34d399; --warn:#fbbf24; --bad:#f87171; --accent:#60a5fa; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1200px; margin:0 auto; }}
h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
.sub {{ color:var(--muted); font-size:.875rem; margin-bottom:1.5rem; }}
.ok {{ color:var(--ok); font-weight:600; }}
.warn {{ color:var(--warn); font-weight:600; }}
.roots {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:.75rem; margin:1rem 0 1.5rem; }}
.root {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.75rem 1rem; }}
.root b {{ display:block; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
.root code {{ font-size:.8rem; word-break:break-all; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:.5rem; margin-bottom:1.5rem; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.7rem .8rem; }}
.stat .num {{ display:block; font-size:1.35rem; font-weight:650; }}
.stat .lbl {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
#filter {{ width:100%; padding:.6rem .8rem; margin-bottom:1rem; border:1px solid var(--line);
          border-radius:8px; background:var(--card); color:var(--fg); font-size:.9rem; }}
.tabs {{ display:flex; flex-wrap:wrap; gap:.35rem; margin-bottom:1rem; }}
.tab {{ padding:.45rem .8rem; border:1px solid var(--line); background:var(--card); color:var(--fg);
       border-radius:999px; cursor:pointer; font-size:.82rem; }}
.tab.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.panel {{ display:none; }} .panel.active {{ display:block; }}
.scroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:.84rem; }}
th,td {{ text-align:left; padding:.5rem .7rem; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ background:var(--card); font-weight:600; position:sticky; top:0; }}
td.path {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; word-break:break-all; }}
td.num {{ text-align:right; white-space:nowrap; }}
.empty {{ color:var(--muted); padding:1.5rem; text-align:center; }}
tr.hidden {{ display:none; }}
</style></head><body><div class="wrap">
<h1>Drive audit</h1>
<div class="sub">{html.escape(str(meta.get('profile', 'ad-hoc')))} ·
{html.escape(str(meta.get('started', '')))} ·
verify <code>{html.escape(result.verify)}</code> ·
{meta.get('duration_s', 0):.1f}s</div>
{verdict}
<div class="roots">
<div class="root"><b>Left</b><code>{html.escape(result.left.root)}</code><br>
<span class="sub">serial {html.escape(result.left.volume)}</span></div>
<div class="root"><b>Right</b><code>{html.escape(result.right.root)}</code><br>
<span class="sub">serial {html.escape(result.right.volume)}</span></div>
</div>
<div class="stats">{stats}</div>
<input id="filter" type="search" placeholder="Filter rows by path…" autocomplete="off">
<div class="tabs">{buttons}</div>
{panels}
</div><script>
document.querySelectorAll('.tab').forEach(function (tab) {{
  tab.addEventListener('click', function () {{
    document.querySelectorAll('.tab').forEach(function (t) {{ t.classList.remove('active'); }});
    document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('active'); }});
    tab.classList.add('active');
    document.getElementById(tab.dataset.target).classList.add('active');
  }});
}});
document.getElementById('filter').addEventListener('input', function (event) {{
  var needle = event.target.value.toLowerCase();
  document.querySelectorAll('tbody tr').forEach(function (row) {{
    row.classList.toggle('hidden', needle !== '' && row.textContent.toLowerCase().indexOf(needle) === -1);
  }});
}});
</script></body></html>
"""


def _table(headers: list[str], rows: list[str]) -> str:
    if not rows:
        return '<p class="empty">Nothing in this category.</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _rows_single(records) -> str:
    rows = [
        f'<tr><td class="path">{html.escape(r.relpath)}</td>'
        f'<td class="num">{format_bytes(r.size)}</td>'
        f'<td class="num">{format_time(r.mtime_ns)}</td></tr>'
        for r in records[:2000]
    ]
    return _table(["Path", "Size", "Modified"], rows)


def _rows_pairs(pairs) -> str:
    rows = [
        f'<tr><td class="path">{html.escape(p.relpath)}</td>'
        f'<td class="num">{format_bytes(p.left.size)}</td>'
        f'<td class="num">{format_time(p.left.mtime_ns)}</td>'
        f'<td class="num">{format_bytes(p.right.size)}</td>'
        f'<td class="num">{format_time(p.right.mtime_ns)}</td>'
        f'<td>{html.escape(p.reason)}</td></tr>'
        for p in pairs[:2000]
    ]
    return _table(["Path", "Left size", "Left modified", "Right size", "Right modified", "Why"], rows)


def _rows_groups(groups) -> str:
    rows = []
    for group in groups[:1000]:
        paths = "<br>".join(html.escape(path) for path in group.paths())
        rows.append(
            f'<tr><td class="path">{paths}</td>'
            f'<td class="num">{format_bytes(group.size)}</td>'
            f'<td class="num">{len(group.entries)}</td>'
            f'<td class="num">{format_bytes(group.wasted_bytes)}</td></tr>'
        )
    return _table(["Paths", "File size", "Copies", "Reclaimable"], rows)
