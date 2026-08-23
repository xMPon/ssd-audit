"""Persistent hash cache.

Hashing is the expensive part of an audit, and almost nothing changes between
runs. This caches every digest against ``(volume serial, path, size, mtime)`` so
a repeat audit of an unchanged drive reads no file content at all.

The cache is keyed on the **volume serial**, never the drive letter, so it stays
valid when Windows remounts the drive somewhere else.

All access is single-threaded by design: hashing fans out across a thread pool,
but results come back to the main thread for storage. That keeps SQLite simple
and correct without a connection-per-thread dance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_hash (
    volume_id   TEXT    NOT NULL,
    relpath_key TEXT    NOT NULL,
    relpath     TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    quick_hash  TEXT,
    full_hash   TEXT,
    hashed_at   TEXT    NOT NULL,
    PRIMARY KEY (volume_id, relpath_key)
);
CREATE INDEX IF NOT EXISTS idx_file_hash_full ON file_hash(full_hash);
CREATE INDEX IF NOT EXISTS idx_file_hash_size ON file_hash(size);
"""


def default_cache_path() -> Path:
    return data_dir() / "cache.db"


class HashIndex:
    """Cache of file digests, valid only while size and mtime are unchanged."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = Path(db_path) if db_path else default_cache_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.executescript(SCHEMA)
        # Durability matters less here than speed: a lost cache entry only costs
        # a re-hash, never correctness.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.commit()
        self.hits = 0
        self.misses = 0

    def lookup(self, volume: str, key: str, size: int, mtime_ns: int) -> tuple[str, str] | None:
        """Return cached ``(quick_hash, full_hash)``, or None if stale/absent.

        A row only counts as a hit when size *and* mtime match exactly -- any
        edit invalidates it.
        """
        row = self.connection.execute(
            "SELECT quick_hash, full_hash, size, mtime_ns FROM file_hash "
            "WHERE volume_id = ? AND relpath_key = ?",
            (volume, key),
        ).fetchone()

        if row is None or row[2] != size or row[3] != mtime_ns:
            self.misses += 1
            return None

        self.hits += 1
        return row[0] or "", row[1] or ""

    def store(
        self,
        volume: str,
        key: str,
        relpath: str,
        size: int,
        mtime_ns: int,
        quick: str = "",
        full: str = "",
    ) -> None:
        """Upsert a digest, preserving whichever tier is not being written."""
        self.connection.execute(
            """
            INSERT INTO file_hash
                (volume_id, relpath_key, relpath, size, mtime_ns, quick_hash, full_hash, hashed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(volume_id, relpath_key) DO UPDATE SET
                relpath    = excluded.relpath,
                size       = excluded.size,
                mtime_ns   = excluded.mtime_ns,
                quick_hash = COALESCE(NULLIF(excluded.quick_hash, ''), file_hash.quick_hash),
                full_hash  = COALESCE(NULLIF(excluded.full_hash, ''),  file_hash.full_hash),
                hashed_at  = excluded.hashed_at
            """,
            (
                volume,
                key,
                relpath,
                size,
                mtime_ns,
                quick,
                full,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )

    def commit(self) -> None:
        self.connection.commit()

    def prune(self, volume: str, live_keys: set[str]) -> int:
        """Drop rows for files that no longer exist on *volume*."""
        cursor = self.connection.execute(
            "SELECT relpath_key FROM file_hash WHERE volume_id = ?", (volume,)
        )
        dead = [(volume, key) for (key,) in cursor if key not in live_keys]
        self.connection.executemany(
            "DELETE FROM file_hash WHERE volume_id = ? AND relpath_key = ?", dead
        )
        self.connection.commit()
        return len(dead)

    def stats(self) -> dict:
        rows, volumes = self.connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT volume_id) FROM file_hash"
        ).fetchone()
        return {"rows": rows, "volumes": volumes, "hits": self.hits, "misses": self.misses}

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> "HashIndex":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
