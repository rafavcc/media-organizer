import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import db, service, worker
from media_organizer.locations import LocalLocation, MacUnreachable
from media_organizer.service import SCANS, Service, Stopping, due_scans, run_scan
from tests.helpers import make_image, make_video, test_config

H = 3600.0
T = 1_700_000_000.0
STUB_SORTER = "import sys, pathlib\npathlib.Path(__file__).with_name('argv.txt').write_text(' '.join(sys.argv[1:]))\n"


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


class WorkTests(Base):
    def test_end_to_end(self):
        self.cfg.sorter.write_text(STUB_SORTER)
        (self.d.root / "A").mkdir()
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "A" / "x.mp4")
        make_video(self.d.root / "_TO_CONVERT" / "y.mp4")
        make_image(self.dl.root / "p.png")
        self.svc.tick()
        for kind in ("video", "image"):
            while self.svc.work_once(kind) is not None:
                pass
        self.svc.tick()
        self.assertTrue((self.d.root / "A" / "x_compressed.mp4").exists())
        self.assertTrue((self.d.root / "_CONVERTED" / "y_compressed.mp4").exists())
        argv = (self.tmp / "argv.txt").read_text()
        self.assertIn("--src", argv)
        self.assertTrue(argv.split("--src ")[1].split(" --dst")[0].endswith("_CONVERTED"))
        self.assertTrue((self.mega.root / "TBC" / "p_compressed.webp").exists())
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "0")

    def test_sorter_waits_for_intake_items(self):
        self.cfg.sorter.write_text(STUB_SORTER)
        make_video(self.d.root / "y.mp4")
        self.queue(self.d, "y.mp4", source="intake", priority=1)
        db.meta_set(self.con, "sorter_dirty", "1")
        self.assertFalse(service.run_sorter(self.con, self.locs, self.cfg))
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "1")
        self.assertFalse((self.tmp / "argv.txt").exists())

    def test_missing_sorter_clears_flag(self):
        db.meta_set(self.con, "sorter_dirty", "1")
        with self.assertLogs("media_organizer.service", "ERROR"):
            service.run_sorter(self.con, self.locs, self.cfg)
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "0")

    def test_downloads_video_goes_first(self):
        make_video(self.d.root / "lib.mp4")
        make_video(self.dl.root / "dl.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)
        self.queue(self.dl, "dl.mp4", source="downloads", priority=0,
                   dest_location="mega", dest_dir="TBC")
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
        self.assertEqual(db.meta_get(self.con, "mac_reachable"), "0")

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
        with mock.patch.object(worker, "process_item", side_effect=MacUnreachable("x")):
            self.assertEqual(self.svc.work_once("image"), "queued")
        self.assertFalse(self.svc.mac_up(now=self.svc._mac_at + 1))

    def test_stop_requeues_running_video(self):
        make_video(self.d.root / "lib.mp4")
        self.queue(self.d, "lib.mp4", source="library", priority=3)

        def fake(con, item, locs, cfg, work, on_progress=None):
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

        def fake(con, item, locs, cfg, work, on_progress=None):
            on_progress(0.25)
            seen.append(dict(self.svc.current))
            return "done"
        with mock.patch.object(worker, "process_item", side_effect=fake):
            self.svc.work_once("video")
        self.assertEqual(seen, [{"name": "lib.mp4", "size": self.row("lib.mp4")["size"],
                                 "progress": 0.25, "location": "d"}])
        self.assertEqual(self.svc.current, {})
        self.assertEqual(json.loads(db.meta_get(self.con, "current")), {})


if __name__ == "__main__":
    unittest.main()
