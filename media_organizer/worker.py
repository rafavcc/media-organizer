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
import time
from pathlib import Path
from typing import Callable

from . import db, rules
from .config import Config
from .convert import convert
from .locations import Location, MacUnreachable, NoSpace

log = logging.getLogger(__name__)
_finalize_lock = threading.Lock()
_post_step_lock = threading.Lock()
# held from choosing an output name through recording the plan, so two workers never pick one name
_name_lock = threading.Lock()
MTIME_TOLERANCE_S = 2.0
NO_SPACE_BACKOFF_S = 3600
OUTPUT_RATIO = 0.75  # an accepted output is at most this share of the original
FINALIZE_MAX_FAILS = 5
EVICT_GIVE_UP_S = 7 * 86400
_POST_STEP_STATUSES = ("done", "skipped", "rejected")
NAME_TAKEN_BACKOFF_S = 60
EVICT_BUDGET_S = 60.0
_NO_PLAN = {"final_relpath": None, "final_size": None, "final_mtime": None}
_REJECT_STATUSES = {"efficient": "skipped", "no-gain": "rejected", "low-quality": "rejected",
                    "multi-frame": "rejected", "error": "error"}


def _split(relpath: str) -> tuple[str, str]:
    folder, _, name = relpath.rpartition("/")
    return folder, name


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _planned_mtime(item) -> float:
    """The mtime the final file should have: the recorded stamp (rows from before the column
    existed were stamped with the original's mtime)."""
    return item["mtime"] if item["final_mtime"] is None else item["final_mtime"]


def _landed_is_ours(landed, item) -> bool:
    """A file at the recorded path is ours only if its size and mtime match what we planned."""
    return (landed is not None and landed.size == item["final_size"]
            and abs(landed.mtime - _planned_mtime(item)) <= MTIME_TOLERANCE_S)


def _taken_in(con, loc: Location, folder: str, item_id: int) -> Callable[[str], bool]:
    """Name is taken if the file exists or another live row plans to publish it. Names compare
    case-insensitively: NTFS and APFS would treat `A.webp` and `a.webp` as one file."""
    planned = db.planned_names(con, loc.name, folder, exclude_id=item_id)
    present = {n.casefold() for n in loc.list_names(folder)}
    return lambda n: (n.casefold() in planned or n.casefold() in present
                      or loc.exists(_join(folder, n)))


def _require_space(loc: Location, cfg: Config, work: Path, size: int) -> None:
    """Back off (NoSpace) before fetching when the destination or the work folder is too full."""
    min_free = int(cfg.min_free_gb * 1024 ** 3)
    if loc.free_bytes() < size * OUTPUT_RATIO + min_free:
        raise NoSpace(f"{loc.name}: not enough free space for {size} bytes")
    if shutil.disk_usage(work).free < 2 * size + min_free:  # the fetched copy plus the encode
        raise NoSpace(f"work folder: not enough free space for {size} bytes")


def process_item(con, item: sqlite3.Row, locs: dict[str, Location], cfg: Config, work: Path,
                 on_progress: Callable[[float], None] | None = None,
                 is_up: Callable[[str], bool] | None = None) -> str:
    work.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=work))
    try:
        if item["dest_location"]:
            return _process_download(con, item, locs, cfg, tmp, on_progress, is_up)
        return _process_library(con, item, locs, cfg, tmp, on_progress, is_up)
    except MacUnreachable:
        db.set_status(con, item["id"], "queued")
        raise
    except NoSpace:
        db.set_status(con, item["id"], "queued", reason="no-space", **_NO_PLAN,
                      not_before=time.time() + NO_SPACE_BACKOFF_S)
        return "queued"
    except FileExistsError:
        # another file took the planned name: plan again with a fresh look at the folder
        db.set_status(con, item["id"], "queued", reason="name-taken", **_NO_PLAN,
                      not_before=time.time() + NAME_TAKEN_BACKOFF_S)
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
                      **_NO_PLAN)  # a landed copy is of the old content
        return "queued"
    return None


def _fetch(loc: Location, item, tmp: Path) -> tuple[Path, str]:
    name = _split(item["relpath"])[1]
    local = tmp / name
    loc.fetch(item["relpath"], local)
    return local, name


