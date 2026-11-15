"""Process one queued item end to end: fetch, convert, publish, delete the original.

Safety rule: an original is deleted only after its replacement exists at the final path with
the expected size and the original still has the size and mtime recorded when it was read.
"""
from __future__ import annotations

import logging
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
_post_step_lock = threading.Lock()
MTIME_TOLERANCE_S = 2.0
_POST_STEP_STATUSES = ("done", "skipped", "rejected")
_REJECT_STATUSES = {"efficient": "skipped", "no-gain": "rejected", "low-quality": "rejected",
                    "error": "error"}


def _split(relpath: str) -> tuple[str, str]:
    folder, _, name = relpath.rpartition("/")
    return folder, name


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _landed_is_ours(landed, item) -> bool:
    """A file at the recorded path is ours only if its size and mtime match what we planned
    (`publish` stamps the final file with the source's mtime)."""
    return (landed is not None and landed.size == item["final_size"]
            and abs(landed.mtime - item["mtime"]) <= MTIME_TOLERANCE_S)


def _taken_in(con, loc: Location, folder: str, item_id: int) -> Callable[[str], bool]:
    """Name is taken if the file exists or another live row plans to publish it."""
    planned = db.planned_names(con, loc.name, folder, exclude_id=item_id)
    return lambda n: n in planned or loc.exists(_join(folder, n))


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
        db.set_status(con, item["id"], "queued", reason="no-space", final_relpath=None,
                      final_size=None)
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
        db.set_status(con, item["id"], "queued", size=entry.size, mtime=entry.mtime,
                      final_relpath=None, final_size=None)  # a landed copy is of the old content
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
    delay = cfg.mac_delete_delay_s if loc.is_mac else 0
    if item["final_relpath"] and item["final_size"] is not None:
        landed = loc.stat(item["final_relpath"])
        if _landed_is_ours(landed, item):
            # an earlier attempt published this file but was cut off before recording it
            db.set_status(con, item["id"], "published", not_before=loc.now() + delay)
            return _after_published(con, item, locs, loc, cfg)
        db.set_status(con, item["id"], "running", final_relpath=None, final_size=None)
    # once online-only, always: a retry finds the file already downloaded by the first fetch
    online_only = bool(item["was_online_only"]) or loc.is_online_only(item["relpath"])
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
    final = rules.final_name(name, rules.out_ext(name), _taken_in(con, loc, folder, item["id"]))
    final_rel = _join(folder, final)
    expected = outcome.out.stat().st_size
    # record the plan first: if the publish lands but is not recorded, the retry finds it
    db.set_status(con, item["id"], "running", final_relpath=final_rel, final_size=expected)
    size = loc.publish(outcome.out, final_rel, mtime=item["mtime"],
                       min_free=int(cfg.min_free_gb * 1024 ** 3))
    db.set_status(con, item["id"], "published", final_relpath=final_rel, final_size=size,
                  not_before=loc.now() + delay)
    return _after_published(con, item, locs, loc, cfg)


def _after_published(con, item, locs, loc: Location, cfg: Config) -> str:
    if not loc.is_mac:
        _finalize(con, locs)
    status = con.execute("SELECT status FROM items WHERE id=?", (item["id"],)).fetchone()["status"]
    return _intake_post_step(con, item, loc, cfg, status)


def _under(relpath: str, folder: str) -> bool:
    return relpath.startswith(folder + "/")


def _intake_post_step(con, item, loc: Location, cfg: Config, status: str) -> str:
    if loc.name != "d" or item["source"] != "intake" or status not in _POST_STEP_STATUSES:
        return status
    try:
        _move_to_converted(con, item["id"], loc, cfg)
    except Exception:
        # never change the item's status: the original may already be deleted
        log.exception("moving the result of %s to %s failed", item["relpath"], cfg.d_converted)
        row = con.execute("SELECT status FROM items WHERE id=?", (item["id"],)).fetchone()
        if row is not None:
            db.set_status(con, item["id"], row["status"], reason="post-step-failed")
    return status


