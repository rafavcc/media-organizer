"""Process one queued item end to end: fetch, convert, publish, delete the original.

Safety rule: an original is deleted only after its replacement exists at the final path with
the expected size and the original still has the size and mtime recorded when it was read.
"""
from __future__ import annotations

import logging
import posixpath
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Callable

from . import db, rules
from .config import Config
from .convert import convert
from .locations import Location, MacUnreachable, NoSpace

log = logging.getLogger(__name__)
_finalize_lock = threading.Lock()
_REJECT_STATUSES = {"efficient": "skipped", "no-gain": "rejected", "low-quality": "rejected",
                    "error": "error"}


def _split(relpath: str) -> tuple[str, str]:
    folder, _, name = relpath.rpartition("/")
    return folder, name


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def process_item(con, item: sqlite3.Row, locs: dict[str, Location], cfg: Config, work: Path,
                 on_progress: Callable[[float], None] | None = None) -> str:
    work.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=work))
    try:
        if item["dest_location"]:
            return _process_download(con, item, locs, cfg, tmp, on_progress)
        return _process_library(con, item, locs, cfg, tmp, on_progress)
    except MacUnreachable:
        db.set_status(con, item["id"], "queued")
        raise
    except NoSpace:
        db.set_status(con, item["id"], "queued", reason="no-space")
        return "queued"
    except Exception as e:
        log.exception("processing %s failed", item["relpath"])
        db.set_status(con, item["id"], "error", error=f"{type(e).__name__}: {e}")
        return "error"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_source(con, item, loc: Location) -> str | None:
    """Return a final result ("gone" / "queued") if the source is missing or changed."""
    entry = loc.stat(item["relpath"])
    if entry is None:
        db.delete_item(con, item["id"])
        return "gone"
    if entry.size != item["size"] or entry.mtime != item["mtime"]:
        db.set_status(con, item["id"], "queued", size=entry.size, mtime=entry.mtime)
        return "queued"
    return None


def _fetch(loc: Location, item, tmp: Path) -> tuple[Path, str]:
    name = _split(item["relpath"])[1]
    local = tmp / name
    loc.fetch(item["relpath"], local)
    return local, name


def _process_library(con, item, locs, cfg, tmp, on_progress) -> str:
    loc = locs[item["location"]]
    early = _check_source(con, item, loc)
    if early:
        return early
    online_only = loc.is_online_only(item["relpath"])
    db.set_status(con, item["id"], "running", was_online_only=int(online_only))
    local, name = _fetch(loc, item, tmp)
    outcome = convert(local, name, item["size"], tmp, cfg, on_progress)

    if outcome.status != "ok":
        status = _REJECT_STATUSES[outcome.status]
        fields = {"reason": outcome.status, "finished_at": loc.now()}
        if status == "error":
            fields["error"] = outcome.message
        db.set_status(con, item["id"], status, **fields)
        if online_only:
            loc.evict(item["relpath"])
        return _intake_post_step(con, item, loc, cfg, status)

    folder = _split(item["relpath"])[0]
    final = rules.final_name(name, rules.out_ext(name),
                             lambda n: loc.exists(_join(folder, n)))
    final_rel = _join(folder, final)
    size = loc.publish(outcome.out, final_rel, mtime=item["mtime"],
                       min_free=int(cfg.min_free_gb * 1024 ** 3))
    delay = cfg.mac_delete_delay_s if loc.is_mac else 0
    db.set_status(con, item["id"], "published", final_relpath=final_rel, final_size=size,
                  not_before=loc.now() + delay)
    if not loc.is_mac:
        finalize_published(con, locs, cfg)
    status = con.execute("SELECT status FROM items WHERE id=?", (item["id"],)).fetchone()["status"]
    return _intake_post_step(con, item, loc, cfg, status)


def _intake_post_step(con, item, loc: Location, cfg: Config, status: str) -> str:
    if loc.name != "d" or item["source"] != "intake" or status not in ("done", "skipped",
                                                                          "rejected"):
        return status
    row = con.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone()
    current = row["final_relpath"] if status == "done" and row["final_relpath"] else row["relpath"]
    name = _split(current)[1]
    target = rules.free_name(name, lambda n: loc.exists(_join(cfg.d_converted, n)))
    new_rel = _join(cfg.d_converted, target)
    loc.move_within(current, new_rel)
    try:
        with db._tx(con):
            con.execute("UPDATE items SET relpath=? WHERE id=?", (new_rel, item["id"]))
    except sqlite3.IntegrityError:
        db.delete_item(con, item["id"])  # a stale row already owns the new path
    db.meta_set(con, "sorter_dirty", "1")
    return status


