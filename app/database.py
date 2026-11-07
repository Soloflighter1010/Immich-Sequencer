"""
database.py — SQLite schema and helpers.

All connections use WAL mode for safe concurrent reads from the API
thread and writes from the background sort thread.
"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/sequencer.db")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS albums (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    asset_count     INTEGER NOT NULL DEFAULT 0,
    -- Status values:
    --   unsorted    Never been sorted
    --   sorted      Sort completed, hash matches current state
    --   updated     Sorted before but asset_count changed since (new photos)
    --   interrupted Sort was started but cancelled or crashed mid-run
    --   sorting     Currently being sorted (cleared to sorted/interrupted on completion)
    --   empty       Album has no assets
    --   error       Last sort attempt failed entirely
    status          TEXT NOT NULL DEFAULT 'unsorted',
    order_hash      TEXT,           -- SHA-256 fingerprint of sorted filenames
    last_sorted_at  TEXT,           -- UTC ISO timestamp of last successful sort
    last_scanned_at TEXT,           -- UTC ISO timestamp of last scan
    assets_updated  INTEGER NOT NULL DEFAULT 0,
    assets_failed   INTEGER NOT NULL DEFAULT 0,
    mode            TEXT,           -- auto | manual | fallback
    base_time_used  TEXT,
    interval_s      REAL,
    sort_progress   INTEGER NOT NULL DEFAULT 0,  -- assets done in current/last run
    sort_total      INTEGER NOT NULL DEFAULT 0,  -- total assets in current/last run
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_albums_status ON albums(status);
CREATE INDEX IF NOT EXISTS idx_albums_name   ON albums(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS sort_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'running',  -- running|completed|cancelled|failed
    total_albums   INTEGER NOT NULL DEFAULT 0,
    sorted_albums  INTEGER NOT NULL DEFAULT 0,
    failed_albums  INTEGER NOT NULL DEFAULT 0,
    skipped_albums INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "immich_url":         lambda: os.environ.get("IMMICH_URL", "http://immich-server:2283"),
    "api_key":            lambda: os.environ.get("API_KEY", ""),
    "min_year":           lambda: "1990",
    "request_delay":      lambda: "0.05",
    "album_delay":        lambda: "1.0",
    "max_retries":        lambda: "3",
    "http_method":        lambda: "PUT",
    "base_time_override": lambda: "",   # empty = auto
}


def init_db() -> None:
    """Create schema and seed default settings (first run only)."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
        for key, factory in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, factory()),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def save_settings(data: dict) -> None:
    with get_db() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, str(v)),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Album helpers
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def upsert_album_from_scan(album_id: str, name: str, asset_count: int) -> str:
    """
    Sync a single album record from a fresh Immich scan.
    Returns the new status string so callers can build summary counts.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, asset_count FROM albums WHERE id = ?", (album_id,)
        ).fetchone()

        ts = now_utc()

        if row is None:
            # Brand-new album
            new_status = "empty" if asset_count == 0 else "unsorted"
            conn.execute(
                """INSERT INTO albums (id, name, asset_count, status, last_scanned_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (album_id, name, asset_count, new_status, ts),
            )
        else:
            old_status = row["status"]
            old_count  = row["asset_count"]

            if old_status == "sorting":
                # Server restarted mid-sort
                new_status = "interrupted"
            elif old_status == "sorted" and asset_count != old_count:
                # Photos were added or removed since last sort
                new_status = "updated"
            elif asset_count == 0 and old_status not in ("sorting",):
                new_status = "empty"
            else:
                new_status = old_status   # preserve existing status

            conn.execute(
                """UPDATE albums
                   SET name = ?, asset_count = ?, status = ?, last_scanned_at = ?
                   WHERE id = ?""",
                (name, asset_count, new_status, ts, album_id),
            )

        conn.commit()
        return new_status


def delete_album(album_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        conn.commit()


def get_album_counts() -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM albums GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def set_album_status(album_id: str, status: str, **extra) -> None:
    """Minimal status-only update used by the sorter."""
    with get_db() as conn:
        sets   = ["status = ?"]
        values = [status]
        for k, v in extra.items():
            sets.append(f"{k} = ?")
            values.append(v)
        values.append(album_id)
        conn.execute(
            f"UPDATE albums SET {', '.join(sets)} WHERE id = ?", values
        )
        conn.commit()


def mark_album_sorted(album_id: str, **fields) -> None:
    """Write the full sort result back to the album record."""
    with get_db() as conn:
        fields["last_sorted_at"] = now_utc()
        sets   = [f"{k} = ?" for k in fields]
        values = list(fields.values()) + [album_id]
        conn.execute(
            f"UPDATE albums SET {', '.join(sets)} WHERE id = ?", values
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Sort log helpers
# ---------------------------------------------------------------------------

def start_sort_log(total: int) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sort_log (started_at, status, total_albums) VALUES (?, 'running', ?)",
            (now_utc(), total),
        )
        conn.commit()
        return cur.lastrowid


def finish_sort_log(log_id: int, status: str, sorted_n: int, failed_n: int, skipped_n: int) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE sort_log
               SET completed_at = ?, status = ?,
                   sorted_albums = ?, failed_albums = ?, skipped_albums = ?
               WHERE id = ?""",
            (now_utc(), status, sorted_n, failed_n, skipped_n, log_id),
        )
        conn.commit()