def _process_library(con, item, locs, cfg, tmp, on_progress, is_up=None) -> str:
    loc = locs[item["location"]]
    early = _check_source(con, item, loc)
    if early:
        return early
    delay = cfg.mac_delete_delay_s if loc.is_mac else 0
    if item["final_relpath"] and item["final_size"] is not None:
        landed = loc.stat(item["final_relpath"])
        if _landed_is_ours(landed, item):
            # an earlier attempt published this file but was cut off before recording it
            db.set_status(con, item["id"], "published", final_mtime=landed.mtime,
                          not_before=loc.now() + delay)
            return _after_published(con, item, loc, cfg, is_up)
        db.set_status(con, item["id"], "running", **_NO_PLAN)
    _require_space(loc, cfg, tmp.parent, item["size"])
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
    expected = outcome.out.stat().st_size
    stamp = float(int(time.time()))  # converted files carry the conversion time
    with _name_lock:
        final = rules.final_name(name, rules.out_ext(name),
                                 _taken_in(con, loc, folder, item["id"]))
        final_rel = _join(folder, final)
        # record the plan first: if the publish lands but is not recorded, the retry finds it
        db.set_status(con, item["id"], "running", final_relpath=final_rel, final_size=expected,
                      final_mtime=stamp)
    size = loc.publish(outcome.out, final_rel, mtime=stamp,
                       min_free=int(cfg.min_free_gb * 1024 ** 3))
    landed = loc.stat(final_rel)  # a filesystem may round or ignore the stamp: keep what it did
    db.set_status(con, item["id"], "published", final_relpath=final_rel, final_size=size,
                  final_mtime=stamp if landed is None else landed.mtime,
                  not_before=loc.now() + delay)
    return _after_published(con, item, loc, cfg, is_up)


def _after_published(con, item, loc: Location, cfg: Config,
                     is_up: Callable[[str], bool] | None = None) -> str:
    if not loc.is_mac:
        # only this item's own location: a worker thread must not touch another (Mac) location,
        # and failures here are not counted (the tick counts at most one per pass)
        _finalize(con, {loc.name: loc}, is_up, count_failures=False)
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
    with _post_step_lock, _name_lock:
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


def _process_download(con, item, locs, cfg, tmp, on_progress, is_up=None) -> str:
    src_loc = locs[item["location"]]
    dest = locs[item["dest_location"]]
    early = _check_source(con, item, src_loc)
    if early:
        return early
    if not _loc_up(dest, is_up):
        db.set_status(con, item["id"], "queued")
        raise MacUnreachable(f"{dest.name} is not reachable")
    if item["final_relpath"] and item["final_size"] is not None:
        landed = dest.stat(item["final_relpath"])
        if _landed_is_ours(landed, item):
            # an earlier attempt published this copy but was cut off before the source was deleted
            # converted output is at most 75% of the source; an unconverted payload is equal
            return _finish_download(con, item, src_loc, dest, item["final_relpath"],
                                    landed.size, converted=item["final_size"] < item["size"])
        db.set_status(con, item["id"], "running", **_NO_PLAN)
    _require_space(dest, cfg, tmp.parent, item["size"])
    local, name = _fetch(src_loc, item, tmp)
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
    expected = payload.stat().st_size
    stamp = float(int(time.time())) if converted else item["mtime"]
    with _name_lock:
        taken = _taken_in(con, dest, item["dest_dir"], item["id"])
        if converted:
            out_name = rules.final_name(name, rules.out_ext(name), taken)
        else:
            out_name = rules.free_name(name, taken)
        dest_rel = _join(item["dest_dir"], out_name)
        db.set_status(con, item["id"], "running", final_relpath=dest_rel, final_size=expected,
                      final_mtime=stamp)
    size = dest.publish(payload, dest_rel, mtime=stamp,
                        min_free=int(cfg.min_free_gb * 1024 ** 3))
    landed = dest.stat(dest_rel)
    if size != expected or landed is None or landed.size != expected:
        raise OSError(f"published {dest_rel} does not have the expected size {expected}")
    db.set_status(con, item["id"], "running", final_mtime=landed.mtime)
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


def _loc_up(loc: Location, is_up: Callable[[str], bool] | None) -> bool:
    """Whether a location can be used now. A worker thread must not call `reachable()` of a Mac
    location (it can mount and unmount), so the service passes its cached answer as `is_up`."""
    if not loc.is_mac:
        return True
    return loc.reachable() if is_up is None else bool(is_up(loc.name))


