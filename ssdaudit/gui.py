"""Desktop interface.

A window for choosing what to compare and watching it happen. Built on tkinter
so it stays dependency-free and runs from a plain Python install.

Two rules shape the design:

* **Nothing is chosen for you.** No drive is preselected and the Start button
  stays disabled until both sides have been picked deliberately. Auditing the
  wrong pair wastes a long scan, and a helpful default is exactly how that
  happens.
* **The work is always visible.** Scanning and hashing run on a worker thread
  and report continuously, so the window never looks frozen and you can see
  which drive is being read and how far along it is.

Only the main thread touches widgets; the worker communicates through a queue.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .compare import compare
from .config import IgnoreRules
from .dupes import DEFAULT_MIN_SIZE, find_duplicates
from .history import append_run, run_directory
from .index import HashIndex, default_cache_path
from .progress import format_bytes, format_duration
from .remediate import write_all
from .report import new_run_id, write_audit
from .resolver import DEFAULT_WORKERS
from .scanner import scan
from .volumes import Volume, list_volumes

POLL_MS = 80


class SideChooser(ttk.LabelFrame):
    """One side of the comparison: a drive list plus an optional folder."""

    COLUMNS = (
        ("drive", "Drive", 60),
        ("label", "Label", 150),
        ("format", "Format", 65),
        ("capacity", "Capacity", 85),
        ("used", "Used", 85),
        ("free", "Free", 85),
        ("type", "Type", 85),
    )

    def __init__(self, parent, title: str, on_change):
        super().__init__(parent, text=title, padding=8)
        self.on_change = on_change
        self.volumes: list[Volume] = []
        self.path = tk.StringVar(value="")

        self.tree = ttk.Treeview(
            self, columns=[key for key, _, _ in self.COLUMNS],
            show="headings", height=5, selectmode="browse",
        )
        for key, heading, width in self.COLUMNS:
            self.tree.heading(key, text=heading)
            anchor = "e" if key in ("capacity", "used", "free") else "w"
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "label"))
        self.tree.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Label(self, text="Comparing:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.path_entry = ttk.Entry(self, textvariable=self.path, state="readonly")
        self.path_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0), padx=(4, 4))
        ttk.Button(self, text="Choose folder...", command=self._browse).grid(
            row=1, column=2, sticky="e", pady=(8, 0))

        self.columnconfigure(1, weight=1)

    def set_volumes(self, volumes: list[Volume]) -> None:
        self.volumes = volumes
        self.tree.delete(*self.tree.get_children())
        for index, volume in enumerate(volumes):
            self.tree.insert("", "end", iid=str(index), values=(
                volume.mount,
                volume.label or "(no label)",
                volume.fs_type,
                format_bytes(volume.total_bytes),
                format_bytes(volume.used_bytes),
                format_bytes(volume.free_bytes),
                volume.drive_type,
            ))
        # Deliberately no default selection.
        self.path.set("")
        self.on_change()

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection:
            self.path.set(self.volumes[int(selection[0])].mount)
        self.on_change()

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title=f"{self['text']} - choose a folder")
        if chosen:
            self.path.set(os.path.normpath(chosen))
            self.tree.selection_remove(*self.tree.selection())
            self.on_change()

    def selection(self) -> str:
        return self.path.get().strip()


class AuditApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"SSD Audit {__version__}")
        self.geometry("980x760")
        self.minsize(860, 660)

        self.messages: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_run_dir: Path | None = None

        self._build()
        self.refresh_drives()
        self.after(POLL_MS, self._drain)

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Choose two drives or folders to compare. Both are read only - "
                 "nothing is copied, moved or deleted.",
            wraplength=920,
        ).pack(anchor="w", pady=(0, 10))

        self.left = SideChooser(outer, "LEFT side", self._validate)
        self.left.pack(fill="x", pady=(0, 8))

        self.right = SideChooser(outer, "RIGHT side", self._validate)
        self.right.pack(fill="x", pady=(0, 8))

        self._build_options(outer)
        self._build_actions(outer)
        self._build_output(outer)

    def _build_options(self, parent) -> None:
        options = ttk.LabelFrame(parent, text="Options", padding=8)
        options.pack(fill="x", pady=(0, 8))

        ttk.Label(options, text="Check files by:").grid(row=0, column=0, sticky="w")
        self.verify = tk.StringVar(value="smart")
        for column, (value, text) in enumerate((
            ("metadata", "Size and date only (fastest)"),
            ("smart", "Read only what's unclear (recommended)"),
            ("full", "Read every file (slowest)"),
        ), start=1):
            ttk.Radiobutton(options, text=text, value=value,
                            variable=self.verify).grid(row=0, column=column, sticky="w", padx=6)

        self.want_dupes = tk.BooleanVar(value=True)
        self.skip_dev = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Find duplicate files",
                        variable=self.want_dupes).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(options, text="Skip node_modules, .git and build folders",
                        variable=self.skip_dev).grid(row=1, column=2, columnspan=2,
                                                     sticky="w", pady=(6, 0))

    def _build_actions(self, parent) -> None:
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(0, 8))

        ttk.Button(actions, text="Refresh drives", command=self.refresh_drives).pack(side="left")
        self.start_button = ttk.Button(actions, text="Start comparison",
                                       command=self.start, state="disabled")
        self.start_button.pack(side="right")
        self.status = ttk.Label(actions, text="Select a drive or folder on both sides.")
        self.status.pack(side="left", padx=12)

    def _build_output(self, parent) -> None:
        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 4))

        self.phase = ttk.Label(parent, text="", anchor="w")
        self.phase.pack(fill="x", pady=(0, 6))

        frame = ttk.LabelFrame(parent, text="Progress and results", padding=4)
        frame.pack(fill="both", expand=True)

        self.log = tk.Text(frame, height=14, wrap="none", state="disabled",
                           font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        results = ttk.Frame(parent)
        results.pack(fill="x", pady=(8, 0))
        self.open_report = ttk.Button(results, text="Open report",
                                      command=self._open_report, state="disabled")
        self.open_report.pack(side="left")
        self.open_folder = ttk.Button(results, text="Open audit folder",
                                      command=self._open_folder, state="disabled")
        self.open_folder.pack(side="left", padx=6)

    # ------------------------------------------------------------------ helpers

    def write(self, text: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_drives(self) -> None:
        volumes = list_volumes()
        self.left.set_volumes(volumes)
        self.right.set_volumes(volumes)
        if not volumes:
            self.write("No drives could be identified. Use 'Choose folder...' instead.")

    def _validate(self, *_args) -> None:
        left, right = self.left.selection(), self.right.selection()

        if not left or not right:
            self.status.configure(text="Select a drive or folder on both sides.")
            self.start_button.configure(state="disabled")
            return

        if os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right)):
            self.status.configure(text="Both sides are the same location - pick two different ones.")
            self.start_button.configure(state="disabled")
            return

        self.status.configure(text="Ready.")
        self.start_button.configure(state="normal")

    # -------------------------------------------------------------------- run

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        left, right = self.left.selection(), self.right.selection()
        for path, side in ((left, "Left"), (right, "Right")):
            if not os.path.isdir(path):
                messagebox.showerror("Cannot start", f"{side} side is not a folder:\n{path}")
                return

        self.start_button.configure(state="disabled")
        self.open_report.configure(state="disabled")
        self.open_folder.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        self.write(f"LEFT   {left}")
        self.write(f"RIGHT  {right}")
        self.write(f"Check  {self.verify.get()}")
        self.write("")
        self.write("Reading both drives. Nothing will be modified.")
        self.write("")

        plan = {
            "left": left,
            "right": right,
            "verify": self.verify.get(),
            "dupes": self.want_dupes.get(),
            "presets": ["dev"] if self.skip_dev.get() else [],
        }
        self.worker = threading.Thread(target=self._run, args=(plan,), daemon=True)
        self.worker.start()

    def _run(self, plan: dict) -> None:
        """Worker thread. Communicates only through the queue."""
        post = self.messages.put
        try:
            started = time.time()
            started_iso = datetime.now().isoformat(timespec="seconds")
            rules = IgnoreRules.build(presets=plan["presets"])

            def scan_reporter(label):
                def report(result, current=""):
                    post(("phase", f"{label}: {len(result.files):,} files, "
                                   f"{result.dirs_scanned:,} folders"))
                return report

            post(("phase", "Scanning left..."))
            left = scan(plan["left"], rules, [], scan_reporter("Scanning left"))
            post(("log", f"Scanned left:  {len(left.files):>9,} files "
                         f"in {left.dirs_scanned:,} folders"))

            post(("phase", "Scanning right..."))
            right = scan(plan["right"], rules, [], scan_reporter("Scanning right"))
            post(("log", f"Scanned right: {len(right.files):>9,} files "
                         f"in {right.dirs_scanned:,} folders"))

            def hash_reporter(label):
                def report(done, total):
                    post(("hash", (label, done, total)))
                return report

            with HashIndex(default_cache_path()) as index:
                post(("phase", "Comparing..."))
                result = compare(left, right, index, verify=plan["verify"],
                                 workers=DEFAULT_WORKERS,
                                 on_progress=hash_reporter("Verifying contents"))
                post(("log", "Compared:      done"))

                duplicates = None
                if plan["dupes"]:
                    post(("phase", "Looking for duplicates..."))
                    duplicates = find_duplicates(
                        left, right, index, scope="both", min_size=DEFAULT_MIN_SIZE,
                        workers=DEFAULT_WORKERS,
                        on_progress=hash_reporter("Hashing for duplicates"),
                    )
                    post(("log", "Duplicates:    done"))
                cache_stats = index.stats()

            duration = time.time() - started
            run_id = new_run_id("gui")
            directory = run_directory(run_id)
            meta = {
                "run_id": run_id, "profile": "gui", "started": started_iso,
                "duration_s": duration, "verify": plan["verify"], "folders": [],
                "left_label": "", "right_label": "", "version": __version__,
            }
            write_audit(result, duplicates, directory, meta)
            write_all(result, duplicates, directory)
            append_run({
                **meta,
                "left_volume": left.volume, "right_volume": right.volume,
                "left_root": left.root, "right_root": right.root,
                "counts": {**result.counts(), **(duplicates.counts() if duplicates else {})},
                "cache": cache_stats,
            })

            post(("done", (result, duplicates, directory, duration, cache_stats)))
        except Exception as error:                      # noqa: BLE001 - surfaced in the UI
            post(("error", f"{type(error).__name__}: {error}"))

    # ------------------------------------------------------------------ drain

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "phase":
                    self.phase.configure(text=payload)
                elif kind == "log":
                    self.write(payload)
                elif kind == "hash":
                    label, done, total = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(total, 1), value=done)
                    percent = (done / total * 100) if total else 0
                    self.phase.configure(text=f"{label}: {done:,}/{total:,} ({percent:.0f}%)")
                elif kind == "done":
                    self._finish(*payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.after(POLL_MS, self._drain)

    def _finish(self, result, duplicates, directory: Path, duration: float, cache: dict) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self.phase.configure(text=f"Finished in {format_duration(duration)}")

        counts = result.counts()
        self.write("")
        self.write("=" * 64)
        if result.in_sync:
            self.write("Both sides hold the same files.")
        else:
            self.write(f"{counts['only_left']:>9,} files missing from the RIGHT side "
                       f"({format_bytes(result.bytes_only_left)})")
            self.write(f"{counts['only_right']:>9,} files missing from the LEFT side "
                       f"({format_bytes(result.bytes_only_right)})")
            self.write(f"{counts['conflicts']:>9,} content conflicts (same path, different content)")
        self.write(f"{counts['identical']:>9,} identical on both sides")

        if counts["cruft_files"]:
            self.write(f"{counts['cruft_files']:>9,} junk files "
                       f"({format_bytes(counts['bytes_cruft'])} reclaimable)")
        if duplicates:
            groups = len(duplicates.within_left) + len(duplicates.within_right)
            wasted = (sum(g.wasted_bytes for g in duplicates.within_left)
                      + sum(g.wasted_bytes for g in duplicates.within_right))
            if groups:
                self.write(f"{groups:>9,} duplicate groups ({format_bytes(wasted)} reclaimable)")
            if duplicates.cross:
                self.write(f"{len(duplicates.cross):>9,} files on both sides under different names")

        errors = counts["scan_errors"] + len(result.hash_errors)
        if errors:
            self.write(f"{errors:>9,} files or folders could not be read")

        self.write("=" * 64)
        self.write(f"Cache: {cache['hits']:,} hits, {cache['misses']:,} misses")
        self.write("")
        self.write(f"Saved to: {directory}")
        if not result.in_sync:
            self.write("Review the generated .cmd scripts before running them.")
            self.write("Neither side was modified by this audit.")

        self.last_run_dir = directory
        self.open_report.configure(state="normal")
        self.open_folder.configure(state="normal")
        self.start_button.configure(state="normal")

    def _fail(self, message: str) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.phase.configure(text="Failed")
        self.write("")
        self.write(f"ERROR: {message}")
        self.start_button.configure(state="normal")
        messagebox.showerror("Audit failed", message)

    # ------------------------------------------------------------------ opening

    def _open(self, target: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(target)                     # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except OSError as error:
            messagebox.showerror("Could not open", f"{target}\n\n{error}")

    def _open_report(self) -> None:
        if self.last_run_dir:
            self._open(self.last_run_dir / "report.html")

    def _open_folder(self) -> None:
        if self.last_run_dir:
            self._open(self.last_run_dir)


def main(argv: list[str] | None = None) -> int:
    AuditApp().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
