"""SQLite cache for file audio tracks and AniList answers (``UserData/cache.db``)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

LOOKUP_CHUNK = 500
SAVE_BATCH = 25
MISSING = object()  # get_anilist() result when nothing usable is cached


class Cache:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, "
                               "mtime REAL, tracks TEXT, scanned_at REAL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS anilist (key TEXT PRIMARY KEY, response TEXT, "
                               "fetched_at REAL)")
            self._conn.commit()

    # ------------------------------------------------------------ files

    def get_files(self, wanted: Mapping[str, tuple[int, float]]) -> dict[str, list[dict]]:
        """Cached tracks for every key whose size and mtime still match. One query per 500 keys."""
        keys = list(wanted)
        found: dict[str, list[dict]] = {}
        for start in range(0, len(keys), LOOKUP_CHUNK):
            chunk = keys[start:start + LOOKUP_CHUNK]
            query = f"SELECT path, size, mtime, tracks FROM files WHERE path IN ({','.join('?' * len(chunk))})"
            with self._lock:
                rows = self._conn.execute(query, chunk).fetchall()
            for path, size, mtime, tracks in rows:
                want_size, want_mtime = wanted[path]
                if size == want_size and abs((mtime or 0.0) - want_mtime) < 0.001:
                    try:
                        found[path] = json.loads(tracks)
                    except ValueError:
                        pass
        return found

    def save_files(self, rows: Iterable[tuple[str, int, float, list[dict]]]) -> None:
        now = time.time()
        data = [(key, size, mtime, json.dumps(tracks, ensure_ascii=False), now) for key, size, mtime, tracks in rows]
        if not data:
            return
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO files (path, size, mtime, tracks, scanned_at) "
                                   "VALUES (?, ?, ?, ?, ?)", data)
            self._conn.commit()

    def file_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # ------------------------------------------------------------ AniList

    def get_anilist(self, key: str, max_age_days: float) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT response, fetched_at FROM anilist WHERE key = ?", (key,)).fetchone()
        if row is None or time.time() - row[1] > max_age_days * 86400:
            return MISSING
        try:
            return json.loads(row[0])
        except ValueError:
            return MISSING

    def save_anilist(self, key: str, response: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO anilist (key, response, fetched_at) VALUES (?, ?, ?)",
                               (key, json.dumps(response, ensure_ascii=False), time.time()))
            self._conn.commit()

    def clear_anilist(self) -> int:
        with self._lock:
            count = self._conn.execute("DELETE FROM anilist").rowcount
            self._conn.commit()
        return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class FileCacheWriter:
    """Collects freshly read files from reader threads and saves them in batches."""

    def __init__(self, cache: Cache, batch_size: int = SAVE_BATCH) -> None:
        self._cache = cache
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._pending: list[tuple[str, int, float, list[dict]]] = []

    def add(self, key: str, size: int, mtime: float, tracks: list[dict]) -> None:
        with self._lock:
            self._pending.append((key, size, mtime, tracks))
            if len(self._pending) < self._batch_size:
                return
            rows, self._pending = self._pending, []
        self._cache.save_files(rows)

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
        self._cache.save_files(rows)
