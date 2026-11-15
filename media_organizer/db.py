"""SQLite work queue and state. Every function commits before returning."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Collection

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, location TEXT NOT NULL, relpath TEXT NOT NULL,
  size INTEGER NOT NULL, mtime REAL NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', reason TEXT, settings TEXT,
  dest_location TEXT, dest_dir TEXT, final_relpath TEXT, final_size INTEGER,
  was_online_only INTEGER NOT NULL DEFAULT 0, not_before REAL, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, started_at REAL, finished_at REAL, UNIQUE(location, relpath));
CREATE INDEX IF NOT EXISTS items_queue ON items(status, kind, priority, size DESC);
CREATE TABLE IF NOT EXISTS conversions(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
  location TEXT NOT NULL, relpath TEXT NOT NULL, kind TEXT NOT NULL,
  bytes_before INTEGER NOT NULL, bytes_after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, name TEXT NOT NULL, started REAL,
  finished REAL, ok INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS snapshots(ts REAL NOT NULL, location TEXT NOT NULL,
  total_bytes INTEGER, free_bytes INTEGER);
CREATE TABLE IF NOT EXISTS folders(location TEXT NOT NULL, folder TEXT NOT NULL, bytes INTEGER,
  files INTEGER, compressed_files INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

ITEM_COLUMNS = {
    "location", "relpath", "size", "mtime", "kind", "source", "priority", "status", "reason",
    "settings", "dest_location", "dest_dir", "final_relpath", "final_size", "was_online_only",
    "not_before", "attempts", "error", "started_at", "finished_at",
}
TERMINAL = ("done", "skipped", "rejected", "error")


def open_db(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: autocommit, transactions are explicit (see _tx).
    con = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


@contextmanager
def _tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def upsert_candidate(con, *, location, relpath, size, mtime, kind, source, priority, settings,
                     dest_location=None, dest_dir=None) -> str:
    with _tx(con):
        row = con.execute("SELECT id, size, mtime, status, settings FROM items "
                          "WHERE location=? AND relpath=?", (location, relpath)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO items(location, relpath, size, mtime, kind, source, priority, "
                "settings, dest_location, dest_dir) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (location, relpath, size, mtime, kind, source, priority, settings,
                 dest_location, dest_dir))
            return "queued"
        same_file = row["size"] == size and row["mtime"] == mtime
        status = row["status"]
        if status in ("running", "published"):
            return "known"
        if status == "queued":
            if same_file:
                return "known"
            con.execute("UPDATE items SET size=?, mtime=? WHERE id=?", (size, mtime, row["id"]))
            return "requeued"
        if same_file and (status not in ("skipped", "rejected") or row["settings"] == settings):
            return "known"
        con.execute(
            "UPDATE items SET status='queued', reason=NULL, error=NULL, final_relpath=NULL, "
            "final_size=NULL, not_before=NULL, size=?, mtime=?, settings=?, source=?, "
            "priority=?, dest_location=?, dest_dir=? WHERE id=?",
            (size, mtime, settings, source, priority, dest_location, dest_dir, row["id"]))
        return "requeued"


def adopt_moved(con, *, location, relpath, size, mtime, exists: Callable[[str], bool]) -> bool:
    base = relpath.rsplit("/", 1)[-1]
    marks = ",".join("?" * len(TERMINAL))
    candidates = con.execute(
        f"SELECT id, relpath FROM items WHERE location=? AND size=? AND mtime=? "
        f"AND status IN ({marks}) AND relpath != ?",
        (location, size, mtime, *TERMINAL, relpath)).fetchall()
    for cand in candidates:
        if cand["relpath"].rsplit("/", 1)[-1] != base or exists(cand["relpath"]):
            continue
        try:
            with _tx(con):
                con.execute("UPDATE items SET relpath=? WHERE id=?", (relpath, cand["id"]))
        except sqlite3.IntegrityError:
            return False  # the new path already has its own row
        return True
    return False


def claim_next(con, *, kind: str, exclude_locations: Collection[str]):
    excluded = list(exclude_locations)
    marks = ",".join("?" * len(excluded))
    clause = (f" AND location NOT IN ({marks}) AND (dest_location IS NULL "
              f"OR dest_location NOT IN ({marks}))") if excluded else ""
    with _tx(con):
        row = con.execute(
            "SELECT id FROM items WHERE status='queued' AND kind=?" + clause +
            " ORDER BY priority ASC, size DESC, id ASC LIMIT 1",
            (kind, *excluded, *excluded)).fetchone()
        if row is None:
            return None
        con.execute("UPDATE items SET status='running', attempts=attempts+1, started_at=? "
                    "WHERE id=?", (time.time(), row["id"]))
        return con.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone()


def set_status(con, item_id: int, status: str, **fields) -> None:
    unknown = set(fields) - ITEM_COLUMNS
    if unknown or "status" in fields:
        raise ValueError(f"unknown item columns: {sorted(unknown | (set(fields) & {'status'}))}")
    cols = ["status", *fields]
    sets = ", ".join(f"{c}=?" for c in cols)
    with _tx(con):
        con.execute(f"UPDATE items SET {sets} WHERE id=?", (status, *fields.values(), item_id))


def due_published(con, now: float) -> list:
    return con.execute("SELECT * FROM items WHERE status='published' AND not_before <= ? "
                       "ORDER BY id", (now,)).fetchall()


def reset_running(con) -> int:
    with _tx(con):
        return con.execute("UPDATE items SET status='queued' WHERE status='running'").rowcount


def retry_errors(con) -> int:
    with _tx(con):
        return con.execute("UPDATE items SET status='queued', error=NULL "
                           "WHERE status='error'").rowcount


def delete_item(con, item_id: int) -> None:
    with _tx(con):
        con.execute("DELETE FROM items WHERE id=?", (item_id,))


def prune_missing(con, *, location: str, under: str, seen: set[str]) -> int:
    with _tx(con):
        rows = con.execute("SELECT id, relpath FROM items WHERE location=? "
                           "AND status NOT IN ('running','published')", (location,)).fetchall()
        gone = [r["id"] for r in rows
                if r["relpath"] not in seen
                and (not under or r["relpath"] == under or r["relpath"].startswith(under + "/"))]
        con.executemany("DELETE FROM items WHERE id=?", [(i,) for i in gone])
        return len(gone)


def log_conversion(con, *, ts, location, relpath, kind, bytes_before, bytes_after) -> None:
    with _tx(con):
        con.execute("INSERT INTO conversions(ts, location, relpath, kind, bytes_before, "
                    "bytes_after) VALUES(?,?,?,?,?,?)",
                    (ts, location, relpath, kind, bytes_before, bytes_after))


def meta_get(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def meta_set(con, key: str, value: str) -> None:
    with _tx(con):
        con.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def record_scan(con, *, name: str, started: float, finished: float, ok: bool,
                message: str) -> None:
    with _tx(con):
        con.execute("INSERT INTO scans(name, started, finished, ok, message) VALUES(?,?,?,?,?)",
                    (name, started, finished, int(ok), message))


def last_scan(con, name: str):
    return con.execute("SELECT * FROM scans WHERE name=? ORDER BY id DESC LIMIT 1",
                       (name,)).fetchone()


def add_snapshot(con, *, ts, location, total_bytes, free_bytes) -> None:
    with _tx(con):
        con.execute("INSERT INTO snapshots(ts, location, total_bytes, free_bytes) "
                    "VALUES(?,?,?,?)", (ts, location, total_bytes, free_bytes))


def replace_folders(con, location: str, rows: list[tuple[str, int, int, int]]) -> None:
    with _tx(con):
        con.execute("DELETE FROM folders WHERE location=?", (location,))
        con.executemany("INSERT INTO folders(location, folder, bytes, files, compressed_files) "
                        "VALUES(?,?,?,?,?)", [(location, *r) for r in rows])
