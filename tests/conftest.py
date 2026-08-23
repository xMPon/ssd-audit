"""Synthetic drive pairs.

Every test runs against temp directories built here -- no real drive is ever
read. The fixture deliberately covers each classification bucket, including the
awkward ones (whole-hour timestamp drift, same-size-different-content, Unicode
form mismatch) that a naive comparison gets wrong.
"""

from __future__ import annotations

import os
import unicodedata

import pytest

from ssdaudit.index import HashIndex

# Fixed epoch so timestamp arithmetic in tests is readable.
BASE_TIME = 1_700_000_000


def write(path, content: bytes, mtime: int = BASE_TIME) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def trees(tmp_path):
    """Two drive roots covering every case the comparison must handle."""
    left = tmp_path / "left"
    right = tmp_path / "right"

    # --- identical, and must never be read ---
    write(left / "docs/same.txt", b"same content")
    write(right / "docs/same.txt", b"same content")

    # --- present on one side only ---
    write(left / "docs/only-left.txt", b"left only")
    write(right / "docs/only-right.txt", b"right only")

    # --- different size: provable without reading content ---
    write(left / "docs/size.txt", b"short")
    write(right / "docs/size.txt", b"considerably longer content")

    # --- mtime differs by less than exFAT's 2s granularity: not a difference ---
    write(left / "docs/granularity.txt", b"tolerated", BASE_TIME)
    write(right / "docs/granularity.txt", b"tolerated", BASE_TIME + 1)

    # --- touched but unmodified: hashing must clear it ---
    write(left / "docs/touched.txt", b"identical bytes", BASE_TIME)
    write(right / "docs/touched.txt", b"identical bytes", BASE_TIME + 99_999)

    # --- same size, different content: only hashing catches this ---
    write(left / "docs/silent.bin", b"AAAABBBBCCCC", BASE_TIME)
    write(right / "docs/silent.bin", b"AAAABBBBDDDD", BASE_TIME + 99_999)

    # --- whole-hour drift from a DST boundary, content untouched ---
    write(left / "docs/dst.txt", b"dst content", BASE_TIME)
    write(right / "docs/dst.txt", b"dst content", BASE_TIME + 3600)

    # --- cruft from Windows and macOS ---
    write(left / "docs/.DS_Store", b"mac junk")
    write(left / "docs/._resource", b"apple double")
    write(right / "docs/Thumbs.db", b"windows junk")

    # --- duplicate content within the left drive ---
    duplicate = b"D" * 8192
    write(left / "pics/holiday.jpg", duplicate)
    write(left / "pics/backup/holiday-copy.jpg", duplicate)

    # --- same file, moved to a different folder on the right drive ---
    moved = b"M" * 8192
    write(left / "media/clip.mp4", moved)
    write(right / "media/archive/clip.mp4", moved)

    # --- system directory that must be pruned, not walked ---
    write(left / "System Volume Information/tracking.log", b"should never be seen")

    return left, right


@pytest.fixture
def unicode_trees(tmp_path):
    """A filename written composed on one side and decomposed on the other."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    name = "Zażółć.txt"
    write(left / unicodedata.normalize("NFC", name), b"polish")
    write(right / unicodedata.normalize("NFD", name), b"polish")
    return left, right


@pytest.fixture
def index(tmp_path):
    with HashIndex(tmp_path / "cache.db") as cache:
        yield cache
