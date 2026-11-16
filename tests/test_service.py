import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import db, service, worker
from media_organizer.locations import LocalLocation, MacUnreachable
from media_organizer.service import SCANS, Service, Stopping, due_scans, run_scan
from tests.helpers import make_image, make_video, test_config

H = 3600.0
T = 1_700_000_000.0


class FakeMac(LocalLocation):
    is_mac = True

    def __init__(self, name, root):
        super().__init__(name, root)
        self.reachable_flag = True

    def reachable(self):
        return self.reachable_flag


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.9, min_age_min=0,
                               mac_delete_delay_s=0)
        self.d = LocalLocation("d", self.cfg.d_root)
        self.dl = LocalLocation("win_dl", self.cfg.win_downloads)
        self.mega = FakeMac("mega", self.tmp / "mega")
        self.mac_dl = FakeMac("mac_dl", self.tmp / "mac_dl")
        self.locs = {"d": self.d, "win_dl": self.dl, "mega": self.mega, "mac_dl": self.mac_dl}
        for loc in self.locs.values():
            loc.root.mkdir(parents=True)
        self.svc = Service(self.cfg, self.locs)
        self.addCleanup(self.svc.con.close)
        self.con = self.svc.con

    def set_mac(self, up):
        self.mega.reachable_flag = self.mac_dl.reachable_flag = up

    def ran(self, name, t, ok=True):
        db.record_scan(self.con, name=name, started=t, finished=t, ok=ok, message="")

    def all_ran(self, t):
        for s in SCANS:
            self.ran(s.name, t)

    def names(self, now):
        return {s.name for s in due_scans(self.con, self.cfg, now)}

    def row(self, rel, loc="d"):
        return self.con.execute("SELECT * FROM items WHERE location=? AND relpath=?",
                                (loc, rel)).fetchone()

    def queue(self, loc, rel, kind="video", **kw):
        st = loc.path(rel).stat()
        db.upsert_candidate(self.con, location=loc.name, relpath=rel, size=st.st_size,
                            mtime=st.st_mtime, kind=kind, settings="s", **kw)


