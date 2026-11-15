"""Read-only summary statistics for the status command and the web dashboard."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta

from . import db
from .config import Config
from .service import SCANS

DAILY_DAYS = 30
ETA_WINDOW_DAYS = 3
FOLDER_LIMIT = 15
RECENT_LIMIT = 50
PROBLEM_ITEM_LIMIT = 100
STUCK_PUBLISHED_S = 3600
FINISHED = ("done", "skipped", "rejected", "error")


def _disk_usage(path):
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


def _freed(con) -> dict:
    out = {"d": 0, "mega": 0}
    for row in con.execute("SELECT location, SUM(bytes_before - bytes_after) AS freed "
                           "FROM conversions GROUP BY location"):
        if row["location"] in out:
            out[row["location"]] = int(row["freed"] or 0)
    return {"total": out["d"] + out["mega"], **out}


def _disk(con, cfg: Config) -> dict:
    usage = _disk_usage(cfg.d_root) if cfg.d_root.exists() else None
    snap = con.execute("SELECT free_bytes FROM snapshots WHERE location='d' "
                       "ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
    if usage:
        d_free = usage.free  # live; the snapshot only lags behind the weekly scan
    elif snap is not None:
        d_free = int(snap["free_bytes"] or 0)
    else:
        d_free = 0
    latest = con.execute("SELECT total_bytes FROM snapshots WHERE location='mega' "
                         "ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
    earliest = con.execute("SELECT total_bytes FROM snapshots WHERE location='mega' "
                           "ORDER BY ts ASC, rowid ASC LIMIT 1").fetchone()
    return {"d_free": d_free, "d_total": usage.total if usage else 0,
            "mega_bytes": int(latest["total_bytes"] or 0) if latest else 0,
            "mega_bytes_start": int(earliest["total_bytes"] or 0) if earliest else 0}


def _first_pass(con, location: str, since: float) -> dict:
    marks = ",".join("?" * len(FINISHED))
    row = con.execute(
        f"SELECT COUNT(*) AS total, COALESCE(SUM(status IN ({marks})), 0) AS done "
        "FROM items WHERE location=? AND source IN ('library','intake')",
        (*FINISHED, location)).fetchone()
    remaining = con.execute(
        "SELECT COALESCE(SUM(size), 0) AS n FROM items WHERE location=? AND status='queued' "
        "AND source IN ('library','intake')", (location,)).fetchone()["n"]
    recent = con.execute("SELECT COALESCE(SUM(bytes_before), 0) AS n FROM conversions "
                         "WHERE location=? AND ts >= ?", (location, since)).fetchone()["n"]
    per_day = recent / ETA_WINDOW_DAYS
    eta = remaining / per_day if remaining and per_day else None
    return {"done": int(row["done"]), "total": int(row["total"]), "eta_days": eta}


def _milestones(first_pass, disk, freed_total) -> list[dict]:
    out = []
    for prefix, loc in (("D:", "d"), ("MEGA", "mega")):
        fp = first_pass[loc]
        total, done = fp["total"], fp["done"]
        for pct in (25, 50, 75):
            out.append({"label": f"{prefix} first pass {pct}%",
                        "done": total > 0 and done * 100 >= pct * total})
        out.append({"label": f"{prefix} first pass done", "done": total > 0 and done == total})
    d_total = disk["d_total"]
    out.append({"label": "D: under 70% full",
                "done": d_total > 0 and (d_total - disk["d_free"]) / d_total < 0.70})
    for gb in (100, 250, 500):
        out.append({"label": f"{gb} GB freed", "done": freed_total >= gb * 1024 ** 3})
    return out


def _daily(con, generated: float) -> list[dict]:
    today = datetime.fromtimestamp(generated).date()
    days = [today - timedelta(days=n) for n in range(DAILY_DAYS - 1, -1, -1)]
    buckets = {d.isoformat(): {"date": d.isoformat(), "d": 0, "mega": 0} for d in days}
    start = datetime.combine(days[0], datetime.min.time()).timestamp()
    for row in con.execute("SELECT ts, location, bytes_before - bytes_after AS freed "
                           "FROM conversions WHERE ts >= ?", (start,)):
        bucket = buckets.get(datetime.fromtimestamp(row["ts"]).date().isoformat())
        if bucket is not None and row["location"] in ("d", "mega"):
            bucket[row["location"]] += int(row["freed"])
    return list(buckets.values())


def _current(con):
    try:
        value = json.loads(db.meta_get(con, "current") or "{}")
    except ValueError:
        return None
    return value if isinstance(value, dict) and value else None


def _queue(con, now: float) -> dict:
    def count(where: str, args=()) -> int:
        return con.execute(f"SELECT COUNT(*) AS n FROM items WHERE status='queued' AND {where}",
                           args).fetchone()["n"]
    return {"downloads": count("source='downloads'"), "intake": count("source='intake'"),
            "mega": count("source='library' AND location='mega'"),
            "library": count("source='library' AND location='d'"),
            "no_space": count("not_before > ? AND reason='no-space'", (now,)),
            "evict_pending": con.execute("SELECT COUNT(*) AS n FROM items WHERE evict_pending=1"
                                         ).fetchone()["n"]}


def _scans(con) -> list[dict]:
    out = []
    for scan in SCANS:
        last = db.last_scan(con, scan.name)
        if last is None or last["finished"] is None:
            out.append({"name": scan.name, "finished": None, "ok": None, "message": ""})
        else:
            out.append({"name": scan.name, "finished": last["finished"],
                        "ok": None if last["ok"] is None else bool(last["ok"]),
                        "message": last["message"] or ""})
    return out


def _folders(con, location: str) -> list[dict]:
    rows = con.execute("SELECT folder, bytes, files, compressed_files FROM folders "
                       "WHERE location=? ORDER BY bytes DESC LIMIT ?",
                       (location, FOLDER_LIMIT))
    return [dict(r) for r in rows]


def _problems(con, now: float) -> dict:
    out = {"error": con.execute("SELECT COUNT(*) AS n FROM items WHERE status='error'"
                                ).fetchone()["n"]}
    for reason in ("no-gain", "low-quality", "changed", "multi-frame"):
        out[reason] = con.execute("SELECT COUNT(*) AS n FROM items WHERE status='rejected' "
                                  "AND reason=?", (reason,)).fetchone()["n"]
    out["stuck_published"] = con.execute(
        "SELECT COUNT(*) AS n FROM items WHERE status='published' "
        "AND not_before < ?", (now - STUCK_PUBLISHED_S,)).fetchone()["n"]
    rows = con.execute("SELECT location, relpath, reason, error FROM items WHERE status='error' "
                       "ORDER BY finished_at DESC, id DESC LIMIT ?", (PROBLEM_ITEM_LIMIT,))
    out["items"] = [dict(r) for r in rows]
    return out


def summary(con, cfg: Config, now: float | None = None) -> dict:
    """Everything the status command and dashboard show. Plain SELECTs only."""
    generated = time.time() if now is None else now
    freed = _freed(con)
    disk = _disk(con, cfg)
    since = generated - ETA_WINDOW_DAYS * 86400
    first_pass = {loc: _first_pass(con, loc, since) for loc in ("d", "mega")}
    recent = con.execute("SELECT ts, location, relpath, bytes_before, bytes_after "
                         "FROM conversions ORDER BY ts DESC, id DESC LIMIT ?", (RECENT_LIMIT,))
    return {
        "generated": generated,
        "freed": freed,
        "disk": disk,
        "first_pass": first_pass,
        "milestones": _milestones(first_pass, disk, freed["total"]),
        "daily": _daily(con, generated),
        "now": _current(con),
        "paused": db.meta_get(con, "paused") == "1",
        "queue": _queue(con, generated),
        "scans": _scans(con),
        "mac_reachable": db.meta_get(con, "mac_reachable") == "1",
        "folders": {"d": _folders(con, "d"), "mega": _folders(con, "mega")},
        "recent": [dict(r) for r in recent],
        "problems": _problems(con, generated),
    }