def finalize_published(con, locs: dict[str, Location], cfg: Config,
                       is_up: Callable[[str], bool] | None = None,
                       count_failures: bool = True, evictions: bool = True) -> int:
    """Delete the original of every published row that is due and verified; also retry any
    pending evictions and any intake post-step that failed earlier. Returns the number of
    originals deleted."""
    if evictions:
        retry_evictions(con, locs, cfg, is_up=is_up)
    deleted = _finalize(con, locs, is_up, count_failures=count_failures)
    _retry_post_steps(con, locs, cfg)
    return deleted


def _finalize(con, locs: dict[str, Location], is_up: Callable[[str], bool] | None = None,
              count_failures: bool = True) -> int:
    deleted = 0
    for loc in locs.values():
        due = [r for r in db.due_published(con, loc.now()) if r["location"] == loc.name]
        if not due:
            continue
        if not _loc_up(loc, is_up):
            continue
        for stale in due:
            try:
                deleted += _finalize_one(con, loc, stale["id"])
            except MacUnreachable:
                break
            except OSError as e:
                log.exception("could not finalize %s", stale["relpath"])
                if count_failures:
                    _record_finalize_failure(con, stale["id"], e)
    return deleted


def _record_finalize_failure(con, item_id: int, exc: Exception) -> None:
    fails = db.bump_finalize_fails(con, item_id)
    if fails >= FINALIZE_MAX_FAILS:
        # the replacement stays where it is; retry-errors finalizes again through the plan shortcut
        row = con.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        if row is not None and row["status"] == "published":
            db.set_status(con, item_id, "error", error=f"could not delete original: {exc}")


def _try_evict(loc: Location, relpath: str, raise_unreachable: bool = False) -> bool:
    """True only when the evict succeeded and the file is now online-only."""
    try:
        return bool(loc.evict(relpath)) and bool(loc.is_online_only(relpath))
    except MacUnreachable:
        if raise_unreachable:
            raise
        return False


def retry_evictions(con, locs: dict[str, Location], cfg: Config, limit: int = 20,
                    now: float | None = None,
                    is_up: Callable[[str], bool] | None = None,
                    budget_s: float = EVICT_BUDGET_S) -> int:
    """Release converted online-only files from the Mac disk again; returns how many were
    released. Rows older than seven days give up (the flag is cleared, nothing else changes).
    Stops for this tick at the first unreachable Mac and after `budget_s` seconds."""
    rows = con.execute("SELECT * FROM items WHERE evict_pending=1 AND status='done' "
                       "ORDER BY finished_at ASC, id ASC").fetchall()
    released = tried = 0
    started = time.monotonic()
    for row in rows:
        if tried >= limit or time.monotonic() - started > budget_s:
            break
        loc = locs.get(row["location"])
        if loc is None or not _loc_up(loc, is_up):
            continue
        tried += 1
        when = loc.now() if now is None else now
        if not row["final_relpath"]:
            db.set_status(con, row["id"], "done", evict_pending=0)
        elif row["finished_at"] is not None and when - row["finished_at"] > EVICT_GIVE_UP_S:
            log.warning("giving up releasing %s from the Mac disk after 7 days",
                        row["final_relpath"])
            db.set_status(con, row["id"], "done", evict_pending=0)
        else:
            try:
                if _try_evict(loc, row["final_relpath"], raise_unreachable=True):
                    db.set_status(con, row["id"], "done", evict_pending=0)
                    released += 1
            except MacUnreachable:
                break
    return released


def _finalize_one(con, loc: Location, item_id: int) -> int:
    with _finalize_lock:
        item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None or item["status"] != "published":
            return 0
        final = loc.stat(item["final_relpath"])
        if (final is None or final.size != item["final_size"]
                or abs(final.mtime - _planned_mtime(item)) > MTIME_TOLERANCE_S):
            log.warning("LOST %s disappeared or changed after conversion; keeping %s",
                        item["final_relpath"], item["relpath"])
            db.set_status(con, item_id, "queued", **_NO_PLAN, not_before=None,
                          evict_pending=0, finalize_fails=0)
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
        db.set_status(con, item_id, "done", finished_at=loc.now(), finalize_fails=0,
                      evict_pending=int(bool(item["was_online_only"])))
        db.log_conversion(con, ts=loc.now(), location=item["location"],
                          relpath=item["relpath"], kind=item["kind"],
                          bytes_before=item["size"], bytes_after=item["final_size"])
        if item["was_online_only"] and _try_evict(loc, item["final_relpath"]):
            db.set_status(con, item_id, "done", evict_pending=0)
        return 1
