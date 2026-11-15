import logging
import math
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from media_organizer import db, worker
from media_organizer.convert import Outcome
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
        self.evict_fail = 0  # number of evict calls that fail before one succeeds
        self.evict_effective = True  # a successful evict leaves the file online-only
        self.free = None  # override free_bytes
        self.fetched = []

    def free_bytes(self):
        return super().free_bytes() if self.free is None else self.free

    def fetch(self, relpath, dest):
        self.fetched.append(relpath)
        super().fetch(relpath, dest)

    def now(self):
        return super().now() if self.clock is None else self.clock

    def reachable(self):
        return self.reachable_flag

    def is_online_only(self, relpath):
        return relpath in self.online_only

    def evict(self, relpath):
        self.evicted.append(relpath)
        if self.evict_fail > 0:
            self.evict_fail -= 1
            return False
        if self.evict_effective:
            self.online_only.add(relpath)
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
        self.assertLess(abs(out.stat().st_mtime - time.time()), 30)  # carries the conversion time
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


class NoSpaceBackoffTests(Base):
    def queue(self, loc, rel, size_bytes=None, **kw):
        st = loc.path(rel).stat()
        db.upsert_candidate(self.con, location=loc.name, relpath=rel, size=st.st_size,
                            mtime=st.st_mtime, kind="video", source="library", priority=2,
                            settings="s", **kw)
        return self.con.execute("SELECT * FROM items WHERE location=? AND relpath=?",
                                (loc.name, rel)).fetchone()

    def claim(self, now=None):
        return db.claim_next(self.con, kind="video", exclude_locations=(), now=now)

    def assert_backed_off(self, item, start):
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"]), ("queued", "no-space"))
        self.assertGreaterEqual(r["not_before"], start + 3600 - 5)
        self.assertLessEqual(r["not_before"], time.time() + 3600 + 5)
        self.assertEqual((r["final_relpath"], r["final_size"]), (None, None))
        return r

    def test_low_destination_space_backs_off_without_fetching(self):
        make_video(self.mega.root / "big.mp4", size="640x480")
        make_video(self.mega.root / "small.mp4", size="64x64")
        big = self.queue(self.mega, "big.mp4")
        small = self.queue(self.mega, "small.mp4")
        self.assertEqual(self.claim()["relpath"], "big.mp4")
        self.mega.free = 10
        start = time.time()
        self.assertEqual(self.run_item(big), "queued")
        self.assertEqual(self.mega.fetched, [])
        r = self.assert_backed_off(big, start)
        self.assertEqual(self.claim()["relpath"], "small.mp4")  # next-largest, not the same item
        self.assertIsNone(self.claim())
        self.assertEqual(self.claim(now=r["not_before"] + 1)["relpath"], "big.mp4")
        self.assertEqual(self.row(small)["status"], "running")

    def test_required_space_is_three_quarters_plus_min_free(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        need = math.ceil(p.stat().st_size * 0.75) + int(self.cfg.min_free_gb * 1024 ** 3)
        self.mega.free = need - 1
        self.assertEqual(self.run_item(item), "queued")
        self.assertEqual(self.mega.fetched, [])
        db.set_status(self.con, item["id"], "running", not_before=None)
        self.mega.free = need
        self.run_item(self.row(item))  # the final publish may still refuse (test ratio is 0.9)
        self.assertEqual(self.mega.fetched, ["x.mp4"])  # but the pre-check let it through

    def test_no_space_from_publish_backs_off(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = NoSpace("full")
        start = time.time()
        self.assertEqual(self.run_item(item), "queued")
        r = self.assert_backed_off(item, start)
        self.assertIsNone(self.claim())
        self.assertIsNotNone(self.claim(now=r["not_before"] + 1))

    def test_downloads_item_checks_destination(self):
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        self.mega.free = 10
        start = time.time()
        self.assertEqual(self.run_item(item), "queued")
        self.assert_backed_off(item, start)
        self.assertTrue((self.dl.root / "pic.png").exists())

    def test_unreachable_mac_during_space_check_is_plain_unreachable(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        with mock.patch.object(self.mega, "free_bytes", side_effect=MacUnreachable("gone")):
            with self.assertRaises(MacUnreachable):
                self.run_item(item)
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"], r["not_before"]), ("queued", None, None))

    def test_work_folder_space_is_checked(self):
        p = make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        real = shutil.disk_usage
        usage = namedtuple("usage", "total used free")

        def du(path):
            if str(path).startswith(str(self.work)):
                return usage(10 ** 12, 0, 2 * p.stat().st_size - 1)
            return real(path)
        start = time.time()
        with mock.patch("shutil.disk_usage", du):
            self.assertEqual(self.run_item(item), "queued")
        self.assert_backed_off(item, start)
        self.assertTrue(p.exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_work_folder_space_checked_for_downloads(self):
        p = make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        real = shutil.disk_usage
        usage = namedtuple("usage", "total used free")

        def du(path):
            if str(path).startswith(str(self.work)):
                return usage(10 ** 12, 0, 1)
            return real(path)
        with mock.patch("shutil.disk_usage", du):
            self.assertEqual(self.run_item(item), "queued")
        self.assertEqual(self.row(item)["reason"], "no-space")
        self.assertTrue(p.exists())


class NameChoiceTests(Base):
    def run_threads(self, pairs, barrier_timeout=1.0):
        """Run process_item for each (location, relpath) in its own thread and connection,
        pausing every thread between name choice and publish until all have arrived."""
        items = []
        for loc, rel in pairs:
            st = loc.path(rel).stat()
            db.upsert_candidate(self.con, location=loc.name, relpath=rel, size=st.st_size,
                                mtime=st.st_mtime, kind="image", source="library", priority=2,
                                settings="s")
        for _ in pairs:
            items.append(db.claim_next(self.con, kind="image", exclude_locations=()))
        barrier = threading.Barrier(len(pairs))
        real_final = worker.rules.final_name

        def final_then_wait(*a, **k):
            name = real_final(*a, **k)
            try:
                barrier.wait(timeout=barrier_timeout)
            except threading.BrokenBarrierError:
                pass
            return name
        results, errors = {}, []

        def go(item):
            con = db.open_db(self.tmp / "state" / "o.db")
            try:
                results[item["relpath"]] = worker.process_item(con, item, self.locs, self.cfg,
                                                               self.work)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
            finally:
                con.close()
        with mock.patch.object(worker.rules, "final_name", final_then_wait):
            threads = [threading.Thread(target=go, args=(i,)) for i in items]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        return results

    def test_same_stem_different_extension_get_distinct_outputs(self):
        make_image(self.d.root / "a.png")
        make_image(self.d.root / "a.jpg", size="320x240")
        res = self.run_threads([(self.d, "a.png"), (self.d, "a.jpg")])
        self.assertEqual(sorted(res.values()), ["done", "done"])
        outs = sorted(p.name for p in self.d.root.iterdir())
        self.assertEqual(len(outs), 2, outs)
        self.assertTrue(all(n.endswith(".webp") for n in outs))
        self.assertEqual(len(self.conversions()), 2)
        rows = self.con.execute("SELECT * FROM items").fetchall()
        for r in rows:
            self.assertEqual(r["status"], "done")
            self.assertEqual((self.d.root / r["final_relpath"]).stat().st_size, r["final_size"])
        self.assertEqual(len({r["final_relpath"] for r in rows}), 2)

    def test_case_variants_get_distinct_names(self):
        make_image(self.d.root / "IMG.JPG", size="320x240")
        make_image(self.d.root / "img.png")
        res = self.run_threads([(self.d, "IMG.JPG"), (self.d, "img.png")])
        self.assertEqual(sorted(res.values()), ["done", "done"])
        outs = [p.name for p in self.d.root.iterdir()]
        self.assertEqual(len(outs), 2)
        self.assertEqual(len({n.casefold() for n in outs}), 2, outs)

    def test_existing_file_differing_by_case_is_taken(self):
        make_image(self.d.root / "a.png")
        (self.d.root / "A_COMPRESSED.webp").write_bytes(b"theirs")
        item = self.add(self.d, "a.png", kind="image")
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual((self.d.root / "A_COMPRESSED.webp").read_bytes(), b"theirs")
        self.assertTrue((self.d.root / "a_png_compressed.webp").exists())

    def test_file_exists_error_requeues_with_plan_cleared(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = FileExistsError("taken")
        self.assertEqual(self.run_item(item), "queued")
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"], r["final_relpath"], r["final_size"]),
                         ("queued", "name-taken", None, None))
        self.assertIsNone(r["error"])
        self.assertTrue((self.mega.root / "x.mp4").exists())

    def test_file_exists_error_on_downloads_requeues(self):
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        self.mega.publish_error = FileExistsError("taken")
        self.assertEqual(self.run_item(item), "queued")
        r = self.row(item)
        self.assertEqual((r["status"], r["reason"], r["final_relpath"]),
                         ("queued", "name-taken", None))
        self.assertTrue((self.dl.root / "pic.png").exists())


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


class EvictRetryTests(Base):
    def published(self, rel="x.mp4", online_only=False):
        p = make_video(self.mega.root / rel)
        if online_only:
            self.mega.online_only.add(rel)
        item = self.add(self.mega, rel)
        self.assertEqual(self.run_item(item), "published")
        return p, item

    def finish(self, online_only=True):
        p, item = self.published(online_only=online_only)
        self.mega.clock = self.row(item)["not_before"] + 1
        return p, item

    def tick(self):
        return worker.finalize_published(self.con, self.locs, self.cfg)

    def flag(self, item):
        return self.row(item)["evict_pending"]

    def test_failed_evict_is_retried_until_it_works(self):
        p, item = self.finish()
        self.mega.evict_fail = 2
        self.tick()  # deletes the original; first evict fails
        self.assertEqual((self.row(item)["status"], self.flag(item)), ("done", 1))
        self.tick()
        self.assertEqual(self.flag(item), 1)
        self.tick()
        self.assertEqual(self.flag(item), 0)
        self.assertEqual(len(self.mega.evicted), 3)
        self.tick()
        self.assertEqual(len(self.mega.evicted), 3)  # cleared: no further attempts

    def test_evict_that_does_not_take_effect_keeps_flag(self):
        p, item = self.finish()
        self.mega.evict_effective = False
        self.tick()
        self.tick()
        self.assertEqual(self.flag(item), 1)
        self.assertEqual(len(self.mega.evicted), 2)

    def test_offline_item_never_flagged(self):
        p, item = self.finish(online_only=False)
        self.tick()
        self.assertEqual(self.flag(item), 0)

    def test_gives_up_after_seven_days(self):
        p, item = self.finish()
        self.mega.evict_fail = 99
        self.tick()
        finished = self.row(item)["finished_at"]
        calls = len(self.mega.evicted)
        with self.assertLogs("media_organizer.worker", logging.INFO):
            worker.retry_evictions(self.con, self.locs, self.cfg, now=finished + 7 * 86400 + 1)
        self.assertEqual(self.flag(item), 0)
        self.assertEqual(len(self.mega.evicted), calls)
        self.assertIsNone(self.row(item)["reason"])

    def test_retries_before_seven_days(self):
        p, item = self.finish()
        self.mega.evict_fail = 1
        self.tick()
        finished = self.row(item)["finished_at"]
        n = worker.retry_evictions(self.con, self.locs, self.cfg, now=finished + 6 * 86400)
        self.assertEqual((n, self.flag(item)), (1, 0))

    def test_unreachable_mac_untouched(self):
        p, item = self.finish()
        self.mega.evict_fail = 1
        self.tick()
        calls = len(self.mega.evicted)
        self.mega.reachable_flag = False
        self.assertEqual(worker.retry_evictions(self.con, self.locs, self.cfg), 0)
        self.assertEqual((self.flag(item), len(self.mega.evicted)), (1, calls))

    def test_is_up_callable_replaces_reachable(self):
        p, item = self.finish()
        self.mega.evict_fail = 1
        self.tick()
        self.mega.reachable_flag = False  # would skip, but the service says it is up
        n = worker.retry_evictions(self.con, self.locs, self.cfg, is_up=lambda name: True)
        self.assertEqual((n, self.flag(item)), (1, 0))

    def test_limit_and_oldest_first(self):
        rows = []
        for i, fin in enumerate((300.0, 100.0, 200.0)):
            make_video(self.mega.root / f"v{i}.mp4")
            it = self.add(self.mega, f"v{i}.mp4")
            self.assertEqual(self.run_item(it), "published")
            db.set_status(self.con, it["id"], "done", evict_pending=1, finished_at=fin,
                          final_relpath=f"v{i}_compressed.mp4")
            rows.append(it)
        n = worker.retry_evictions(self.con, self.locs, self.cfg, limit=2, now=400.0)
        self.assertEqual(n, 2)
        self.assertEqual(self.mega.evicted, ["v1_compressed.mp4", "v2_compressed.mp4"])

    def test_finalize_uses_is_up_not_reachable(self):
        p, item = self.finish(online_only=False)
        self.mega.reachable_flag = False
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg,
                                                   is_up=lambda name: True), 1)
        self.assertFalse(p.exists())
        p2, item2 = self.published("y.mp4")
        self.mega.clock = self.row(item2)["not_before"] + 1
        self.mega.reachable_flag = True
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg,
                                                   is_up=lambda name: False), 0)
        self.assertTrue(p2.exists())


