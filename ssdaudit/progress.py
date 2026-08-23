"""Live progress reporting.

A whole-drive scan can run for minutes with nothing to show for it. Silence
during that is indistinguishable from a hang, so every long phase reports what
it is doing, where it has got to, and how fast it is going.

On a terminal this redraws one line in place. When output is redirected it falls
back to periodic complete lines, so a piped run still leaves a readable log
rather than thousands of carriage returns.
"""

from __future__ import annotations

import shutil
import sys
import time


def format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def shorten(text: str, width: int) -> str:
    """Trim a path from the left, keeping the informative tail."""
    if width <= 3 or len(text) <= width:
        return text
    return "..." + text[-(width - 1):]


class Progress:
    """One redrawing status line, with a periodic fallback when redirected."""

    def __init__(self, stream=None, enabled: bool | None = None, interval: float = 0.1):
        self.stream = stream or sys.stdout
        self.interval = interval
        self.started = time.time()
        self._last_draw = 0.0
        self._last_line_length = 0
        self._active = False

        if enabled is None:
            try:
                enabled = self.stream.isatty()
            except (AttributeError, ValueError):
                enabled = False
        self.interactive = bool(enabled)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def update(self, text: str, force: bool = False) -> None:
        """Redraw the status line, rate-limited to keep the cost negligible."""
        now = time.time()
        if not force and now - self._last_draw < self.interval:
            return
        self._last_draw = now

        if self.interactive:
            width = shutil.get_terminal_size((100, 24)).columns - 1
            line = text[:width]
            padding = " " * max(0, self._last_line_length - len(line))
            self.stream.write("\r" + line + padding)
            self.stream.flush()
            self._last_line_length = len(line)
            self._active = True
        elif force:
            self.stream.write(text + "\n")
            self.stream.flush()

    def finish(self, text: str = "") -> None:
        """Clear the live line and optionally replace it with a final one."""
        if self.interactive and self._active:
            self.stream.write("\r" + " " * self._last_line_length + "\r")
            self._last_line_length = 0
            self._active = False
        if text:
            self.stream.write(text + "\n")
        self.stream.flush()


class ScanReporter:
    """Renders scanner progress: files found, rate, and where it currently is."""

    def __init__(self, progress: Progress, label: str):
        self.progress = progress
        self.label = label

    def __call__(self, result, current: str = "") -> None:
        elapsed = max(self.progress.elapsed, 0.001)
        rate = len(result.files) / elapsed
        text = (
            f"  {self.label}: {len(result.files):,} files | "
            f"{result.dirs_scanned:,} folders | {rate:,.0f}/s"
        )
        if current:
            text += f" | {shorten(current, 45)}"
        self.progress.update(text)


class HashReporter:
    """Renders hashing progress, which is the phase that actually reads data."""

    def __init__(self, progress: Progress, label: str):
        self.progress = progress
        self.label = label

    def __call__(self, done: int, total: int) -> None:
        percent = (done / total * 100) if total else 100.0
        elapsed = max(self.progress.elapsed, 0.001)
        remaining = ""
        if done:
            eta = (total - done) * (elapsed / done)
            remaining = f" | about {format_duration(eta)} left"
        self.progress.update(
            f"  {self.label}: {done:,}/{total:,} files ({percent:.0f}%){remaining}"
        )
