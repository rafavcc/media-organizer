"""Scanners: list library files and queue work, sweep Downloads, refresh folder statistics."""
from __future__ import annotations

import logging
import posixpath
import time
from dataclasses import dataclass, field

from . import db, rules
from .config import Config
from .locations import PART_SUFFIX, Location, NoSpace
from .probe import probe_video

JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
log = logging.getLogger(__name__)
PRIORITY = {"downloads": 0, "intake": 1, "mega": 2, "d": 3}
STALE_PART_S = 24 * 3600
GIB = 1024 ** 3


@dataclass
class ScanResult:
    found: int = 0
    queued: int = 0
    known: int = 0
    adopted: int = 0
    moved: int = 0
    waiting: int = 0
    suspect: bool = False  # full scan walked nothing although rows exist: left untouched
    nfo_deleted: int = 0
    dirs_removed: int = 0
    would_move: list[tuple[str, str]] = field(default_factory=list)  # dry run only
    would_delete: list[str] = field(default_factory=list)  # dry run only: .nfo files
    would_remove: list[str] = field(default_factory=list)  # dry run only: empty folders


def _intake_folder(loc: Location, cfg: Config) -> str | None:
    return {"d": cfg.d_intake, "mega": cfg.mega_intake}.get(loc.name)


def _old_enough(loc: Location, entry, cfg: Config) -> bool:
    return loc.now() - entry.mtime >= cfg.min_age_min * 60


def _row_id(con, location: str, relpath: str) -> int:
    return con.execute("SELECT id FROM items WHERE location=? AND relpath=?",
                       (location, relpath)).fetchone()["id"]


def _skip_if_efficient(con, loc: Location, cfg: Config, relpath: str) -> bool:
    info = probe_video(loc.path(relpath))
    if info is None:
        return False
    bpp = rules.bits_per_pixel(info.bitrate, info.width, info.height, info.fps_value)
    if rules.video_decision(info.codec, bpp, cfg) != "efficient":
        return False
    db.set_status(con, _row_id(con, loc.name, relpath), "skipped", reason="efficient")
    return True


def _has_rows(con, location: str, under: str) -> bool:
    if not under:
        return con.execute("SELECT 1 FROM items WHERE location=? LIMIT 1",
                           (location,)).fetchone() is not None
    return con.execute("SELECT 1 FROM items WHERE location=? AND (relpath=? OR "
                       "substr(relpath, 1, ?)=?) LIMIT 1",
                       (location, under, len(under) + 1, under + "/")).fetchone() is not None


def scan_library(con, loc: Location, cfg: Config, *, sub: str = "", full: bool,
                 dry_run: bool = False) -> ScanResult:
    """With dry_run, nothing is deleted or pruned and no folder statistics are written; candidate
    rows still go to `con`, so the caller must pass a throwaway database."""
    res = ScanResult()
    if not loc.root.exists():
        return res
    entries = 0
    intake = _intake_folder(loc, cfg)
    seen: set[str] = set()
    skipped: list[str] = []
    for entry in loc.walk(sub, skipped=skipped):
        entries += 1
        name = posixpath.basename(entry.relpath)
        if full and name.endswith(".mo-part"):
            if not dry_run and loc.now() - entry.mtime > STALE_PART_S:
                try:
                    loc.delete(entry.relpath)
                except FileNotFoundError:
                    pass
            continue
        if not rules.is_candidate(name):
            continue
        seen.add(entry.relpath)  # even if too young, so that prune keeps its row
        if entry.size == 0 or not _old_enough(loc, entry, cfg):
            continue
        res.found += 1
        in_intake = intake is not None and (entry.relpath == intake
                                            or entry.relpath.startswith(intake + "/"))
        has_row = con.execute("SELECT 1 FROM items WHERE location=? AND relpath=?",
                              (loc.name, entry.relpath)).fetchone()
        if not has_row and db.adopt_moved(con, location=loc.name, relpath=entry.relpath,
                                          size=entry.size, mtime=entry.mtime, exists=loc.exists):
            res.adopted += 1
        kind = rules.kind_of(name)
        outcome = db.upsert_candidate(
            con, location=loc.name, relpath=entry.relpath, size=entry.size, mtime=entry.mtime,
            kind=kind, source="intake" if in_intake else "library",
            priority=PRIORITY["intake"] if in_intake else PRIORITY[loc.name],
            settings=cfg.settings)
        if outcome == "known":
            res.known += 1
            continue
        if kind == "video" and not loc.is_mac and _skip_if_efficient(con, loc, cfg, entry.relpath):
            continue
        res.queued += 1
    if full and entries == 0 and _has_rows(con, loc.name, sub):
        res.suspect = True  # an unmounted drive looks like an empty folder: prune nothing
        return res
    for folder in skipped:  # an unreadable folder says nothing about the rows under it
        seen.update(r["relpath"] for r in con.execute(
            "SELECT relpath FROM items WHERE location=? AND substr(relpath, 1, ?)=?",
            (loc.name, len(folder) + 1, folder + "/")))
    if full and not dry_run:
        db.prune_missing(con, location=loc.name, under=sub, seen=seen)
        refresh_folders(con, loc)
    return res


