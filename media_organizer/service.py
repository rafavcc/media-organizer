"""The service: scheduler (which scans are due), worker threads, sorter hand-off, main loop."""
from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import db, worker
from .config import Config
from .locations import Location, MacUnreachable, build_locations
from .scanner import refresh_folders, scan_downloads, scan_library
from .worker import finalize_published

log = logging.getLogger(__name__)

TICK_S = 30
SNAPSHOT_EVERY_S = 3600  # history-chart points, however often the folders are recounted
IDLE_S = 15
MAC_CACHE_S = 60
CURRENT_EVERY_S = 10
JOIN_TIMEOUT_S = 20
INTAKE_BLOCKING = ("queued", "running", "published")


@dataclass(frozen=True)
class ScanDef:
    name: str
    location: str
    kind: str  # "intake" | "full" | "downloads"


SCANS: tuple[ScanDef, ...] = (
    ScanDef("d-intake", "d", "intake"),
    ScanDef("d-full", "d", "full"),
    ScanDef("mega-intake", "mega", "intake"),
    ScanDef("mega-full", "mega", "full"),
    ScanDef("win-downloads", "win_dl", "downloads"),
    ScanDef("mac-downloads", "mac_dl", "downloads"),
)


class Stopping(BaseException):
    """Raised from the progress callback to abort the running encode on shutdown.

    A BaseException so that process_item's `except Exception` does not mark the item as failed.
    """


def due_scans(con, cfg: Config, now: float) -> list[ScanDef]:
    due = []
    for scan in SCANS:
        last = db.last_scan(con, scan.name)
        if last is None:
            is_due = True
        else:
            hours = cfg.full_scan_interval_h if scan.kind == "full" else cfg.scan_interval_h
            wait = hours if last["ok"] else cfg.mac_retry_h
            is_due = now - last["finished"] >= wait * 3600
        if is_due:
            due.append(scan)
    full_locations = {s.location for s in due if s.kind == "full"}
    return [s for s in due if not (s.kind == "intake" and s.location in full_locations)]


def run_scan(con, scan: ScanDef, locs: dict[str, Location], cfg: Config,
             is_up: Callable[[Location], bool] | None = None,
             on_mac_down: Callable[[], None] | None = None) -> bool:
    """Run one scan and record it. True when the scan completed.

    on_mac_down is called when a Mac location turns out to be unreachable during the scan."""
    loc = locs[scan.location]
    started = time.time()
    ok, message = False, ""
    try:
        up = is_up(loc) if is_up else (not loc.is_mac or loc.reachable())
        if not up:
            message = "unreachable"
        elif not loc.root.exists():
            message = "root missing"
        else:
            if scan.kind == "downloads":
                res = scan_downloads(con, loc, locs, cfg)
            elif scan.kind == "intake":
                sub = cfg.d_intake if loc.name == "d" else cfg.mega_intake
                res = scan_library(con, loc, cfg, sub=sub, full=False)
            else:
                res = scan_library(con, loc, cfg, sub="", full=True)
            if res.suspect:
                message = "suspect: empty listing"
            else:
                ok, message = True, _scan_message(res)
            if loc.name == "d":
                refresh_folders(con, locs["d"])
    except MacUnreachable:
        message = "unreachable"
        if on_mac_down is not None and loc.is_mac:
            on_mac_down()
    except Exception as e:
        log.exception("scan %s failed", scan.name)
        message = str(e) or type(e).__name__
    finished = time.time()
    db.record_scan(con, name=scan.name, started=started, finished=finished, ok=ok,
                   message=message)
    if ok and scan.kind == "full":
        db.record_scan(con, name=scan.name.replace("-full", "-intake"), started=started,
                       finished=finished, ok=True, message=message)
    return ok


def _scan_message(res) -> str:
    message = f"found {res.found}, queued {res.queued}"
    for label, count in (("moved", res.moved), ("nfo", res.nfo_deleted),
                         ("folders", res.dirs_removed)):
        if count:
            message += f", {label} {count}"
    return message