def _move_to_converted(con, item_id: int, loc: Location, cfg: Config) -> None:
    with _post_step_lock:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None or not _under(row["relpath"], cfg.d_intake):
            return  # already moved
        current = (row["final_relpath"] if row["status"] == "done" and row["final_relpath"]
                   else row["relpath"])
        name = _split(current)[1]
        target = rules.free_name(name, _taken_in(con, loc, cfg.d_converted, item_id))
        new_rel = _join(cfg.d_converted, target)
        loc.move_within(current, new_rel)
        db.rename_item(con, item_id, new_rel)
        if row["reason"] == "post-step-failed":
            db.set_status(con, item_id, row["status"], reason=None)
        db.meta_set(con, "sorter_dirty", "1")


def _retry_post_steps(con, locs: dict[str, Location], cfg: Config) -> None:
    loc = locs.get("d")
    if loc is None:
        return
    marks = ",".join("?" * len(_POST_STEP_STATUSES))
    rows = con.execute(f"SELECT * FROM items WHERE location='d' AND source='intake' "
                       f"AND status IN ({marks})", _POST_STEP_STATUSES).fetchall()
    for row in rows:
        if not _under(row["relpath"], cfg.d_intake):
            continue
        current = (row["final_relpath"] if row["status"] == "done" and row["final_relpath"]
                   else row["relpath"])
        if loc.exists(current):
            _intake_post_step(con, row, loc, cfg, row["status"])


def _process_download(con, item, locs, cfg, tmp, on_progress) -> str:
    src_loc = locs[item["location"]]
    dest = locs[item["dest_location"]]
    early = _check_source(con, item, src_loc)
    if early:
        return early
    if not dest.reachable():
        db.set_status(con, item["id"], "queued")
        raise MacUnreachable(f"{dest.name} is not reachable")
    if item["final_relpath"] and item["final_size"] is not None:
        landed = dest.stat(item["final_relpath"])
        if _landed_is_ours(landed, item):
            # an earlier attempt published this copy but was cut off before the source was deleted
            # converted output is at most 75% of the source; an unconverted payload is equal
            return _finish_download(con, item, src_loc, dest, item["final_relpath"],
                                    landed.size, converted=item["final_size"] < item["size"])
        db.set_status(con, item["id"], "running", final_relpath=None, final_size=None)
    local, name = _fetch(src_loc, item, tmp)
    taken = _taken_in(con, dest, item["dest_dir"], item["id"])
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
    db.set_status(con, item["id"], "running", final_relpath=dest_rel, final_size=expected)
    size = dest.publish(payload, dest_rel, mtime=item["mtime"],
                        min_free=int(cfg.min_free_gb * 1024 ** 3))
    landed = dest.stat(dest_rel)
    if size != expected or landed is None or landed.size != expected:
        raise OSError(f"published {dest_rel} does not have the expected size {expected}")
    return _finish_download(con, item, src_loc, dest, dest_rel, size, converted)


def _finish_download(con, item, src_loc: Location, dest: Location, dest_rel: str, size: int,
                     converted: bool) -> str:
    """The copy is at its final path with the expected size; delete the source if unchanged."""
    current = src_loc.stat(item["relpath"])
    if current is None or (current.size, current.mtime) != (item["size"], item["mtime"]):
        db.set_status(con, item["id"], "rejected", reason="changed", finished_at=src_loc.now())
        return "rejected"
    src_loc.delete(item["relpath"])
    if converted:
        db.log_conversion(con, ts=dest.now(), location=dest.name, relpath=dest_rel,
                          kind=item["kind"], bytes_before=item["size"], bytes_after=size)
    db.delete_item(con, item["id"])
    return "done"


def finalize_published(con, locs: dict[str, Location], cfg: Config) -> int:
    """Delete the original of every published row that is due and verified; also retry any
    intake post-step that failed earlier. Returns the number of originals deleted."""
    deleted = _finalize(con, locs)
    _retry_post_steps(con, locs, cfg)
    return deleted


def _finalize(con, locs: dict[str, Location]) -> int:
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
