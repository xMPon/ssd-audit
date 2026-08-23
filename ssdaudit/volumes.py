"""Volume identity.

Drive letters are not stable for external drives -- unplug two SSDs, plug them
back in the other order, and ``G:`` is now the other disk. A saved profile that
records ``G:\\Photos`` would silently audit the wrong hardware.

So a volume is identified by its **serial number**, read straight from the
filesystem, and the current mount point is resolved from that serial at run
time. Everything here is read-only: no file is opened and nothing is written to
the drive.
"""

from __future__ import annotations

import ctypes
import os
import string
from ctypes import wintypes
from dataclasses import dataclass, asdict

# GetDriveTypeW return values.
_DRIVE_TYPES = {
    0: "unknown",
    1: "no-root-dir",
    2: "removable",
    3: "fixed",
    4: "network",
    5: "cdrom",
    6: "ramdisk",
}

# Stops Windows popping a "There is no disk in the drive" dialog when we probe
# an empty card reader slot.
_SEM_FAILCRITICALERRORS = 0x0001


@dataclass(frozen=True)
class Volume:
    """A mounted filesystem, identified by serial rather than by letter."""

    mount: str
    serial: str
    label: str
    fs_type: str
    drive_type: str

    @property
    def is_external(self) -> bool:
        """Heuristic only -- USB-attached SSDs frequently report as ``fixed``."""
        return self.drive_type == "removable"

    def describe(self) -> str:
        label = self.label or "(no label)"
        return f"{self.mount}  {label}  [{self.fs_type}, {self.drive_type}, serial {self.serial}]"

    def to_dict(self) -> dict:
        return asdict(self)


def _format_serial(raw: int) -> str:
    """Render a volume serial the way Windows itself does: ``1A2B-3C4D``."""
    value = raw & 0xFFFFFFFF
    return f"{value >> 16:04X}-{value & 0xFFFF:04X}"


def _query_windows_volume(root: str) -> Volume | None:
    """Read volume metadata for a root like ``G:\\``, or None if unreadable."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    label_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()

    previous_mode = kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS)
    try:
        ok = kernel32.GetVolumeInformationW(
            wintypes.LPCWSTR(root),
            label_buf,
            ctypes.sizeof(label_buf) // ctypes.sizeof(ctypes.c_wchar),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            fs_buf,
            ctypes.sizeof(fs_buf) // ctypes.sizeof(ctypes.c_wchar),
        )
        drive_type = _DRIVE_TYPES.get(kernel32.GetDriveTypeW(wintypes.LPCWSTR(root)), "unknown")
    finally:
        kernel32.SetErrorMode(previous_mode)

    if not ok:
        return None

    return Volume(
        mount=root,
        serial=_format_serial(serial.value),
        label=label_buf.value,
        fs_type=fs_buf.value,
        drive_type=drive_type,
    )


def _posix_volume(path: str) -> Volume | None:
    """Fallback identity for non-Windows: the device number of the mount."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return Volume(
        mount=path,
        serial=f"dev-{stat.st_dev:x}",
        label="",
        fs_type="",
        drive_type="unknown",
    )


def list_volumes() -> list[Volume]:
    """Every currently mounted volume we can read identity from."""
    if os.name != "nt":
        # No portable enumeration; callers on POSIX address volumes by path.
        return []

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    bitmask = kernel32.GetLogicalDrives()

    volumes = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not bitmask & (1 << index):
            continue
        volume = _query_windows_volume(f"{letter}:\\")
        if volume is not None:
            volumes.append(volume)
    return volumes


def find_by_serial(serial: str) -> Volume | None:
    """Locate a volume by serial, wherever it happens to be mounted now."""
    target = serial.strip().upper()
    for volume in list_volumes():
        if volume.serial == target:
            return volume
    return None


def volume_for_path(path: str) -> Volume | None:
    """Identify the volume that *path* lives on."""
    if os.name != "nt":
        return _posix_volume(path)
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None
    return _query_windows_volume(drive + "\\")


def volume_id(path: str) -> str:
    """Stable cache key for the volume holding *path*.

    Falls back to the drive root when identity cannot be read, so the cache
    still works (just less reliably) on unusual mounts.
    """
    volume = volume_for_path(path)
    if volume is not None:
        return volume.serial
    return os.path.splitdrive(os.path.abspath(path))[0].upper() or os.path.abspath(path)