class FinalizeFailureTests(Base):
    def library_published(self):
        p = make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        with mock.patch.object(self.d, "delete", side_effect=PermissionError("read-only")), \
                self.assertLogs("media_organizer.worker", logging.ERROR):
            self.assertEqual(self.run_item(item), "published")
        return p, item

    def tick_failing(self):
        with mock.patch.object(self.d, "delete", side_effect=PermissionError("read-only")), \
                self.assertLogs("media_organizer.worker", logging.ERROR):
            worker.finalize_published(self.con, self.locs, self.cfg)

    def test_counter_then_error_then_retry_finishes(self):
        p, item = self.library_published()
        self.assertEqual(self.row(item)["finalize_fails"], 0)  # the immediate finalize is free
        for expected in (1, 2, 3, 4):
            self.tick_failing()
            r = self.row(item)
            self.assertEqual((r["status"], r["finalize_fails"]), ("published", expected))
        self.tick_failing()
        r = self.row(item)
        self.assertEqual((r["status"], r["finalize_fails"]), ("error", 5))
        self.assertTrue(r["error"].startswith("could not delete original: "), r["error"])
        self.assertIn("read-only", r["error"])
        self.assertTrue(p.exists())
        self.assertTrue((self.d.root / "x_compressed.mp4").exists())
        self.assertEqual(db.retry_errors(self.con), 1)
        self.assertEqual(self.row(item)["finalize_fails"], 0)
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        self.assertEqual(self.run_item(item), "done")
        self.assertFalse(p.exists())
        self.assertEqual(len(self.conversions()), 1)

    def test_final_file_mtime_mismatch_keeps_original(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.assertEqual(self.run_item(item), "published")
        final = self.mega.root / "x_compressed.mp4"
        os.utime(final, (1_000_000_000, 1_000_000_000))
        self.mega.clock = self.row(item)["not_before"] + 1
        with self.assertLogs("media_organizer.worker", logging.WARNING):
            self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 0)
        self.assertTrue(p.exists())
        self.assertEqual(self.row(item)["status"], "queued")


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

    def fake_convert(self, size=50):
        def convert(local, name, src_size, tmp, cfg, on_progress=None):
            out = Path(tmp) / "out.bin"
            out.write_bytes(b"c" * size)
            return Outcome("ok", "", out)
        p = mock.patch("media_organizer.worker.convert", side_effect=convert)
        p.start()
        self.addCleanup(p.stop)

    def test_planned_name_of_other_item_not_taken(self):
        self.fake_convert()
        a = self.dl.root / "pic.png"
        b = self.dl.root / "pic.jpg"
        a.write_bytes(b"a" * 200)
        b.write_bytes(b"b" * 300)
        os.utime(a, (1_500_000_000, 1_500_000_000))
        os.utime(b, (1_600_000_000, 1_600_000_000))
        item_a = self.add_dl("pic.png")
        db.set_status(self.con, item_a["id"], "running", final_relpath="TBC/pic_compressed.webp",
                      final_size=50)  # A's publish was cut off before the rename
        item_a = self.row(item_a)
        item_b = self.add_dl("pic.jpg")
        self.assertEqual(self.row(item_b)["relpath"], "pic.jpg")
        self.assertEqual(self.run_item(item_b), "done")
        self.assertFalse((self.mega.root / "TBC" / "pic_compressed.webp").exists())
        self.assertEqual(self.run_item(item_a), "done")
        self.assertEqual(sorted(f.name for f in (self.mega.root / "TBC").iterdir()),
                         ["pic_compressed.webp", "pic_jpg_compressed.webp"])
        self.assertLess(abs((self.mega.root / "TBC" / "pic_compressed.webp").stat().st_mtime
                            - time.time()), 30)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(len(self.conversions()), 2)

    def test_landed_file_with_other_mtime_is_not_ours(self):
        a = self.dl.root / "pic.png"
        a.write_bytes(b"a" * 200)
        os.utime(a, (1_500_000_000, 1_500_000_000))
        item = self.add_dl("pic.png")
        self.fake_convert()
        (self.mega.root / "TBC").mkdir()
        other = self.mega.root / "TBC" / "pic_compressed.webp"
        other.write_bytes(b"o" * 50)
        os.utime(other, (1_400_000_000, 1_400_000_000))
        db.set_status(self.con, item["id"], "running", final_relpath="TBC/pic_compressed.webp",
                      final_size=50, final_mtime=1_500_000_000.0)
        self.assertEqual(self.run_item(self.row(item)), "done")
        self.assertEqual(other.read_bytes(), b"o" * 50)
        self.assertEqual(sorted(f.name for f in (self.mega.root / "TBC").iterdir()),
                         ["pic_compressed.webp", "pic_png_compressed.webp"])

    def test_library_landed_file_with_other_mtime_is_not_ours(self):
        p = make_video(self.mega.root / "x.mp4")
        os.utime(p, (1_500_000_000, 1_500_000_000))
        other = self.mega.root / "x_compressed.mp4"
        other.write_bytes(b"o" * 99)
        os.utime(other, (1_400_000_000, 1_400_000_000))
        item = self.add(self.mega, "x.mp4")
        db.set_status(self.con, item["id"], "running", final_relpath="x_compressed.mp4",
                      final_size=99, final_mtime=1_500_000_000.0)
        self.run_item(self.row(item))  # the final publish may still refuse (test ratio is 0.9)
        self.assertEqual(self.mega.fetched, ["x.mp4"])  # but the pre-check let it through
        self.assertEqual(self.row(item)["final_relpath"], "x_mp4_compressed.mp4")

    def test_unconverted_compressed_named_webp_logs_no_conversion(self):
        (self.dl.root / "foo_compressed.webp").write_bytes(b"webpdata")
        item = self.add_dl("foo_compressed.webp")
        self.mega.error_after_publish = MacUnreachable("x")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        self.mega.error_after_publish = None
        item = self.requeue_claim(item)
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual(self.conversions(), [])
        self.assertTrue((self.mega.root / "TBC" / "foo_compressed.webp").exists())
        self.assertFalse((self.dl.root / "foo_compressed.webp").exists())

    def test_intake_post_step_avoids_planned_name(self):
        (self.d.root / "_TO_CONVERT").mkdir()
        make_video(self.d.root / "_TO_CONVERT" / "v.mp4")
        item = self.add(self.d, "_TO_CONVERT/v.mp4", source="intake")
        db.upsert_candidate(self.con, location="d", relpath="other.mp4", size=1, mtime=1.0,
                            kind="video", source="library", priority=3, settings="s")
        other = self.con.execute("SELECT id FROM items WHERE relpath='other.mp4'").fetchone()
        db.set_status(self.con, other["id"], "queued",
                      final_relpath="_CONVERTED/v_compressed.mp4", final_size=1)
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual(self.row(item)["relpath"], "_CONVERTED/v_compressed (1).mp4")

    def add_dl(self, name, kind="image"):
        return self.add(self.dl, name, kind=kind, source="downloads",
                        dest_location="mega", dest_dir="TBC")

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


