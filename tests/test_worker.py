import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import db, worker
from media_organizer.locations import LocalLocation, MacUnreachable, NoSpace
from tests.helpers import make_image, make_video, test_config


class FakeMac(LocalLocation):
    is_mac = True

    def __init__(self, name, root):
        super().__init__(name, root)
        self.online_only = set()
        self.evicted = []
        self.reachable_flag = True
        self.clock = None
        self.publish_error = None
        self.error_after_publish = None

    def now(self):
        return super().now() if self.clock is None else self.clock

    def reachable(self):
        return self.reachable_flag

    def is_online_only(self, relpath):
        return relpath in self.online_only

    def evict(self, relpath):
        self.evicted.append(relpath)
        return True

    def publish(self, src, relpath, mtime, min_free):
        if self.publish_error is not None:
            raise self.publish_error
        size = super().publish(src, relpath, mtime, min_free)
        if self.error_after_publish is not None:
            raise self.error_after_publish
        return size


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.9)
        self.d = LocalLocation("d", self.cfg.d_root)
        self.dl = LocalLocation("win_dl", self.cfg.win_downloads)
        self.mega = FakeMac("mega", self.tmp / "mega")
        for loc in (self.d, self.dl, self.mega):
            loc.root.mkdir(parents=True)
        self.locs = {"d": self.d, "win_dl": self.dl, "mega": self.mega}
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.con = db.open_db(self.tmp / "state" / "o.db")
        self.addCleanup(self.con.close)

    def add(self, loc, rel, kind="video", source="library", **kw):
        st = loc.path(rel).stat()
        db.upsert_candidate(self.con, location=loc.name, relpath=rel, size=st.st_size,
                            mtime=st.st_mtime, kind=kind, source=source, priority=2,
                            settings="s", **kw)
        return db.claim_next(self.con, kind=kind, exclude_locations=())

    def row(self, item):
        return self.con.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone()

    def conversions(self):
        return self.con.execute("SELECT * FROM conversions").fetchall()

    def run_item(self, item):
        return worker.process_item(self.con, item, self.locs, self.cfg, self.work)