class SchedulerTests(Base):
    def test_fresh_database(self):
        self.assertEqual(self.names(T), {"d-full", "mega-full", "win-downloads", "mac-downloads"})

    def test_intervals(self):
        self.all_ran(T)
        self.assertEqual(self.names(T + 5 * H), set())
        self.assertEqual(self.names(T + 6.1 * H),
                         {"d-intake", "mega-intake", "win-downloads", "mac-downloads"})
        self.assertEqual(self.names(T + 169 * H),
                         {"d-full", "mega-full", "win-downloads", "mac-downloads"})

    def test_failed_scan_retries_after_an_hour(self):
        self.all_ran(T)
        self.ran("mac-downloads", T + 1 * H, ok=False)
        self.assertNotIn("mac-downloads", self.names(T + 1 * H + 59 * 60))
        self.assertIn("mac-downloads", self.names(T + 1 * H + 61 * 60))

    def test_unreachable_mac_is_recorded(self):
        self.set_mac(False)
        scan = next(s for s in SCANS if s.name == "mac-downloads")
        self.assertFalse(run_scan(self.con, scan, self.locs, self.cfg))
        last = db.last_scan(self.con, "mac-downloads")
        self.assertEqual((last["ok"], last["message"]), (0, "unreachable"))

    def test_missing_root_is_recorded(self):
        self.d.root.rmdir()
        scan = next(s for s in SCANS if s.name == "d-full")
        self.assertFalse(run_scan(self.con, scan, self.locs, self.cfg))
        last = db.last_scan(self.con, "d-full")
        self.assertEqual((last["ok"], last["message"]), (0, "root missing"))

    def test_full_scan_also_restarts_intake_schedule(self):
        make_video(self.d.root / "x.mp4")
        scan = next(s for s in SCANS if s.name == "d-full")
        self.assertTrue(run_scan(self.con, scan, self.locs, self.cfg))
        self.assertEqual(db.last_scan(self.con, "d-full")["message"], "found 1, queued 1")
        self.assertEqual(db.last_scan(self.con, "d-intake")["ok"], 1)

    def test_downloads_scan_message_includes_nonzero_counts(self):
        dl = self.cfg.win_downloads
        dl.mkdir(exist_ok=True)
        old = time.time() - 3600
        nfo = dl / "a" / "x.nfo"
        nfo.parent.mkdir()
        nfo.write_bytes(b"x")
        empty = dl / "e"
        empty.mkdir()
        for p in (nfo, empty):
            os.utime(p, (old, old))
        scan = next(s for s in SCANS if s.name == "win-downloads")
        self.assertTrue(run_scan(self.con, scan, self.locs, self.cfg))
        self.assertEqual(db.last_scan(self.con, "win-downloads")["message"],
                         "found 0, queued 0, nfo 1, folders 2")  # min_age_min is 0 here

    def test_exception_in_scan_is_recorded_not_raised(self):
        scan = next(s for s in SCANS if s.name == "d-full")
        with mock.patch.object(service, "scan_library", side_effect=RuntimeError("boom")):
            with self.assertLogs("media_organizer.service", "ERROR"):
                self.assertFalse(run_scan(self.con, scan, self.locs, self.cfg))
        last = db.last_scan(self.con, "d-full")
        self.assertEqual((last["ok"], last["message"]), (0, "boom"))

    def test_suspect_scan_fails(self):
        make_video(self.d.root / "x.mp4")
        scan = next(s for s in SCANS if s.name == "d-full")
        run_scan(self.con, scan, self.locs, self.cfg)
        (self.d.root / "x.mp4").unlink()
        self.assertFalse(run_scan(self.con, scan, self.locs, self.cfg))
        self.assertEqual(db.last_scan(self.con, "d-full")["message"], "suspect: empty listing")


class FolderChartTests(Base):
    def folders(self, loc="d"):
        return {r["folder"]: r["bytes"] for r in
                self.con.execute("SELECT folder, bytes FROM folders WHERE location=?", (loc,))}

    def snapshots(self, loc="d"):
        return self.con.execute("SELECT COUNT(*) FROM snapshots WHERE location=?",
                                (loc,)).fetchone()[0]

    def test_d_folders_follow_changes_every_few_minutes(self):
        (self.d.root / "a").mkdir()
        (self.d.root / "a" / "x.bin").write_bytes(b"1" * 100)
        self.svc.refresh_folder_charts(now=T)
        self.assertEqual(self.folders(), {"a": 100})
        (self.d.root / "a" / "y.bin").write_bytes(b"1" * 50)
        self.svc.refresh_folder_charts(now=T + 4 * 60)  # too soon
        self.assertEqual(self.folders(), {"a": 100})
        self.svc.refresh_folder_charts(now=T + 5 * 60)
        self.assertEqual(self.folders(), {"a": 150})

    def test_snapshots_are_at_most_hourly(self):
        (self.d.root / "a").mkdir()
        (self.d.root / "a" / "x.bin").write_bytes(b"1")
        for minutes in (0, 5, 10, 55):
            self.svc.refresh_folder_charts(now=T + minutes * 60)
        self.assertEqual(self.snapshots(), 1)
        self.con.execute("UPDATE snapshots SET ts = ts - 7200")  # the point is now two hours old
        self.con.commit()
        self.svc.refresh_folder_charts(now=T + 70 * 60)
        self.assertEqual(self.snapshots(), 2)

    def test_zero_minutes_disables_the_refresh(self):
        self.svc.cfg = test_config(self.tmp, folders_refresh_min=0, mac_folders_refresh_min=0)
        (self.d.root / "x.bin").write_bytes(b"1")
        self.svc.refresh_folder_charts(now=T)
        self.assertEqual(self.folders(), {})

    def test_mega_waits_for_the_mac_and_an_hour(self):
        (self.mega.root / "m").mkdir()
        (self.mega.root / "m" / "x.bin").write_bytes(b"1" * 10)
        self.set_mac(False)
        self.svc.refresh_folder_charts(now=T)
        self.assertEqual(self.folders("mega"), {})
        self.set_mac(True)
        self.svc._mac_at = None
        self.svc.refresh_folder_charts(now=T + 1)
        self.assertEqual(self.folders("mega"), {"m": 10})
        (self.mega.root / "m" / "y.bin").write_bytes(b"1" * 5)
        self.svc.refresh_folder_charts(now=T + 30 * 60)
        self.assertEqual(self.folders("mega"), {"m": 10})
        self.svc.refresh_folder_charts(now=T + 61 * 60)
        self.assertEqual(self.folders("mega"), {"m": 15})

    def test_mac_dropping_mid_refresh_is_not_an_error(self):
        with mock.patch("media_organizer.service.refresh_folders", side_effect=MacUnreachable):
            self.svc.refresh_folder_charts(now=T)  # must not raise
        self.assertFalse(self.svc.mac_cached())

    def test_unmounted_empty_drive_keeps_the_last_numbers(self):
        (self.d.root / "a").mkdir()
        (self.d.root / "a" / "x.bin").write_bytes(b"1" * 100)
        self.svc.refresh_folder_charts(now=T)
        (self.d.root / "a" / "x.bin").unlink()
        (self.d.root / "a").rmdir()
        self.svc.refresh_folder_charts(now=T + 6 * 60)
        self.assertEqual(self.folders(), {"a": 100})


