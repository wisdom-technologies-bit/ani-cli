"""Durable MAL id -> HiAnime mapping store.

Why a store at all: resolving a MAL id costs one metadata lookup plus a verification
probe per candidate, so a verified answer must be reused rather than re-derived on
every request. The store holds only *verified* rows; a row that was never proven by a
provider embed cannot be written here.

Backend: SQLite from the standard library (no new dependency). The file lives at
ANI_API_MAPPING_DB. When that path is not writable (read-only container, ephemeral
filesystem) construction falls back to an in-memory database and says so through
``persistent``, which the admin endpoint reports instead of pretending to persist.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS anime_map (
    mal_id        INTEGER PRIMARY KEY,
    hianime_id    TEXT NOT NULL,
    title         TEXT,
    title_english TEXT,
    title_native  TEXT,
    titles_json   TEXT,
    format        TEXT,
    season_year   INTEGER,
    episode_count INTEGER DEFAULT 0,
    verified_by   TEXT,
    verified_at   TEXT,
    candidate_source TEXT
);
CREATE TABLE IF NOT EXISTS anime_parts (
    mal_id           INTEGER NOT NULL,
    hianime_id       TEXT NOT NULL,
    title            TEXT,
    episode_count    INTEGER DEFAULT 0,
    mal_episode_start INTEGER,
    mal_episode_end   INTEGER,
    verified_at      TEXT,
    evidence         TEXT,
    PRIMARY KEY (mal_id, hianime_id)
);
CREATE TABLE IF NOT EXISTS episode_map (
    mal_id     INTEGER NOT NULL,
    mal_number INTEGER NOT NULL,
    hianime_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    verified_at TEXT,
    evidence   TEXT,
    PRIMARY KEY (mal_id, mal_number)
);
"""


@dataclass
class StoredAnime:
    mal_id: int
    hianime_id: str
    title: Optional[str] = None
    title_english: Optional[str] = None
    title_native: Optional[str] = None
    titles: list[str] = field(default_factory=list)
    format: Optional[str] = None
    season_year: Optional[int] = None
    episode_count: int = 0
    verified_by: Optional[str] = None
    verified_at: Optional[str] = None
    candidate_source: Optional[str] = None
    parts: list[dict] = field(default_factory=list)


