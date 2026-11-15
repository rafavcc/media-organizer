import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from media_organizer import db, stats
from media_organizer.service import SCANS
from tests.helpers import test_config

GB = 1024 ** 3
KEYS = {"generated", "freed", "disk", "first_pass", "milestones", "daily", "now", "paused",
        "queue", "scans", "mac_reachable", "folders", "recent", "problems"}


def add_item(con, location, relpath, status, source="library", size=1000, error=None,
             reason=None, finished_at=None):
    db.upsert_candidate(con, location=location, relpath=relpath, size=size, mtime=1.0,
                        kind="video", source=source, priority=3, settings="s")
    row = con.execute("SELECT id FROM items WHERE location=? AND relpath=?",
                      (location, relpath)).fetchone()
    if status != "queued":
        db.set_status(con, row["id"], status, error=error, reason=reason,
                      finished_at=finished_at)


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = test_config(Path(self.tmp.name))
        self.con = db.open_db(self.cfg.db_path)
        self.addCleanup(self.con.close)

    def test_empty_database(self):
        s = stats.summary(self.con, self.cfg)
        self.assertEqual(set(s), KEYS)
        self.assertIsNone(s["now"])
        self.assertFalse(s["paused"])
        self.assertFalse(s["mac_reachable"])
        self.assertEqual(s["freed"], {"total": 0, "d": 0, "mega": 0})
        self.assertEqual(s["disk"], {"d_free": 0, "d_total": 0, "mega_bytes": 0,
                                     "mega_bytes_start": 0})
        self.assertEqual(s["first_pass"]["d"], {"done": 0, "total": 0, "eta_days": None})
        self.assertEqual(len(s["daily"]), 30)
        self.assertTrue(all(d["d"] == 0 and d["mega"] == 0 for d in s["daily"]))
        self.assertEqual(s["queue"], {"downloads": 0, "intake": 0, "mega": 0, "library": 0,
                                      "no_space": 0, "evict_pending": 0})
        self.assertEqual([x["name"] for x in s["scans"]], [x.name for x in SCANS])
        self.assertEqual(len(s["milestones"]), 12)
        self.assertFalse(any(m["done"] for m in s["milestones"]))
        self.assertEqual(s["problems"], {"error": 0, "no-gain": 0, "low-quality": 0,
                                         "changed": 0, "multi-frame": 0, "stuck_published": 0,
                                         "items": []})
        self.assertEqual(s["recent"], [])

    def seed(self):
        now = time.time()
        add_item(self.con, "d", "a.mp4", "done")
        add_item(self.con, "d", "b.mp4", "done")
        add_item(self.con, "d", "c.mp4", "queued", size=6 * GB)
        add_item(self.con, "d", "e.mp4", "error", error="boom", finished_at=now)
        db.log_conversion(self.con, ts=now - 86400, location="d", relpath="a.mp4",
                          kind="video", bytes_before=40 * GB, bytes_after=10 * GB)
        db.log_conversion(self.con, ts=now, location="mega", relpath="m.mp4", kind="video",
                          bytes_before=8 * GB, bytes_after=3 * GB)
        db.add_snapshot(self.con, ts=now - 100, location="d", total_bytes=100, free_bytes=10)
        db.add_snapshot(self.con, ts=now - 100, location="mega", total_bytes=500, free_bytes=0)
        db.add_snapshot(self.con, ts=now, location="mega", total_bytes=400, free_bytes=0)
        db.replace_folders(self.con, "d", [(f"f{i}", i * 10, i, 0) for i in range(20)])
        return now

    def test_seeded(self):
        now = self.seed()
        s = stats.summary(self.con, self.cfg, now)
        self.assertEqual(s["freed"], {"total": 35 * GB, "d": 30 * GB, "mega": 5 * GB})
        self.assertEqual(s["first_pass"]["d"]["done"], 3)
        self.assertEqual(s["first_pass"]["d"]["total"], 4)
        self.assertIsNotNone(s["first_pass"]["d"]["eta_days"])
        self.assertIsNone(s["first_pass"]["mega"]["eta_days"])
        ms = {m["label"]: m["done"] for m in s["milestones"]}
        self.assertTrue(ms["D: first pass 50%"])
        self.assertTrue(ms["D: first pass 75%"])
        self.assertFalse(ms["D: first pass done"])
        self.assertFalse(ms["MEGA first pass 25%"])
        self.assertTrue(ms["100 GB freed"] is False)
        self.assertEqual(s["disk"]["d_free"], 10)
        self.assertEqual(s["disk"]["mega_bytes"], 400)
        self.assertEqual(s["disk"]["mega_bytes_start"], 500)
        self.assertEqual(len(s["daily"]), 30)
        self.assertEqual(s["daily"][-1]["mega"], 5 * GB)
        self.assertEqual(s["daily"][-2]["d"], 30 * GB)
        self.assertEqual(sum(1 for d in s["daily"] if d["d"] or d["mega"]), 2)
        self.assertEqual(s["queue"]["library"], 1)
        self.assertEqual(s["problems"]["error"], 1)
        self.assertEqual(s["problems"]["items"][0]["error"], "boom")
        self.assertEqual(len(s["folders"]["d"]), 15)
        sizes = [f["bytes"] for f in s["folders"]["d"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(s["folders"]["mega"], [])
        self.assertEqual([r["location"] for r in s["recent"]], ["mega", "d"])
        self.assertEqual(len(s["scans"]), 6)
        self.assertTrue(all(x["finished"] is None for x in s["scans"]))

    def test_meta_and_scans_and_queue(self):
        db.meta_set(self.con, "paused", "1")
        db.meta_set(self.con, "mac_reachable", "1")
        cur = {"name": "x.mp4", "size": 5, "progress": 0.5, "location": "d"}
        db.meta_set(self.con, "current", json.dumps(cur))
        db.record_scan(self.con, name="d-full", started=1.0, finished=2.0, ok=True,
                       message="fine")
        add_item(self.con, "d", "q1.mp4", "queued", source="downloads")
        add_item(self.con, "d", "q2.mp4", "queued", source="intake")
        add_item(self.con, "mega", "q3.mp4", "queued", source="library")
        add_item(self.con, "d", "r.mp4", "rejected", reason="no-gain")
        s = stats.summary(self.con, self.cfg)
        self.assertTrue(s["paused"])
        self.assertTrue(s["mac_reachable"])
        self.assertEqual(s["now"], cur)
        self.assertEqual(s["queue"], {"downloads": 1, "intake": 1, "mega": 1, "library": 0,
                                      "no_space": 0, "evict_pending": 0})
        self.assertEqual(s["problems"]["no-gain"], 1)
        scan = {x["name"]: x for x in s["scans"]}["d-full"]
        self.assertEqual((scan["finished"], scan["ok"], scan["message"]), (2.0, True, "fine"))
        for bad in ("{}", "not json"):
            db.meta_set(self.con, "current", bad)
            self.assertIsNone(stats.summary(self.con, self.cfg)["now"])

    def test_queue_counts_waiting_for_space_and_pending_evictions(self):
        now = 1_000_000.0
        add_item(self.con, "d", "a.mp4", "queued")
        add_item(self.con, "d", "b.mp4", "queued")
        add_item(self.con, "d", "c.mp4", "queued")
        add_item(self.con, "d", "e.mp4", "done")
        ids = {r["relpath"]: r["id"] for r in self.con.execute("SELECT id, relpath FROM items")}
        db.set_status(self.con, ids["a.mp4"], "queued", reason="no-space", not_before=now + 10)
        db.set_status(self.con, ids["b.mp4"], "queued", reason="no-space", not_before=now - 10)
        db.set_status(self.con, ids["c.mp4"], "queued", reason="name-taken", not_before=now + 10)
        db.set_status(self.con, ids["e.mp4"], "done", evict_pending=1)
        q = stats.summary(self.con, self.cfg, now)["queue"]
        self.assertEqual((q["no_space"], q["evict_pending"]), (1, 1))

    def test_problem_counts_multi_frame_and_stuck_published(self):
        now = 1_000_000.0
        add_item(self.con, "d", "m.tif", "rejected", reason="multi-frame")
        add_item(self.con, "d", "old.mp4", "published")
        add_item(self.con, "d", "new.mp4", "published")
        add_item(self.con, "d", "requeued.mp4", "published")
        ids = {r["relpath"]: r["id"] for r in self.con.execute("SELECT id, relpath FROM items")}
        # stuck: due more than an hour ago
        db.set_status(self.con, ids["old.mp4"], "published", not_before=now - 7200)
        db.set_status(self.con, ids["new.mp4"], "published", not_before=now - 60)
        # an old item requeued and republished: stale started/finished times do not matter
        db.set_status(self.con, ids["requeued.mp4"], "published", not_before=now + 60,
                      started_at=now - 90000, finished_at=now - 80000)
        p = stats.summary(self.con, self.cfg, now)["problems"]
        self.assertEqual((p["multi-frame"], p["stuck_published"]), (1, 1))

    def test_first_pass_done_survives_a_prune(self):
        add_item(self.con, "d", "a.mp4", "done")
        add_item(self.con, "d", "b.mp4", "queued")
        before = stats.summary(self.con, self.cfg)["first_pass"]["d"]
        db.prune_missing(self.con, location="d", under="", seen={"b.mp4"})  # a.mp4 was deleted
        after = stats.summary(self.con, self.cfg)["first_pass"]["d"]
        self.assertEqual((before["done"], before["total"]), (1, 2))
        self.assertEqual((after["done"], after["total"]), (1, 2))

    def test_first_pass_unchanged_after_a_full_scan_that_prunes(self):
        from media_organizer.locations import LocalLocation
        from media_organizer.scanner import scan_library
        root = Path(self.cfg.d_root)
        root.mkdir(parents=True)
        (root / "left.mp4").write_bytes(b"x" * 10)  # a file that still exists
        old = time.time() - 7200
        import os
        os.utime(root / "left.mp4", (old, old))
        add_item(self.con, "d", "converted.mp4", "done")  # its original was deleted
        add_item(self.con, "d", "vanished.mp4", "skipped")  # a skipped file that is gone
        before = stats.summary(self.con, self.cfg)["first_pass"]["d"]["done"]
        scan_library(self.con, LocalLocation("d", root), self.cfg, full=True)
        after = stats.summary(self.con, self.cfg)["first_pass"]["d"]
        self.assertEqual(before, 2)
        self.assertEqual(after["done"], 1)  # only the skipped ghost went
        self.assertEqual(self.con.execute("SELECT status FROM items WHERE "
                                          "relpath='converted.mp4'").fetchone()[0], "done")

    def test_disk_free_is_live_when_the_root_exists(self):
        from collections import namedtuple
        from unittest import mock
        db.add_snapshot(self.con, ts=1.0, location="d", total_bytes=100, free_bytes=10)
        self.assertEqual(stats.summary(self.con, self.cfg)["disk"]["d_free"], 10)  # fallback
        Path(self.cfg.d_root).mkdir(parents=True)
        usage = namedtuple("usage", "total used free")
        with mock.patch("media_organizer.stats.shutil.disk_usage",
                        return_value=usage(1000, 400, 600)):
            disk = stats.summary(self.con, self.cfg)["disk"]
        self.assertEqual((disk["d_free"], disk["d_total"]), (600, 1000))

    def test_milestones_done_and_disk(self):
        add_item(self.con, "d", "a.mp4", "done")
        add_item(self.con, "mega", "b.mp4", "skipped")
        db.log_conversion(self.con, ts=time.time(), location="d", relpath="a.mp4",
                          kind="video", bytes_before=600 * GB, bytes_after=1)
        db.add_snapshot(self.con, ts=1.0, location="d", total_bytes=100, free_bytes=40)
        s = stats.summary(self.con, self.cfg)
        ms = {m["label"]: m["done"] for m in s["milestones"]}
        self.assertTrue(ms["D: first pass done"] and ms["MEGA first pass done"])
        self.assertTrue(ms["500 GB freed"])
        self.assertFalse(ms["D: under 70% full"])  # d_total is 0 (root missing)
        Path(self.cfg.d_root).mkdir(parents=True)
        self.assertGreater(stats.summary(self.con, self.cfg)["disk"]["d_total"], 0)

    def test_daily_is_local_dates_oldest_first(self):
        now = time.time()
        s = stats.summary(self.con, self.cfg, now)
        today = datetime.fromtimestamp(now).date()
        self.assertEqual(s["daily"][-1]["date"], today.isoformat())
        self.assertEqual(s["daily"][0]["date"], (today - timedelta(days=29)).isoformat())


if __name__ == "__main__":
    unittest.main()
