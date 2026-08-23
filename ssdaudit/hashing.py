"""Content hashing.

Two tiers, because hashing hundreds of gigabytes in full on every run is not
something anyone would wait for:

* **quick hash** -- size plus the first and last 64 KB. Files that merely happen
  to share a size almost always differ within the first block, so this rejects
  the overwhelming majority of false duplicate candidates for the cost of two
  seeks.
* **full hash** -- the whole file, streamed. Only computed once a quick hash has
  already said two files are worth the effort, or when ``--verify full`` is asked
  for explicitly.

blake2b rather than sha256: it is in the standard library and is appreciably
faster on 64-bit machines. This is deduplication, not cryptography.
"""

from __future__ import annotations

import hashlib
import os
import threading

from .paths import long_path

QUICK_HASH_THRESHOLD = 1024 * 1024        # below this, just hash the whole file
QUICK_HASH_EDGE = 64 * 1024               # bytes taken from each end
READ_BLOCK = 1024 * 1024
DIGEST_SIZE = 32


class ReadCounter:
    """Counts files actually read from disk.

    Exists so the test suite can assert that a second run over an unchanged tree
    reads nothing at all -- the cache is the whole performance story, so it gets
    measured rather than assumed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.files = 0
        self.bytes = 0

    def record(self, size: int) -> None:
        with self._lock:
            self.files += 1
            self.bytes += size

    def reset(self) -> None:
        with self._lock:
            self.files = 0
            self.bytes = 0


_counter = ReadCounter()


def counter() -> ReadCounter:
    return _counter


def full_hash(path: str) -> str:
    """Stream the entire file through blake2b."""
    digest = hashlib.blake2b(digest_size=DIGEST_SIZE)
    read = 0
    with open(long_path(path), "rb", buffering=0) as handle:
        while True:
            block = handle.read(READ_BLOCK)
            if not block:
                break
            digest.update(block)
            read += len(block)
    _counter.record(read)
    return digest.hexdigest()


def quick_hash(path: str, size: int) -> str:
    """Hash size + both ends of the file.

    Small files are hashed in full, since seeking costs more than reading them.
    The size is folded into the digest so two files can never collide on edges
    alone.
    """
    if size <= QUICK_HASH_THRESHOLD:
        return "q" + full_hash(path)

    digest = hashlib.blake2b(digest_size=DIGEST_SIZE)
    digest.update(str(size).encode("ascii"))
    with open(long_path(path), "rb", buffering=0) as handle:
        digest.update(handle.read(QUICK_HASH_EDGE))
        handle.seek(-QUICK_HASH_EDGE, os.SEEK_END)
        digest.update(handle.read(QUICK_HASH_EDGE))
    _counter.record(QUICK_HASH_EDGE * 2)
    return "q" + digest.hexdigest()