class LibraryTests(Base):
    def test_video_done(self):
        p = make_video(self.d.root / "x.mp4")
        os.utime(p, (1_500_000_000, 1_500_000_000))
        item = self.add(self.d, "x.mp4")
        self.assertEqual(self.run_item(item), "done")
        out = self.d.root / "x_compressed.mp4"
        self.assertTrue(out.exists())
        self.assertFalse(p.exists())
        (c,) = self.conversions()
        self.assertLess(c["bytes_after"], c["bytes_before"])
        self.assertEqual(out.stat().st_mtime, 1_500_000_000)
        self.assertEqual(self.row(item)["status"], "done")
        self.assertEqual(list(self.work.iterdir()), [])

    def test_no_gain_rejected(self):
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.01)
        p = make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        self.assertEqual(self.run_item(item), "rejected")
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("rejected", "no-gain"))
        self.assertTrue(p.exists())
        self.assertFalse((self.d.root / "x_compressed.mp4").exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_gone(self):
        p = make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        p.unlink()
        self.assertEqual(self.run_item(item), "gone")
        self.assertIsNone(self.row(item))

    def test_changed_after_scan(self):
        p = make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        with open(p, "ab") as f:
            f.write(b"more")
        self.assertEqual(self.run_item(item), "queued")
        r = self.row(item)
        self.assertEqual((r["status"], r["size"]), ("queued", p.stat().st_size))

    def test_error_status(self):
        (self.d.root / "bad.mp4").write_text("not a video")
        item = self.add(self.d, "bad.mp4")
        self.assertEqual(self.run_item(item), "error")
        self.assertEqual(self.row(item)["status"], "error")
        self.assertTrue((self.d.root / "bad.mp4").exists())

    def test_no_space_requeues(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = NoSpace("full")
        self.assertEqual(self.run_item(item), "queued")
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("queued", "no-space"))
        self.assertTrue(p.exists())
        self.assertFalse((self.mega.root / "x_compressed.mp4").exists())

    def test_mac_unreachable_during_publish(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = MacUnreachable("gone")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        self.assertEqual(self.row(item)["status"], "queued")
        self.assertTrue(p.exists())
        self.assertFalse((self.mega.root / "x_compressed.mp4").exists())
        self.assertEqual(list(self.work.iterdir()), [])


class MacFinalizeTests(Base):
    def published(self, rel="x.mp4", online_only=False):
        p = make_video(self.mega.root / rel)
        if online_only:
            self.mega.online_only.add(rel)
        item = self.add(self.mega, rel)
        self.assertEqual(self.run_item(item), "published")
        return p, item

    def test_delay_then_delete(self):
        p, item = self.published()
        self.assertTrue(p.exists())
        r = self.row(item)
        self.assertEqual(r["final_relpath"], "x_compressed.mp4")
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertTrue(p.exists())
        self.mega.clock = r["not_before"] + 1
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.row(item)["status"], "done")
        (c,) = self.conversions()
        self.assertEqual(c["bytes_after"], r["final_size"])

    def test_second_call_does_nothing(self):
        p, item = self.published()
        self.mega.clock = self.row(item)["not_before"] + 1
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 1)
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertEqual(len(self.conversions()), 1)

    def test_stale_row_object_not_acted_on_twice(self):
        p, item = self.published()
        self.mega.clock = self.row(item)["not_before"] + 1
        db.set_status(self.con, item["id"], "done")  # another thread finished it
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertEqual(self.conversions(), [])

    def test_online_only_evicts_final(self):
        p, item = self.published(online_only=True)
        self.assertEqual(self.row(item)["was_online_only"], 1)
        self.mega.clock = self.row(item)["not_before"] + 1
        worker.finalize_published(self.con, self.locs, self.cfg)
        self.assertEqual(self.mega.evicted, ["x_compressed.mp4"])

    def test_rejected_online_only_evicts_original(self):
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.01)
        make_video(self.mega.root / "x.mp4")
        self.mega.online_only.add("x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.assertEqual(self.run_item(item), "rejected")
        self.assertEqual(self.mega.evicted, ["x.mp4"])

    def test_final_lost_requeues(self):
        p, item = self.published()
        (self.mega.root / "x_compressed.mp4").unlink()
        self.mega.clock = self.row(item)["not_before"] + 1
        with self.assertLogs('media_organizer.worker', logging.WARNING):
            self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertTrue(p.exists())
        r = self.row(item)
        self.assertEqual((r["status"], r["final_relpath"], r["final_size"], r["not_before"]),
                         ("queued", None, None, None))

    def test_final_wrong_size_requeues_and_is_kept(self):
        p, item = self.published()
        final = self.mega.root / "x_compressed.mp4"
        with open(final, "ab") as f:
            f.write(b"x")
        self.mega.clock = self.row(item)["not_before"] + 1
        with self.assertLogs('media_organizer.worker', logging.WARNING):
            worker.finalize_published(self.con, self.locs, self.cfg)
        self.assertTrue(p.exists())
        self.assertTrue(final.exists())
        self.assertEqual(self.row(item)["status"], "queued")

    def test_original_changed_rejected(self):
        p, item = self.published()
        with open(p, "ab") as f:
            f.write(b"edit")
        self.mega.clock = self.row(item)["not_before"] + 1
        with self.assertLogs('media_organizer.worker', logging.WARNING):
            self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertTrue(p.exists())
        self.assertTrue((self.mega.root / "x_compressed.mp4").exists())
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("rejected", "changed"))

    def test_original_missing_marks_done(self):
        p, item = self.published()
        p.unlink()
        self.mega.clock = self.row(item)["not_before"] + 1
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertEqual(self.row(item)["status"], "done")
        self.assertEqual(self.conversions(), [])

    def test_unreachable_mac_skipped(self):
        p, item = self.published()
        self.mega.clock = self.row(item)["not_before"] + 1
        self.mega.reachable_flag = False
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertTrue(p.exists())
        self.assertEqual(self.row(item)["status"], "published")


class IntakeTests(Base):
    def test_intake_video_moved_to_converted(self):
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "_TO_CONVERT" / "v.mp4")
        item = self.add(self.d, "_TO_CONVERT/v.mp4", source="intake")
        self.assertEqual(self.run_item(item), "done")
        self.assertTrue((self.d.root / "_CONVERTED" / "v_compressed.mp4").exists())
        self.assertEqual(list((self.d.root / "_TO_CONVERT").iterdir()), [])
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "1")
        self.assertEqual(self.row(item)["relpath"], "_CONVERTED/v_compressed.mp4")

    def test_intake_rejected_original_moved(self):
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.01)
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "_TO_CONVERT" / "v.mp4")
        (self.d.root / "_CONVERTED").mkdir()
        (self.d.root / "_CONVERTED" / "v.mp4").write_bytes(b"x")
        item = self.add(self.d, "_TO_CONVERT/v.mp4", source="intake")
        self.assertEqual(self.run_item(item), "rejected")
        self.assertTrue((self.d.root / "_CONVERTED" / "v (1).mp4").exists())
        self.assertEqual(self.row(item)["relpath"], "_CONVERTED/v (1).mp4")
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "1")