class WorkTests(Base):
    def test_end_to_end(self):
        self.cfg.performers_file.write_text("Alice Smith\n")
        (self.d.root / "A").mkdir()
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "A" / "x.mp4")
        make_video(self.d.root / "_TO_CONVERT" / "alice.smith.y.mp4")
        make_video(self.d.root / "_TO_CONVERT" / "nobody.mp4")
        make_image(self.dl.root / "p.png")
        self.svc.tick()
        for kind in ("video", "image"):
            while self.svc.work_once(kind) is not None:
                pass
        self.svc.tick()
        self.assertTrue((self.d.root / "A" / "x_compressed.mp4").exists())
        self.assertTrue((self.d.root / "Alice Smith" / "alice.smith.y_compressed.mp4").exists())
        self.assertEqual(sorted(p.name for p in (self.d.root / "_CONVERTED").iterdir()),
                         ["nobody_compressed.mp4"])  # no name match: waits in _CONVERTED
        self.assertTrue((self.mega.root / "TBC" / "p_compressed.webp").exists())

    def converted(self, name, data=b"new"):
        (self.d.root / "_CONVERTED").mkdir(exist_ok=True)
        (self.d.root / "_CONVERTED" / name).write_bytes(data)

    def test_sorter_picks_up_leftovers_once_the_name_is_added(self):
        self.cfg.performers_file.write_text("Someone Else\n")
        self.converted("Bob Jones - clip.mp4")
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 0)
        self.cfg.performers_file.write_text("Someone Else\nbob jones\n")
        os.utime(self.cfg.performers_file, (T, T))  # a new mtime even on coarse clocks
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 1)
        self.assertTrue((self.d.root / "Bob Jones" / "Bob Jones - clip.mp4").exists())
        self.assertEqual(self.cfg.performers_file.read_text(),
                         "Pmv Compilation\nBob Jones\nSomeone Else\n")  # tidied as before

    def test_sorter_leaves_files_the_worker_may_still_touch(self):
        self.cfg.performers_file.write_text("Bob Jones\n")
        self.converted("bob jones.mp4")
        self.queue(self.d, "_CONVERTED/bob jones.mp4", source="library", priority=3)
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 0)
        rid = self.row("_CONVERTED/bob jones.mp4")["id"]
        db.set_status(self.con, rid, "done")
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 1)
        self.assertEqual(self.con.execute("SELECT relpath FROM items WHERE id=?",
                                          (rid,)).fetchone()["relpath"], "Bob Jones/bob jones.mp4")

    def test_newer_file_replaces_the_one_in_the_performer_folder(self):
        self.cfg.performers_file.write_text("Bob Jones\n")
        (self.d.root / "Bob Jones").mkdir()
        (self.d.root / "Bob Jones" / "bob jones.mp4").write_bytes(b"a much bigger old file")
        self.queue(self.d, "Bob Jones/bob jones.mp4", source="library", priority=3)
        old = self.row("Bob Jones/bob jones.mp4")["id"]
        db.set_status(self.con, old, "done")
        self.converted("bob jones.mp4", b"new")
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 1)
        self.assertEqual((self.d.root / "Bob Jones" / "bob jones.mp4").read_bytes(), b"new")
        self.assertFalse((self.d.root / "_CONVERTED" / "bob jones.mp4").exists())
        self.assertIsNone(self.con.execute("SELECT 1 FROM items WHERE id=?", (old,)).fetchone())

    def test_missing_performers_file_sorts_nothing(self):
        self.converted("bob jones.mp4")
        self.assertEqual(service.run_sorter(self.con, self.locs, self.cfg), 0)
        self.assertTrue((self.d.root / "_CONVERTED" / "bob jones.mp4").exists())

    def test_downloads_video_goes_first(self):
        make_video(self.d.root / "lib.mp4")
        make_video(self.dl.root / "dl.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        self.queue(self.dl, "dl.mp4", source="downloads", priority=0,
                   dest_location="mega", dest_dir="TBC")
        self.svc.mac_up()  # the main thread refreshes the cache; workers only read it
        self.assertIsNotNone(self.svc.work_once("video"))
        self.assertEqual(self.row("lib.mp4")["status"], "queued")
        self.assertIsNone(self.row("dl.mp4", "win_dl"))  # processed and finished
        self.assertEqual(self.row("lib.mp4")["attempts"], 0)

    def test_paused(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        db.meta_set(self.con, "paused", "1")
        self.assertIsNone(self.svc.work_once("video"))
        self.assertEqual(self.row("lib.mp4")["status"], "queued")
        self.assertEqual(self.row("lib.mp4")["attempts"], 0)

    def test_mac_unreachable_leaves_mac_items(self):
        make_image(self.mega.root / "m.png")
        make_image(self.d.root / "d.png")
        self.queue(self.mega, "m.png", kind="image", source="library", priority=2)
        self.queue(self.d, "d.png", kind="image", source="library", priority=3)
        self.set_mac(False)
        self.assertIsNotNone(self.svc.work_once("image"))
        self.assertIsNone(self.svc.work_once("image"))
        self.assertEqual(self.row("m.png", "mega")["status"], "queued")
        self.assertEqual(self.row("d.png")["status"], "done")
        self.assertFalse(self.svc.mac_up())

    def test_mac_reachability_is_cached_for_a_minute(self):
        calls = []
        self.mega.reachable = lambda: calls.append(1) or True
        self.assertTrue(self.svc.mac_up(now=100.0))
        self.assertTrue(self.svc.mac_up(now=150.0))
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.svc.mac_up(now=161.0))
        self.assertEqual(len(calls), 2)

    def test_mac_unreachable_during_processing(self):
        make_image(self.mega.root / "m.png")
        self.queue(self.mega, "m.png", kind="image", source="library", priority=2)
        self.svc.mac_up()
        with mock.patch.object(worker, "process_item", side_effect=MacUnreachable("x")):
            self.assertEqual(self.svc.work_once("image"), "queued")
        self.assertFalse(self.svc.mac_up(now=self.svc._mac_at + 1))

    def test_work_once_passes_cached_mac_state_to_the_worker(self):
        make_image(self.d.root / "p.png")
        self.queue(self.d, "p.png", kind="image", source="library", priority=3)
        seen = {}

        def fake(con, item, locs, cfg, work, on_progress=None, is_up=None):
            seen["mega"], seen["d"] = is_up("mega"), is_up("d")
            return "done"
        self.mega.reachable = lambda: (_ for _ in ()).throw(AssertionError("probed"))
        self.svc._mac_ok, self.svc._mac_at = True, time.time()
        with mock.patch.object(worker, "process_item", side_effect=fake):
            self.svc.work_once("image")
        self.assertEqual(seen, {"mega": True, "d": True})

    def test_stop_requeues_running_video(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)

        def fake(con, item, locs, cfg, work, on_progress=None, is_up=None):
            self.svc.stop.set()
            on_progress(0.5)
        with mock.patch.object(worker, "process_item", side_effect=fake):
            self.assertEqual(self.svc.work_once("video"), "queued")
        self.assertEqual(self.row("lib.mp4")["status"], "queued")
        self.assertEqual(self.svc.current, {})
        self.assertEqual(db.meta_get(self.con, "current"), "{}")
        self.assertIsNone(self.svc.work_once("video"))

    def test_current_is_reported_while_running(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        seen = []

        def fake(con, item, locs, cfg, work, on_progress=None, is_up=None):
            on_progress(0.25)
            seen.append(dict(self.svc.current))
            return "done"
        with mock.patch.object(worker, "process_item", side_effect=fake):
            self.svc.work_once("video")
        self.assertEqual(seen, [{"name": "lib.mp4", "size": self.row("lib.mp4")["size"],
                                 "progress": 0.25, "location": "d"}])
        self.assertEqual(self.svc.current, {})
        self.assertEqual(json.loads(db.meta_get(self.con, "current")), {})


class FixRound1Tests(Base):
    def test_worker_never_touches_the_service_connection(self):
        make_image(self.mega.root / "m.png")
        self.queue(self.mega, "m.png", kind="image", source="library", priority=2)
        other = db.open_db(self.cfg.db_path)
        self.addCleanup(other.close)

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError(f"service connection used: {name}")
        self.svc.con = Forbidden()
        self.assertTrue(self.svc.mac_up())  # refreshed by the main thread
        self.assertEqual(self.svc.work_once("image", other), "published")

    def test_tick_writes_mac_reachable_from_the_cache(self):
        self.set_mac(False)
        self.svc.tick()
        self.assertEqual(db.meta_get(self.con, "mac_reachable"), "0")

    def test_unexpected_exception_requeues_the_item(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        with mock.patch.object(worker, "process_item", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.svc.work_once("video")
        self.assertEqual(self.row("lib.mp4")["status"], "queued")

    def test_unexpected_exception_leaves_a_finished_item_alone(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)

        def fake(con, item, locs, cfg, work, on_progress=None, is_up=None):
            db.set_status(con, item["id"], "error")
            raise RuntimeError("late")
        with mock.patch.object(worker, "process_item", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self.svc.work_once("video")
        self.assertEqual(self.row("lib.mp4")["status"], "error")

    def test_mac_unreachable_during_scan_marks_mac_down_at_once(self):
        self.mac_dl.reachable_flag = True
        with mock.patch.object(service, "scan_downloads", side_effect=MacUnreachable("x")):
            self.svc.tick()
        self.mega.reachable = self.mac_dl.reachable = lambda: self.fail("reachable() called")
        self.assertFalse(self.svc.mac_up())
        self.assertEqual(db.meta_get(self.con, "mac_reachable"), "0")

    def test_failing_first_report_requeues_the_item(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        with mock.patch.object(self.svc, "_report", side_effect=OSError("db locked")):
            with self.assertRaises(OSError):
                self.svc.work_once("video")
        self.assertEqual(self.row("lib.mp4")["status"], "queued")

    def test_tick_hands_the_cached_mac_state_to_finalize(self):
        seen = []
        self.set_mac(False)
        with mock.patch.object(service, "finalize_published",
                               side_effect=lambda con, locs, cfg, is_up=None, **kw: seen.append(is_up)):
            self.svc.tick()
        is_up = seen[0]
        self.assertFalse(is_up("mega"))
        self.mega.reachable = self.mac_dl.reachable = lambda: self.fail("reachable() called")
        self.assertFalse(is_up("mega"))  # answered from the cache

    def test_tick_finalizes_and_sorts_before_and_after_scans(self):
        calls = []
        scan = lambda *a, **k: calls.append("scan") or mock.Mock(suspect=False, found=0, queued=0)
        with mock.patch.object(service, "finalize_published",
                               side_effect=lambda *a, **kw: calls.append("finalize")), \
                mock.patch.object(service, "run_sorter",
                                  side_effect=lambda *a: calls.append("sorter")), \
                mock.patch.object(service, "scan_library", side_effect=scan), \
                mock.patch.object(service, "scan_downloads", side_effect=scan):
            self.svc.tick()
        self.assertEqual(calls[:3], ["finalize", "sorter", "scan"])
        self.assertEqual(calls[-2:], ["finalize", "sorter"])
        self.assertEqual(calls.count("scan"), 4)


class WorkerCacheTests(Base):
    def on_thread(self, fn):
        out = {}

        def run():
            try:
                out["v"] = fn()
            except BaseException as e:  # noqa: BLE001
                out["e"] = e
        t = threading.Thread(target=run)
        t.start()
        t.join()
        if "e" in out:
            raise out["e"]
        return out["v"]

    def test_worker_thread_never_probes_and_sees_expired_cache_as_down(self):
        make_image(self.mega.root / "m.png")
        self.queue(self.mega, "m.png", kind="image", source="library", priority=2)
        self.mega.reachable = lambda: (_ for _ in ()).throw(AssertionError("probed"))
        self.mac_dl.reachable = self.mega.reachable
        self.assertIsNone(self.svc._mac_at)  # never set: counts as down
        self.assertIsNone(self.on_thread(lambda: self.svc.work_once("image")))
        self.assertFalse(self.on_thread(lambda: self.svc._worker_is_up("mega")))
        self.assertTrue(self.on_thread(lambda: self.svc._worker_is_up("d")))
        self.assertEqual(self.row("m.png", "mega")["status"], "queued")
        self.svc._mac_ok, self.svc._mac_at = True, time.time() - 120  # expired
        self.assertFalse(self.on_thread(lambda: self.svc.mac_cached()))

    def test_tick_refreshes_the_cache_for_workers(self):
        make_image(self.mega.root / "m.png")
        self.queue(self.mega, "m.png", kind="image", source="library", priority=2)
        self.assertFalse(self.svc.mac_cached())
        self.svc.tick()  # main thread probes
        self.assertTrue(self.on_thread(lambda: self.svc.mac_cached()))
        self.assertEqual(self.on_thread(lambda: self.svc.work_once("image")), "published")


class TickCountTests(Base):
    def test_failing_delete_counts_once_per_tick(self):
        make_video(self.d.root / "x.mp4")
        self.queue(self.d, "x.mp4", source="library", priority=3)
        self.all_ran(time.time())  # no scans
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        with mock.patch.object(self.d, "delete", side_effect=PermissionError("ro")), \
                self.assertLogs("media_organizer.worker", "ERROR"):
            self.assertEqual(worker.process_item(self.con, item, self.locs, self.cfg,
                                                 self.cfg.work_dir), "published")
            for n in range(1, 5):
                self.svc.tick()
                r = self.row("x.mp4")
                self.assertEqual((r["status"], r["finalize_fails"]), ("published", n))
            self.svc.tick()
        self.assertEqual(self.row("x.mp4")["status"], "error")

    def test_evictions_retried_once_per_tick(self):
        self.all_ran(time.time())
        with mock.patch.object(worker, "retry_evictions", return_value=0) as m:
            self.svc.tick()
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
