"""Hash resolution: cache lookups on the main thread, reads on a pool.

Cache hits are resolved first and never reach the pool, so an unchanged drive
costs nothing but SQLite lookups. Only genuine misses are dispatched for I/O.

One pool is shared across both drives rather than one per drive: two USB
volumes typically contend for the same host controller, so doubling the workers
would just deepen the queue.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from .hashing import full_hash, quick_hash
from .index import HashIndex
from .paths import volume_relative_key
from .scanner import FileRecord

DEFAULT_WORKERS = 8


def resolve(
    records: list[FileRecord],
    root: str,
    volume: str,
    index: HashIndex,
    tier: str = "quick",
    workers: int = DEFAULT_WORKERS,
    on_progress=None,
) -> list[str]:
    """Populate ``quick_hash``/``full_hash`` on *records*, in place.

    *tier* is ``"quick"`` or ``"full"``. Returns the paths that could not be
    read -- an unreadable file is reported, never fatal.
    """
    if not records:
        return []

    keys = {record.relpath: volume_relative_key(root, record.relpath) for record in records}

    pending: list[FileRecord] = []
    for record in records:
        cached = index.lookup(volume, keys[record.relpath], record.size, record.mtime_ns)
        if cached is not None:
            record.quick_hash, record.full_hash = cached
            if tier == "quick" and record.quick_hash:
                continue
            if tier == "full" and record.full_hash:
                continue
        pending.append(record)

    if not pending:
        return []

    errors: list[str] = []
    done = 0

    def compute(record: FileRecord) -> tuple[FileRecord, str, str | None]:
        path = os.path.join(root, record.relpath.replace("/", os.sep))
        try:
            digest = full_hash(path) if tier == "full" else quick_hash(path, record.size)
            return record, digest, None
        except OSError as error:
            return record, "", f"{record.relpath}: {error}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for record, digest, error in pool.map(compute, pending):
            done += 1
            if error:
                errors.append(error)
                continue
            key = keys[record.relpath]
            if tier == "full":
                record.full_hash = digest
                index.store(volume, key, record.relpath, record.size, record.mtime_ns,
                            quick=record.quick_hash, full=digest)
            else:
                record.quick_hash = digest
                index.store(volume, key, record.relpath, record.size, record.mtime_ns,
                            quick=digest, full=record.full_hash)
            if on_progress and done % 200 == 0:
                on_progress(done, len(pending))

    index.commit()
    return errors