class DownloadsTests(Base):
    def add_dl(self, name, kind="image"):
        return self.add(self.dl, name, kind=kind, source="downloads",
                        dest_location="mega", dest_dir="TBC")

    def test_png_converted_and_moved(self):
        make_image(self.dl.root / "pic.png")
        item = self.add_dl("pic.png")
        self.assertEqual(self.run_item(item), "done")
        self.assertTrue((self.mega.root / "TBC" / "pic_compressed.webp").exists())
        self.assertFalse((self.dl.root / "pic.png").exists())
        self.assertIsNone(self.row(item))
        (c,) = self.conversions()
        self.assertEqual((c["location"], c["relpath"]), ("mega", "TBC/pic_compressed.webp"))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_webp_moved_without_conversion(self):
        (self.dl.root / "pic.webp").write_bytes(b"webpdata")
        item = self.add_dl("pic.webp")
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual((self.mega.root / "TBC" / "pic.webp").read_bytes(), b"webpdata")
        self.assertFalse((self.dl.root / "pic.webp").exists())
        self.assertEqual(self.conversions(), [])

    def test_efficient_original_moved_with_free_name(self):
        make_video(self.dl.root / "v.mp4", codec="libx265")
        (self.mega.root / "TBC").mkdir()
        (self.mega.root / "TBC" / "v.mp4").write_bytes(b"x")
        item = self.add_dl("v.mp4", kind="video")
        self.cfg = test_config(self.tmp, min_free_gb=0.001, min_gain_ratio=0.01)
        self.assertEqual(self.run_item(item), "done")
        self.assertTrue((self.mega.root / "TBC" / "v (1).mp4").exists())
        self.assertFalse((self.dl.root / "v.mp4").exists())

    def test_mac_unreachable(self):
        make_image(self.dl.root / "pic.png")
        item = self.add_dl("pic.png")
        self.mega.reachable_flag = False
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        self.assertTrue((self.dl.root / "pic.png").exists())
        self.assertEqual(self.row(item)["status"], "queued")

    def test_conversion_error_keeps_source(self):
        (self.dl.root / "bad.mp4").write_text("nope")
        item = self.add_dl("bad.mp4", kind="video")
        self.assertEqual(self.run_item(item), "error")
        self.assertTrue((self.dl.root / "bad.mp4").exists())
        self.assertEqual(self.row(item)["status"], "error")

    def test_source_changed_before_delete_kept(self):
        make_image(self.dl.root / "pic.png")
        item = self.add_dl("pic.png")
        orig_publish = self.mega.publish

        def publish_then_touch(src, relpath, mtime, min_free):
            n = orig_publish(src, relpath, mtime, min_free)
            with open(self.dl.root / "pic.png", "ab") as f:
                f.write(b"edit")
            return n
        self.mega.publish = publish_then_touch
        self.assertEqual(self.run_item(item), "rejected")
        self.assertTrue((self.dl.root / "pic.png").exists())
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("rejected", "changed"))