def _process_download(con, item, locs, cfg, tmp, on_progress) -> str:
    src_loc = locs[item["location"]]
    dest = locs[item["dest_location"]]
    early = _check_source(con, item, src_loc)
    if early:
        return early
    if not dest.reachable():
        db.set_status(con, item["id"], "queued")
        raise MacUnreachable(f"{dest.name} is not reachable")
    local, name = _fetch(src_loc, item, tmp)
    taken = lambda n: dest.exists(_join(item["dest_dir"], n))
    converted = False
    payload, out_name = local, None
    if not name.lower().endswith(".webp"):
        outcome = convert(local, name, item["size"], tmp, cfg, on_progress)
        if outcome.status == "error":
            db.set_status(con, item["id"], "error", reason="error", error=outcome.message,
                          finished_at=src_loc.now())
            return "error"
        if outcome.status == "ok":
            converted, payload = True, outcome.out
            out_name = rules.final_name(name, rules.out_ext(name), taken)
    if out_name is None:
        out_name = rules.free_name(name, taken)
    dest_rel = _join(item["dest_dir"], out_name)
    expected = payload.stat().st_size
    size = dest.publish(payload, dest_rel, mtime=item["mtime"],
                        min_free=int(cfg.min_free_gb * 1024 ** 3))
    landed = dest.stat(dest_rel)
    if size != expected or landed is None or landed.size != expected:
        raise OSError(f"published {dest_rel} does not have the expected size {expected}")
    now = src_loc.stat(item["relpath"])
    if now is None or (now.size, now.mtime) != (item["size"], item["mtime"]):
        db.set_status(con, item["id"], "rejected", reason="changed", finished_at=src_loc.now())
        return "rejected"
    src_loc.delete(item["relpath"])
    if converted:
        db.log_conversion(con, ts=dest.now(), location=dest.name, relpath=dest_rel,
                          kind=item["kind"], bytes_before=item["size"], bytes_after=size)
    db.delete_item(con, item["id"])
    return "done"


def finalize_published(con, locs: dict[str, Location], cfg: Config) -> int:
    """Delete the original of every published row that is due and verified. Returns the count."""
    deleted = 0
    for loc in locs.values():
        due = [r for r in db.due_published(con, loc.now()) if r["location"] == loc.name]
        if not due:
            continue
        if loc.is_mac and not loc.reachable():
            continue
        for stale in due:
            try:
                deleted += _finalize_one(con, loc, stale["id"])
            except MacUnreachable:
                break
            except OSError:
                log.exception("could not finalize %s", stale["relpath"])
    return deleted


def _finalize_one(con, loc: Location, item_id: int) -> int:
    with _finalize_lock:
        item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None or item["status"] != "published":
            return 0
        final = loc.stat(item["final_relpath"])
        if final is None or final.size != item["final_size"]:
            log.warning("LOST %s disappeared or changed after conversion; keeping %s",
                        item["final_relpath"], item["relpath"])
            db.set_status(con, item_id, "queued", final_relpath=None, final_size=None,
                          not_before=None)
            return 0
        orig = loc.stat(item["relpath"])
        if orig is None:
            db.set_status(con, item_id, "done", finished_at=loc.now())
            return 0
        if (orig.size, orig.mtime) != (item["size"], item["mtime"]):
            log.warning("CHANGED %s was modified after conversion; keeping both files",
                        item["relpath"])
            db.set_status(con, item_id, "rejected", reason="changed", finished_at=loc.now())
            return 0
        loc.delete(item["relpath"])
        db.set_status(con, item_id, "done", finished_at=loc.now())
        db.log_conversion(con, ts=loc.now(), location=item["location"],
                          relpath=item["relpath"], kind=item["kind"],
                          bytes_before=item["size"], bytes_after=item["final_size"])
        if item["was_online_only"]:
            loc.evict(item["final_relpath"])
        return 1