class MappingStore:
    """Small synchronous SQLite wrapper; call it from a worker thread."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.persistent = False
        self._lock = threading.Lock()
        self._conn = self._connect(path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _connect(self, path: str) -> sqlite3.Connection:
        if path and path != ":memory:":
            try:
                parent = os.path.dirname(os.path.abspath(path))
                os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False)
                self.persistent = True
                return conn
            except Exception:
                pass  # fall through to memory: never fail a request over a cache
        self.persistent = False
        return sqlite3.connect(":memory:", check_same_thread=False)

    # ---------- reads ----------
    def get_anime(self, mal_id: int) -> Optional[StoredAnime]:
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT mal_id, hianime_id, title, title_english, title_native, titles_json,"
                " format, season_year, episode_count, verified_by, verified_at, candidate_source"
                " FROM anime_map WHERE mal_id = ?", (mal_id,)).fetchone()
            if not row:
                return None
            parts = [dict(zip(
                ("hianime_id", "title", "episode_count", "mal_episode_start",
                 "mal_episode_end", "verified_at", "evidence"),
                p)) for p in self._conn.execute(
                "SELECT hianime_id, title, episode_count, mal_episode_start, mal_episode_end,"
                " verified_at, evidence FROM anime_parts WHERE mal_id = ?"
                " ORDER BY COALESCE(mal_episode_start, 999999), hianime_id", (mal_id,))]
        return StoredAnime(
            mal_id=row[0], hianime_id=row[1], title=row[2], title_english=row[3],
            title_native=row[4], titles=json.loads(row[5] or "[]"), format=row[6],
            season_year=row[7], episode_count=row[8] or 0, verified_by=row[9],
            verified_at=row[10], candidate_source=row[11], parts=parts)

    def get_episode(self, mal_id: int, mal_number: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT hianime_id, episode_id, verified_at, evidence FROM episode_map"
                " WHERE mal_id = ? AND mal_number = ?", (mal_id, mal_number)).fetchone()
        if not row:
            return None
        return {"hianime_id": row[0], "episode_id": row[1],
                "verified_at": row[2], "evidence": row[3]}

    def episodes_for(self, mal_id: int) -> dict[int, dict]:
        """All verified episode rows for one MAL id, keyed by MAL episode number."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mal_number, hianime_id, episode_id, verified_at, evidence"
                " FROM episode_map WHERE mal_id = ?", (mal_id,)).fetchall()
        return {int(r[0]): {"hianime_id": r[1], "episode_id": r[2],
                            "verified_at": r[3], "evidence": r[4]} for r in rows}

    def stats(self) -> dict:
        with self._lock:
            anime = self._conn.execute("SELECT COUNT(*) FROM anime_map").fetchone()[0]
            episodes = self._conn.execute("SELECT COUNT(*) FROM episode_map").fetchone()[0]
        return {"anime": anime, "episodes": episodes}

    # ---------- writes ----------
    def save_anime(self, record: StoredAnime) -> None:
        import json

        with self._lock:
            self._conn.execute(
                "INSERT INTO anime_map (mal_id, hianime_id, title, title_english, title_native,"
                " titles_json, format, season_year, episode_count, verified_by, verified_at,"
                " candidate_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(mal_id) DO UPDATE SET hianime_id=excluded.hianime_id,"
                " title=excluded.title, title_english=excluded.title_english,"
                " title_native=excluded.title_native, titles_json=excluded.titles_json,"
                " format=excluded.format, season_year=excluded.season_year,"
                " episode_count=excluded.episode_count, verified_by=excluded.verified_by,"
                " verified_at=excluded.verified_at, candidate_source=excluded.candidate_source",
                (record.mal_id, record.hianime_id, record.title, record.title_english,
                 record.title_native, json.dumps(record.titles), record.format,
                 record.season_year, record.episode_count, record.verified_by,
                 record.verified_at, record.candidate_source))
            for part in record.parts:
                self._conn.execute(
                    "INSERT INTO anime_parts (mal_id, hianime_id, title, episode_count,"
                    " mal_episode_start, mal_episode_end, verified_at, evidence)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(mal_id, hianime_id) DO UPDATE SET title=excluded.title,"
                    " episode_count=excluded.episode_count,"
                    " mal_episode_start=excluded.mal_episode_start,"
                    " mal_episode_end=excluded.mal_episode_end,"
                    " verified_at=excluded.verified_at, evidence=excluded.evidence",
                    (record.mal_id, part["hianime_id"], part.get("title"),
                     part.get("episode_count", 0), part.get("mal_episode_start"),
                     part.get("mal_episode_end"), part.get("verified_at"),
                     part.get("evidence")))
            self._conn.commit()

    def save_episode(self, mal_id: int, mal_number: int, hianime_id: str, episode_id: str,
                     verified_at: str, evidence: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO episode_map (mal_id, mal_number, hianime_id, episode_id,"
                " verified_at, evidence) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(mal_id, mal_number) DO UPDATE SET"
                " hianime_id=excluded.hianime_id, episode_id=excluded.episode_id,"
                " verified_at=excluded.verified_at, evidence=excluded.evidence",
                (mal_id, mal_number, hianime_id, episode_id, verified_at, evidence))
            self._conn.commit()

    def clear(self, mal_id: Optional[int] = None) -> None:
        with self._lock:
            if mal_id is None:
                for table in ("anime_map", "anime_parts", "episode_map"):
                    self._conn.execute(f"DELETE FROM {table}")
            else:
                for table in ("anime_map", "anime_parts", "episode_map"):
                    self._conn.execute(f"DELETE FROM {table} WHERE mal_id = ?", (mal_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store: Optional[MappingStore] = None


def get_store() -> MappingStore:
    """Process-wide store, created on first use from settings."""
    global _store
    if _store is None:
        from .config import settings

        _store = MappingStore(settings.mapping_db)
    return _store


def reset_store(path: str = ":memory:") -> MappingStore:
    """Test hook: replace the process-wide store."""
    global _store
    if _store is not None:
        try:
            _store.close()
        except Exception:
            pass
    _store = MappingStore(path)
    return _store