class RecoveryTests(Base):
    def forbid_convert(self):
        p = mock.patch("media_organizer.worker.convert",
                       side_effect=AssertionError("must not re-encode"))
        p.start()
        self.addCleanup(p.stop)

    def requeue_claim(self, item):
        return db.claim_next(self.con, kind=item["kind"], exclude_locations=())

    def test_library_landed_publish_not_reencoded(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.error_after_publish = MacUnreachable("dropped after rename")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        r = self.row(item)
        self.assertEqual((r["status"], r["final_relpath"]), ("queued", "x_compressed.mp4"))
        self.assertEqual(r["final_size"], (self.mega.root / "x_compressed.mp4").stat().st_size)
        self.mega.error_after_publish = None
        self.forbid_convert()
        item = self.requeue_claim(item)
        self.assertEqual(self.run_item(item), "published")
        self.assertEqual(sorted(f.name for f in self.mega.root.iterdir()),
                         ["x.mp4", "x_compressed.mp4"])
        self.mega.clock = self.row(item)["not_before"] + 1
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.row(item)["status"], "done")
        self.assertEqual(len(self.conversions()), 1)

    def test_reset_running_keeps_recorded_publish(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.error_after_publish = MacUnreachable("x")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        db.set_status(self.con, item["id"], "running")
        db.reset_running(self.con)
        self.assertEqual(self.row(item)["final_relpath"], "x_compressed.mp4")

    def test_downloads_landed_publish_not_duplicated(self):
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        self.mega.error_after_publish = MacUnreachable("dropped after rename")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        r = self.row(item)
        self.assertEqual((r["status"], r["final_relpath"]), ("queued", "TBC/pic_compressed.webp"))
        self.mega.error_after_publish = None
        self.forbid_convert()
        item = self.requeue_claim(item)
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual([f.name for f in (self.mega.root / "TBC").iterdir()],
                         ["pic_compressed.webp"])
        self.assertFalse((self.dl.root / "pic.png").exists())
        self.assertIsNone(self.row(item))
        self.assertEqual(len(self.conversions()), 1)

    def test_downloads_landed_but_source_changed_keeps_source(self):
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        self.mega.error_after_publish = MacUnreachable("x")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        with open(self.dl.root / "pic.png", "ab") as f:
            f.write(b"edit")
        self.mega.error_after_publish = None
        item = self.requeue_claim(item)
        self.assertEqual(self.run_item(item), "queued")
        self.assertTrue((self.dl.root / "pic.png").exists())
        r = self.row(item)
        self.assertEqual((r["final_relpath"], r["final_size"]), (None, None))

    def test_library_changed_original_drops_recorded_publish(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.error_after_publish = MacUnreachable("x")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        with open(p, "ab") as f:
            f.write(b"edit")
        self.mega.error_after_publish = None
        item = self.requeue_claim(item)
        self.assertEqual(self.run_item(item), "queued")
        r = self.row(item)
        self.assertEqual((r["final_relpath"], r["final_size"]), (None, None))

    def test_recorded_final_missing_processed_normally(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        db.set_status(self.con, item["id"], "running", final_relpath="x_compressed.mp4",
                      final_size=1234)
        item = self.row(item)
        self.assertEqual(self.run_item(item), "published")
        r = self.row(item)
        self.assertEqual(r["final_relpath"], "x_compressed.mp4")
        self.assertEqual(r["final_size"], (self.mega.root / "x_compressed.mp4").stat().st_size)

    def test_recorded_final_wrong_size_processed_normally(self):
        make_video(self.mega.root / "x.mp4")
        (self.mega.root / "x_compressed.mp4").write_bytes(b"other")
        item = self.add(self.mega, "x.mp4")
        db.set_status(self.con, item["id"], "running", final_relpath="x_compressed.mp4",
                      final_size=1234)
        item = self.row(item)
        self.assertEqual(self.run_item(item), "published")
        self.assertEqual(self.row(item)["final_relpath"], "x_mp4_compressed.mp4")

    def test_no_space_clears_recorded_publish(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = NoSpace("full")
        self.run_item(item)
        r = self.row(item)
        self.assertEqual((r["final_relpath"], r["final_size"]), (None, None))

    def test_online_only_flag_sticks_across_retry(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        db.set_status(self.con, item["id"], "running", was_online_only=1)
        item = self.row(item)
        self.assertEqual(self.run_item(item), "published")
        self.assertEqual(self.row(item)["was_online_only"], 1)
        self.mega.clock = self.row(item)["not_before"] + 1
        worker.finalize_published(self.con, self.locs, self.cfg)
        self.assertEqual(self.mega.evicted, ["x_compressed.mp4"])

    def test_intake_stale_row_at_target_replaced(self):
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "_TO_CONVERT" / "v.mp4")
        item = self.add(self.d, "_TO_CONVERT/v.mp4", source="intake")
        db.upsert_candidate(self.con, location="d", relpath="_CONVERTED/v_compressed.mp4",
                            size=1, mtime=1.0, kind="video", source="intake", priority=1,
                            settings="s")
        self.assertEqual(self.run_item(item), "done")
        rows = self.con.execute("SELECT * FROM items WHERE location='d'").fetchall()
        self.assertEqual([(r["id"], r["relpath"], r["status"]) for r in rows],
                         [(item["id"], "_CONVERTED/v_compressed.mp4", "done")])

    def test_post_step_failure_keeps_status_and_is_retried(self):
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "_TO_CONVERT" / "v.mp4")
        item = self.add(self.d, "_TO_CONVERT/v.mp4", source="intake")
        real = self.d.move_within
        calls = []

        def flaky(rel, new):
            calls.append(rel)
            if len(calls) == 1:
                raise OSError("disk hiccup")
            real(rel, new)
        self.d.move_within = flaky
        with self.assertLogs("media_organizer.worker", logging.ERROR):
            self.assertEqual(self.run_item(item), "done")
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("done", "post-step-failed"))
        self.assertTrue((self.d.root / "_TO_CONVERT" / "v_compressed.mp4").exists())
        self.assertIsNone(db.meta_get(self.con, "sorter_dirty"))
        worker.finalize_published(self.con, self.locs, self.cfg)
        self.assertTrue((self.d.root / "_CONVERTED" / "v_compressed.mp4").exists())
        self.assertEqual(list((self.d.root / "_TO_CONVERT").iterdir()), [])
        r = self.row(item)
        self.assertEqual((r["status"], r["relpath"]), ("done", "_CONVERTED/v_compressed.mp4"))
        self.assertEqual(db.meta_get(self.con, "sorter_dirty"), "1")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