def run_sorter(con, locs: dict[str, Location], cfg: Config) -> bool:
    """Hand the converted intake files to the sorter once none are still in flight."""
    if db.meta_get(con, "sorter_dirty") != "1":
        return False
    marks = ",".join("?" * len(INTAKE_BLOCKING))
    busy = con.execute(f"SELECT 1 FROM items WHERE location='d' AND source='intake' "
                       f"AND status IN ({marks}) LIMIT 1", INTAKE_BLOCKING).fetchone()
    if busy:
        return False
    if not cfg.sorter.is_file():
        log.error("sorter script missing: %s", cfg.sorter)
        db.meta_set(con, "sorter_dirty", "0")
        return False
    cmd = [sys.executable, str(cfg.sorter), "--src", str(cfg.d_root / cfg.d_converted),
           "--dst", str(cfg.d_root)]
    log.info("$ %s", " ".join(cmd))
    try:
        rc = subprocess.run(cmd, cwd=cfg.sorter.parent).returncode
    except OSError:
        log.exception("could not run the sorter")
        rc = -1
    if rc != 0:
        log.warning("sorter exited with %s", rc)
    db.meta_set(con, "sorter_dirty", "0")
    return rc == 0


class Service:
    def __init__(self, cfg: Config, locs: dict[str, Location] | None = None):
        self.cfg = cfg
        self.locs = build_locations(cfg) if locs is None else locs
        self.con = db.open_db(cfg.db_path)
        self.stop = threading.Event()
        self.current: dict = {}
        self._mac_lock = threading.Lock()
        self._mac_ok = False
        self._mac_at: float | None = None
        self._current_at = 0.0

    # -- Mac reachability: one shared in-memory cache (no database access, worker threads
    #    call this); tick() persists it. Both Mac locations share one session --
    def mac_cached(self, now: float | None = None) -> bool:
        """The cached answer, for any thread. Never probes: an expired or unset cache is DOWN."""
        now = time.time() if now is None else now
        with self._mac_lock:
            return (self._mac_at is not None and now - self._mac_at < MAC_CACHE_S
                    and self._mac_ok)

    def mac_up(self, now: float | None = None) -> bool:
        """Refresh the cache when expired (it can mount/unmount and wait on ssh): main thread only."""
        now = time.time() if now is None else now
        with self._mac_lock:
            if self._mac_at is None or now - self._mac_at >= MAC_CACHE_S:
                macs = [loc for loc in self.locs.values() if loc.is_mac]
                self._mac_ok, self._mac_at = bool(macs) and macs[0].reachable(), now
            return self._mac_ok

    def _mac_down(self) -> None:
        with self._mac_lock:
            self._mac_ok, self._mac_at = False, time.time()

    def _worker_is_up(self, name: str) -> bool:
        """Cached answer for worker threads: Mac locations use the shared cache, local ones are up."""
        loc = self.locs.get(name)
        return True if loc is None or not loc.is_mac else self.mac_cached()

    # -- scheduling --
    def tick(self) -> None:
        up = lambda loc: not loc.is_mac or self.mac_up()
        cached = lambda name: self.mac_up()  # one cached answer for both Mac locations
        self.mac_up()  # keep the cache fresh for the worker threads, which never probe
        # Before the scans too, so a long scan does not delay deleting originals or the sorter.
        # counted and evicted once per tick, on the second pass only
        finalize_published(self.con, self.locs, self.cfg, cached, count_failures=False,
                           evictions=False)
        run_sorter(self.con, self.locs, self.cfg)
        for scan in due_scans(self.con, self.cfg, time.time()):
            if self.stop.is_set():
                return
            self.mac_up()
            run_scan(self.con, scan, self.locs, self.cfg, up, self._mac_down)
        self.refresh_folder_charts()
        finalize_published(self.con, self.locs, self.cfg, cached)
        run_sorter(self.con, self.locs, self.cfg)
        db.meta_set(self.con, "mac_reachable", "1" if self.mac_up() else "0")

    def refresh_folder_charts(self, now: float | None = None) -> None:
        """Recount the folders behind the dashboard's "biggest folders" charts when due."""
        now = time.time() if now is None else now
        for name, minutes in (("d", self.cfg.folders_refresh_min),
                              ("mega", self.cfg.mac_folders_refresh_min)):
            loc = self.locs.get(name)
            if loc is None or minutes <= 0 or self.stop.is_set():
                continue
            key = f"folders_at_{name}"
            if now - float(db.meta_get(self.con, key) or 0) < minutes * 60:
                continue
            if loc.is_mac and not self.mac_up():
                continue
            db.meta_set(self.con, key, str(now))  # also after a failure: no retry every tick
            try:
                refresh_folders(self.con, loc, snapshot_every_s=SNAPSHOT_EVERY_S)
            except MacUnreachable:
                self._mac_down()
            except Exception:
                log.exception("folder refresh for %s failed", name)

    # -- working --
    def _report(self, con, force: bool = False) -> None:
        if not force and time.monotonic() - self._current_at < CURRENT_EVERY_S:
            return
        self._current_at = time.monotonic()
        db.meta_set(con, "current", json.dumps(self.current))

    def work_once(self, kind: str, con=None) -> str | None:
        con = self.con if con is None else con
        if self.stop.is_set() or db.meta_get(con, "paused") == "1":
            return None
        excluded = [] if self.mac_cached() else [l.name for l in self.locs.values() if l.is_mac]
        item = db.claim_next(con, kind=kind, exclude_locations=excluded)
        if item is None:
            return None
        is_video = kind == "video"

        def on_progress(p: float) -> None:
            if self.stop.is_set():
                raise Stopping()
            self.current["progress"] = round(p, 3)
            self._report(con)

        try:
            if is_video:
                self.current = {"name": item["relpath"].rsplit("/", 1)[-1], "size": item["size"],
                                "progress": 0.0, "location": item["location"]}
                self._report(con, force=True)
            return worker.process_item(con, item, self.locs, self.cfg, self.cfg.work_dir,
                                       on_progress if is_video else None,
                                       is_up=self._worker_is_up)
        except MacUnreachable:
            self._mac_down()
            return "queued"
        except Stopping:
            db.set_status(con, item["id"], "queued")
            return "queued"
        except Exception:
            self._requeue_if_running(con, item["id"])
            raise
        finally:
            if is_video:
                self.current = {}
                self._report(con, force=True)

    @staticmethod
    def _requeue_if_running(con, item_id: int) -> None:
        try:
            row = con.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if row is not None and row["status"] == "running":
                db.set_status(con, item_id, "queued")
        except Exception:
            log.exception("could not requeue item %s", item_id)

    def _worker_loop(self, kind: str) -> None:
        con = db.open_db(self.cfg.db_path)
        try:
            while not self.stop.is_set():
                try:
                    result = self.work_once(kind, con)
                except Exception:
                    log.exception("%s worker failed", kind)
                    result = None
                if result in (None, "queued"):
                    self.stop.wait(IDLE_S)
        finally:
            con.close()

    # -- the long-running loop --
    def run_forever(self) -> None:
        cfg = self.cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        lock = open(cfg.state_dir / "lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("media-organizer is already running (lock held)")
        _setup_logging(cfg)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        db.reset_running(self.con)
        shutil.rmtree(cfg.work_dir, ignore_errors=True)
        cfg.work_dir.mkdir(parents=True)
        threads = [threading.Thread(target=self._worker_loop, args=(k,), name=f"{k}-{i}")
                   for i, k in enumerate(["video"] + ["image"] * cfg.image_workers)]
        for t in threads:
            t.start()
        log.info("started")
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.stop.wait(TICK_S)
        log.info("stopping")
        for t in threads:
            t.join(timeout=JOIN_TIMEOUT_S)
        log.info("stopped")


def _setup_logging(cfg: Config) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.handlers.RotatingFileHandler(cfg.log_path, maxBytes=5_000_000,
                                                         backupCount=5),
                    logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