def scan_downloads(con, loc: Location, locs: dict[str, Location], cfg: Config, *,
                   dry_run: bool = False) -> ScanResult:
    res = ScanResult()
    if not loc.root.exists():
        return res
    dest = locs["d"]
    for entry in loc.walk():
        name = posixpath.basename(entry.relpath)
        if name.lower().endswith(".nfo"):
            if _old_enough(loc, entry, cfg):
                _delete_nfo(loc, entry, res, dry_run)
            continue
        route = rules.route_download(name, entry.size, cfg)
        if route is None or entry.size == 0 or not _old_enough(loc, entry, cfg):
            continue
        res.found += 1
        if route == "d":
            _move_to_d(loc, dest, entry, cfg, res, dry_run)
            continue
        outcome = db.upsert_candidate(
            con, location=loc.name, relpath=entry.relpath, size=entry.size, mtime=entry.mtime,
            kind=rules.kind_of(entry.relpath) or "image", source="downloads",
            priority=PRIORITY["downloads"], settings=cfg.settings,
            dest_location="mega", dest_dir=cfg.mega_intake)
        if outcome == "known":
            res.known += 1
        else:
            res.queued += 1
    _remove_empty_folders(loc, cfg, res, dry_run)
    return res


def _delete_nfo(loc: Location, entry, res: ScanResult, dry_run: bool) -> None:
    if dry_run:
        res.would_delete.append(entry.relpath)
        return
    try:
        loc.delete(entry.relpath)
    except FileNotFoundError:
        return
    log.info("deleted %s %s", loc.name, entry.relpath)
    res.nfo_deleted += 1


def _remove_empty_folders(loc: Location, cfg: Config, res: ScanResult, dry_run: bool) -> None:
    """Bottom-up, with rmdir only. Folder mtimes are read now, after the files were handled."""
    for folder in loc.walk_dirs():
        if not _old_enough(loc, folder, cfg):
            continue
        try:
            names = loc.list_names(folder.relpath)
        except OSError:
            continue  # cannot be read: not known to be empty
        junk = [n for n in names if n.lower() in JUNK_NAMES]
        if len(junk) != len(names):
            continue  # holds something else
        try:
            plain = all(loc.is_plain_file(f"{folder.relpath}/{n}") for n in junk)
        except OSError:
            continue
        if not plain:
            continue  # a folder or link named like junk: delete nothing
        if dry_run:
            res.would_remove.append(folder.relpath)
            continue
        try:
            for n in junk:
                loc.delete(f"{folder.relpath}/{n}")
            loc.rmdir(folder.relpath)
        except OSError:  # MacUnreachable is not an OSError and propagates
            continue  # something appeared in it, or it is gone; the next sweep decides
        log.info("removed folder %s %s", loc.name, folder.relpath)
        res.dirs_removed += 1


def _move_to_d(loc: Location, dest: Location, entry, cfg: Config, res: ScanResult,
               dry_run: bool = False) -> None:
    if not dest.reachable():
        res.waiting += 1
        return
    name = rules.free_name(posixpath.basename(entry.relpath), lambda n: dest.exists(f"{cfg.d_intake}/{n}"))
    target = f"{cfg.d_intake}/{name}"
    if dry_run:
        res.would_move.append((entry.relpath, f"{dest.name}/{target}"))
        return
    try:
        written = dest.publish(loc.path(entry.relpath), target, entry.mtime,
                               int(cfg.min_free_gb * GIB))
    except NoSpace:
        res.waiting += 1
        return
    except OSError as e:  # e.g. the source grew during the copy; try again next scan
        if not isinstance(e, FileExistsError):  # that file is somebody else's
            _discard(dest, target)
        _discard(dest, target + PART_SUFFIX)
        res.waiting += 1
        return
    now = loc.stat(entry.relpath)
    if written != entry.size or now is None or (now.size, now.mtime) != (entry.size, entry.mtime):
        _discard(dest, target)  # the source changed after the walk; try again next scan
        res.waiting += 1
        return
    loc.delete(entry.relpath)
    res.moved += 1


def _discard(loc: Location, relpath: str) -> None:
    try:
        loc.delete(relpath)
    except FileNotFoundError:
        pass


def refresh_folders(con, loc: Location) -> None:
    if not loc.root.exists():
        return
    finished = {r["relpath"] for r in con.execute(
        "SELECT relpath FROM items WHERE location=? AND status IN ('done','skipped','rejected')",
        (loc.name,))}
    stats: dict[str, list[int]] = {}
    for entry in loc.walk(quiet=True):  # unreadable folders were logged by the scan itself
        folder = entry.relpath.split("/", 1)[0] if "/" in entry.relpath else "."
        row = stats.setdefault(folder, [0, 0, 0])
        row[0] += entry.size
        row[1] += 1
        name = posixpath.basename(entry.relpath)
        if rules.kind_of(name) and (rules.is_compressed_name(name) or entry.relpath in finished):
            row[2] += 1
    db.replace_folders(con, loc.name, [(f, *v) for f, v in sorted(stats.items())])
    db.add_snapshot(con, ts=time.time(), location=loc.name,
                    total_bytes=sum(v[0] for v in stats.values()), free_bytes=loc.free_bytes())