def _no_reachable(loc):
    loc.reachable = mock.Mock(side_effect=AssertionError("worker called reachable()"))


class WorkerNeverProbesMacTests(Base):
    def test_library_finalize_skips_mac_rows_and_never_probes(self):
        _no_reachable(self.mega)
        make_video(self.mega.root / "m.mp4")
        m = self.add(self.mega, "m.mp4")
        db.set_status(self.con, m["id"], "published", final_relpath="m_compressed.mp4",
                      final_size=1, not_before=0.0)  # a Mac row that is due
        make_video(self.d.root / "x.mp4")
        item = self.add(self.d, "x.mp4")
        self.assertEqual(worker.process_item(self.con, item, self.locs, self.cfg, self.work,
                                             is_up=lambda name: True), "done")
        self.assertEqual(self.row(m)["status"], "published")  # not touched from this thread

    def test_downloads_to_mac_uses_is_up(self):
        _no_reachable(self.mega)
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        self.assertEqual(worker.process_item(self.con, item, self.locs, self.cfg, self.work,
                                             is_up=lambda name: True), "done")
        self.assertTrue((self.mega.root / "TBC" / "pic_compressed.webp").exists())

    def test_downloads_is_up_false_is_unreachable(self):
        _no_reachable(self.mega)
        make_image(self.dl.root / "pic.png")
        item = self.add(self.dl, "pic.png", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        with self.assertRaises(MacUnreachable):
            worker.process_item(self.con, item, self.locs, self.cfg, self.work,
                                is_up=lambda name: False)
        self.assertEqual(self.row(item)["status"], "queued")
        self.assertTrue((self.dl.root / "pic.png").exists())


class ConversionStampTests(Base):
    def test_converted_file_carries_conversion_time_and_original_is_deleted(self):
        p = make_video(self.d.root / "x.mp4")
        os.utime(p, (1_500_000_000, 1_500_000_000))
        item = self.add(self.d, "x.mp4")
        before = time.time()
        self.assertEqual(self.run_item(item), "done")
        out = self.d.root / "x_compressed.mp4"
        self.assertGreaterEqual(out.stat().st_mtime, int(before) - 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.row(item)["final_mtime"], out.stat().st_mtime)

    def test_filesystem_that_changes_the_stamp_still_finalizes(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        real = os.utime

        def skewed(path, times=None, **kw):
            real(path, (times[0] + 500, times[1] + 500), **kw)
        with mock.patch("os.utime", skewed):
            self.assertEqual(self.run_item(item), "published")
        final = self.mega.root / "x_compressed.mp4"
        r = self.row(item)
        self.assertEqual(r["final_mtime"], final.stat().st_mtime)  # the observed one
        self.mega.clock = r["not_before"] + 1
        self.assertEqual(worker.finalize_published(self.con, self.locs, self.cfg), 1)
        self.assertFalse(p.exists())

    def test_unconverted_payload_keeps_original_mtime(self):
        f = self.dl.root / "pic.webp"
        f.write_bytes(b"webpdata")
        os.utime(f, (1_500_000_000, 1_500_000_000))
        item = self.add(self.dl, "pic.webp", kind="image", source="downloads",
                        dest_location="mega", dest_dir="TBC")
        db_row = {}
        orig = self.mega.publish

        def spy(src, relpath, mtime, min_free):
            db_row.update(self.row(item))
            return orig(src, relpath, mtime, min_free)
        self.mega.publish = spy
        self.assertEqual(self.run_item(item), "done")
        self.assertEqual(db_row["final_mtime"], 1_500_000_000)
        self.assertEqual((self.mega.root / "TBC" / "pic.webp").stat().st_mtime, 1_500_000_000)

    def test_shortcut_matches_on_planned_stamp(self):
        p = make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.error_after_publish = MacUnreachable("dropped after rename")
        with self.assertRaises(MacUnreachable):
            self.run_item(item)
        r = self.row(item)
        final = self.mega.root / "x_compressed.mp4"
        self.assertEqual(r["final_mtime"], float(int(r["final_mtime"])))
        self.assertLessEqual(abs(final.stat().st_mtime - r["final_mtime"]), 2)
        self.mega.error_after_publish = None
        mock.patch("media_organizer.worker.convert",
                   side_effect=AssertionError("must not re-encode")).start()
        self.addCleanup(mock.patch.stopall)
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        self.assertEqual(self.run_item(item), "published")

    def test_foreign_same_size_file_with_other_mtime_is_rejected(self):
        p = make_video(self.mega.root / "x.mp4")
        other = self.mega.root / "x_compressed.mp4"
        other.write_bytes(b"o" * 99)
        os.utime(other, (1_400_000_000, 1_400_000_000))
        item = self.add(self.mega, "x.mp4")
        db.set_status(self.con, item["id"], "running", final_relpath="x_compressed.mp4",
                      final_size=99, final_mtime=time.time())
        self.run_item(self.row(item))
        self.assertEqual(self.mega.fetched, ["x.mp4"])  # went on to convert, not the shortcut
        self.assertEqual(other.read_bytes(), b"o" * 99)

    def test_requeue_and_lost_clear_final_mtime(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.assertEqual(self.run_item(item), "published")
        (self.mega.root / "x_compressed.mp4").unlink()
        self.mega.clock = self.row(item)["not_before"] + 1
        with self.assertLogs("media_organizer.worker", logging.WARNING):
            worker.finalize_published(self.con, self.locs, self.cfg)
        r = self.row(item)
        self.assertEqual((r["status"], r["final_mtime"]), ("queued", None))
        db.set_status(self.con, item["id"], "done", final_relpath="a", final_size=1,
                      final_mtime=5.0)
        self.assertEqual(db.upsert_candidate(
            self.con, location="mega", relpath="x.mp4", size=1, mtime=9.0, kind="video",
            source="library", priority=2, settings="s"), "requeued")
        self.assertIsNone(self.row(item)["final_mtime"])


class FinalizeFailureCountTests(FinalizeFailureTests):
    def test_immediate_finalize_failures_in_one_tick_do_not_error_the_row(self):
        p, item = self.library_published()
        with mock.patch.object(self.d, "delete", side_effect=PermissionError("ro")), \
                self.assertLogs("media_organizer.worker", logging.ERROR):
            for _ in range(5):
                worker._after_published(self.con, self.row(item), self.d, self.cfg)
        r = self.row(item)
        self.assertEqual((r["status"], r["finalize_fails"]), ("published", 0))

    def test_success_and_requeue_reset_the_counter(self):
        p, item = self.library_published()
        db.set_status(self.con, item["id"], "published", finalize_fails=3)
        worker.finalize_published(self.con, self.locs, self.cfg)
        r = self.row(item)
        self.assertEqual((r["status"], r["finalize_fails"]), ("done", 0))
        make_video(self.d.root / "y.mp4")
        y = self.add(self.d, "y.mp4")
        db.set_status(self.con, y["id"], "published", final_relpath="gone.mp4", final_size=1,
                      final_mtime=1.0, finalize_fails=3, not_before=0.0)
        with self.assertLogs("media_organizer.worker", logging.WARNING):
            worker.finalize_published(self.con, self.locs, self.cfg)
        r = self.row(y)
        self.assertEqual((r["status"], r["finalize_fails"]), ("queued", 0))


class EvictPendingClearedTests(Base):
    def done_pending(self, rel="x.mp4"):
        make_video(self.d.root / rel)
        it = self.add(self.d, rel)
        db.set_status(self.con, it["id"], "done", evict_pending=1, final_relpath="f", final_size=1)
        return it

    def test_upsert_requeue_clears_flag(self):
        it = self.done_pending()
        self.assertEqual(db.upsert_candidate(
            self.con, location="d", relpath="x.mp4", size=1, mtime=123.0, kind="video",
            source="library", priority=3, settings="s"), "requeued")
        self.assertEqual(self.row(it)["evict_pending"], 0)

    def test_retry_errors_clears_flag(self):
        it = self.done_pending()
        db.set_status(self.con, it["id"], "error", evict_pending=1)
        db.retry_errors(self.con)
        self.assertEqual(self.row(it)["evict_pending"], 0)

    def test_lost_clears_flag(self):
        make_video(self.d.root / "x.mp4")
        it = self.add(self.d, "x.mp4")
        db.set_status(self.con, it["id"], "published", final_relpath="gone.mp4", final_size=1,
                      final_mtime=1.0, evict_pending=1, not_before=0.0)
        with self.assertLogs("media_organizer.worker", logging.WARNING):
            worker.finalize_published(self.con, self.locs, self.cfg)
        self.assertEqual(self.row(it)["evict_pending"], 0)


class EvictBudgetTests(Base):
    def pending(self, n):
        ids = []
        for i in range(n):
            make_video(self.mega.root / f"v{i}.mp4")
            it = self.add(self.mega, f"v{i}.mp4")
            db.set_status(self.con, it["id"], "done", evict_pending=1, finished_at=100.0 + i,
                          final_relpath=f"v{i}_c.mp4", final_size=1)
            ids.append(it)
        return ids

    def test_stops_at_first_mac_unreachable(self):
        self.pending(3)
        calls = []

        def evict(rel):
            calls.append(rel)
            raise MacUnreachable("gone")
        self.mega.evict = evict
        n = worker.retry_evictions(self.con, self.locs, self.cfg, now=200.0)
        self.assertEqual((n, calls), (0, ["v0_c.mp4"]))

    def test_spends_at_most_sixty_seconds(self):
        self.pending(5)
        ticks = iter([0.0, 0.0, 30.0, 61.0, 61.0, 61.0, 61.0, 61.0])
        with mock.patch.object(worker.time, "monotonic", lambda: next(ticks)):
            worker.retry_evictions(self.con, self.locs, self.cfg, now=200.0)
        self.assertEqual(len(self.mega.evicted), 2)


class NameTakenBackoffTests(Base):
    def test_name_taken_requeue_backs_off(self):
        make_video(self.mega.root / "x.mp4")
        item = self.add(self.mega, "x.mp4")
        self.mega.publish_error = FileExistsError("taken")
        before = time.time()
        self.assertEqual(self.run_item(item), "queued")
        nb = self.row(item)["not_before"]
        self.assertGreaterEqual(nb, before + 59)
        self.assertLessEqual(nb, time.time() + 61)
        self.assertIsNone(db.claim_next(self.con, kind="video", exclude_locations=()))


class StalledEncoderItemTests(Base):
    def test_stalled_encode_ends_error_with_message(self):
        make_video(self.d.root / "x.mp4", extra=["-b:v", "4M"])
        item = self.add(self.d, "x.mp4")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        f = bindir / "ffmpeg"
        f.write_text("#!/bin/sh\necho out_time_us=1000\nexec sleep 60\n")
        f.chmod(0o755)
        self.cfg = test_config(self.tmp, min_free_gb=0.001, encode_stall_min=0.03)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            self.assertEqual(self.run_item(item), "error")
        r = self.row(item)
        self.assertEqual(r["status"], "error")
        self.assertIn("encoder stalled", r["error"])
        self.assertTrue((self.d.root / "x.mp4").exists())
