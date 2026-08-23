"""Path normalisation.

Two drives can hold the same file under names that are byte-for-byte different.
Three causes matter in practice, and all three are handled here rather than
scattered through the comparison code:

* **Case.** exFAT and NTFS are case-insensitive but case-preserving, so
  ``Photo.JPG`` and ``photo.jpg`` are the same file on-disk.
* **Unicode form.** macOS writes filenames decomposed (NFD), Windows writes them
  composed (NFC). An accented filename written by a Mac and one written by
  Windows compare unequal despite displaying identically.
* **Length.** Paths over 260 characters need the ``\\\\?\\`` prefix to be opened
  at all, regardless of the LongPathsEnabled registry setting.

Every comparison is done on a *match key*; the original path is kept alongside
it for display, because the user needs to see the real name on disk.
"""

from __future__ import annotations

import os
import unicodedata

LONG_PATH_PREFIX = "\\\\?\\"
UNC_LONG_PATH_PREFIX = "\\\\?\\UNC\\"


def long_path(path: str) -> str:
    """Return *path* in a form that bypasses the MAX_PATH limit on Windows.

    The ``\\\\?\\`` prefix disables all path parsing by the OS, which means the
    path must already be fully qualified with backslash separators and no ``.``
    or ``..`` components -- so it is normalised first. On non-Windows platforms
    the path is returned unchanged.
    """
    if os.name != "nt":
        return path
    if path.startswith(LONG_PATH_PREFIX):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        # UNC \\server\share -> \\?\UNC\server\share
        return UNC_LONG_PATH_PREFIX + absolute[2:]
    return LONG_PATH_PREFIX + absolute


def strip_long_path(path: str) -> str:
    """Inverse of :func:`long_path`, for anything shown to the user."""
    if path.startswith(UNC_LONG_PATH_PREFIX):
        return "\\\\" + path[len(UNC_LONG_PATH_PREFIX):]
    if path.startswith(LONG_PATH_PREFIX):
        return path[len(LONG_PATH_PREFIX):]
    return path


def to_posix(relpath: str) -> str:
    """Normalise separators so keys are comparable across platforms."""
    return relpath.replace("\\", "/").strip("/")


def match_key(relpath: str) -> str:
    """Build the key used to pair a file on one drive with one on the other.

    Folds away the case and Unicode-form differences described in the module
    docstring. The result is for matching only -- never display it.
    """
    return unicodedata.normalize("NFC", to_posix(relpath)).casefold()


def volume_relative_key(root: str, relpath: str) -> str:
    """Key a file by where it sits on the *volume*, not within the scan scope.

    The cache is keyed on this rather than on the scan-relative path, because
    two different scan roots on one drive routinely contain the same relative
    path -- ``D:\\A\\notes.txt`` and ``D:\\B\\notes.txt`` are both ``notes.txt``.
    Keying on the scope-relative path would let one overwrite the other's digest.
    """
    absolute = os.path.abspath(os.path.join(root, relpath.replace("/", os.sep)))
    _, remainder = os.path.splitdrive(absolute)
    return match_key(remainder)


def unicode_form(text: str) -> str:
    """Report which normalisation form *text* is already in.

    Used to surface NFC/NFD divergence in the report: the files match after
    normalisation, but their on-disk names genuinely differ, which some tools
    (and some sync software) will trip over.
    """
    if unicodedata.normalize("NFC", text) == text:
        return "NFC"
    if unicodedata.normalize("NFD", text) == text:
        return "NFD"
    return "mixed"


def is_ascii_only(text: str) -> bool:
    """True when Unicode form cannot possibly differ, letting callers skip the check."""
    return text.isascii()
