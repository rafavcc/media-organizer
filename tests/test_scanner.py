import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from media_organizer import db
from media_organizer.locations import LocalLocation, MacUnreachable
from media_organizer.scanner import ScanResult, refresh_folders, scan_downloads, scan_library
from tests.helpers import make_video, test_config

OLD = time.time() - 3600
MIB = 1024 * 1024


def age(path, mtime=OLD):
    os.utime(path, (mtime, mtime))
    return path


def write(path: Path, data=b"x" * 100, mtime=OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return age(path, mtime)


class ScannerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = test_config(self.root)
        self.cfg.d_root.mkdir()
        self.cfg.win_downloads.mkdir()
        self.con = db.open_db(self.root / "state" / "o.db")
        self.addCleanup(self.con.close)
        self.d = LocalLocation("d", self.cfg.d_root)
        self.dl = LocalLocation("win_dl", self.cfg.win_downloads)

    def rows(self, location=None):
        sql, args = "SELECT * FROM items", ()
        if location:
            sql, args = sql + " WHERE location=?", (location,)
        return self.con.execute(sql + " ORDER BY relpath", args).fetchall()


class LibraryScanTests(ScannerCase):
    def test_queues_only_eligible_files(self):
        a = self.cfg.d_root / "a"
        a.mkdir()
        age(make_video(a / "x.mp4"))
        age(make_video(a / "y_compressed.mp4"))
        write(a / "z.txt")
        age(make_video(self.cfg.d_root / ".hid.mp4"))
        write(self.cfg.d_root / ".hid" / "w.mp4")
        make_video(a / "new.mp4")
        res = scan_library(self.con, self.d, self.cfg, full=False)
        rows = self.rows()
        self.assertEqual([r["relpath"] for r in rows], ["a/x.mp4"])
        r = rows[0]
        self.assertEqual((r["status"], r["source"], r["priority"], r["kind"]),
                         ("queued", "library", 3, "video"))
        self.assertEqual((res.queued, res.known), (1, 0))

    def test_hevc_low_bitrate_is_skipped_as_efficient(self):
        age(make_video(self.cfg.d_root / "h.mp4", codec="libx265",
                       extra=["-b:v", "50k", "-tag:v", "hvc1"]))
        scan_library(self.con, self.d, self.cfg, full=False)
        r = self.rows()[0]
        self.assertEqual((r["status"], r["reason"]), ("skipped", "efficient"))

    def test_intake_folder(self):
        (self.cfg.d_root / "_TO_CONVERT").mkdir()
        age(make_video(self.cfg.d_root / "_TO_CONVERT" / "i.mp4"))
        scan_library(self.con, self.d, self.cfg, full=False)
        r = self.rows()[0]
        self.assertEqual((r["source"], r["priority"]), ("intake", 1))

    def test_second_scan_is_known(self):
        age(make_video(self.cfg.d_root / "x.mp4"))
        scan_library(self.con, self.d, self.cfg, full=False)
        res = scan_library(self.con, self.d, self.cfg, full=False)
        self.assertEqual((res.queued, res.known), (0, 1))

    def test_moved_file_is_adopted(self):
        (self.cfg.d_root / "a").mkdir()
        age(make_video(self.cfg.d_root / "a" / "x.mp4"))
        scan_library(self.con, self.d, self.cfg, full=True)
        db.set_status(self.con, self.rows()[0]["id"], "rejected", reason="no-gain")
        (self.cfg.d_root / "b").mkdir()
        self.d.move_within("a/x.mp4", "b/x.mp4")
        res = scan_library(self.con, self.d, self.cfg, full=True)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["relpath"], rows[0]["status"]), ("b/x.mp4", "rejected"))
        self.assertEqual(res.adopted, 1)

    def test_full_scan_prunes_and_cleans_parts(self):
        age(make_video(self.cfg.d_root / "keep.mp4"))
        db.upsert_candidate(self.con, location="d", relpath="gone.mp4", size=5, mtime=1.0,
                            kind="video", source="library", priority=3, settings="s")
        db.set_status(self.con, self.rows()[0]["id"], "done")
        write(self.cfg.d_root / "stale.mp4.mo-part", mtime=time.time() - 2 * 86400)
        write(self.cfg.d_root / "fresh.mp4.mo-part", mtime=time.time() - 3600)
        scan_library(self.con, self.d, self.cfg, full=True)
        self.assertEqual([r["relpath"] for r in self.rows()], ["keep.mp4"])
        self.assertFalse((self.cfg.d_root / "stale.mp4.mo-part").exists())
        self.assertTrue((self.cfg.d_root / "fresh.mp4.mo-part").exists())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_partial_scan_keeps_part_files(self):
        write(self.cfg.d_root / "stale.mp4.mo-part", mtime=time.time() - 2 * 86400)
        scan_library(self.con, self.d, self.cfg, full=False)
        self.assertTrue((self.cfg.d_root / "stale.mp4.mo-part").exists())


class EmptyRootTests(ScannerCase):
    def populate(self):
        (self.cfg.d_root / "a").mkdir()
        age(make_video(self.cfg.d_root / "a" / "x.mp4"))
        scan_library(self.con, self.d, self.cfg, full=True)
        db.set_status(self.con, self.rows()[0]["id"], "rejected", reason="no-gain")

    def state(self):
        return ([tuple(r) for r in self.rows()],
                [tuple(r) for r in self.con.execute("SELECT * FROM folders")],
                self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def test_emptied_root_is_suspect_and_keeps_everything(self):
        self.populate()
        before = self.state()
        shutil.rmtree(self.cfg.d_root / "a")
        res = scan_library(self.con, self.d, self.cfg, full=True)
        self.assertTrue(res.suspect)
        self.assertEqual(self.state(), before)

    def test_missing_root_returns_empty_result(self):
        self.populate()
        before = self.state()
        shutil.rmtree(self.cfg.d_root)
        self.assertEqual(scan_library(self.con, self.d, self.cfg, full=True), ScanResult())
        self.assertEqual(scan_downloads(self.con, self.d, {"d": self.d}, self.cfg), ScanResult())
        refresh_folders(self.con, self.d)
        self.assertEqual(self.state(), before)

    def test_empty_root_without_rows_is_not_suspect(self):
        res = scan_library(self.con, self.d, self.cfg, full=True)
        self.assertFalse(res.suspect)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_empty_sub_with_rows_elsewhere_is_not_suspect(self):
        self.populate()
        (self.cfg.d_root / "b").mkdir()
        res = scan_library(self.con, self.d, self.cfg, sub="b", full=True)
        self.assertFalse(res.suspect)
        self.assertEqual(len(self.rows()), 1)

    def test_stale_part_alone_is_not_an_empty_walk(self):
        self.populate()
        shutil.rmtree(self.cfg.d_root / "a")
        write(self.cfg.d_root / "stale.mp4.mo-part", mtime=time.time() - 2 * 86400)
        res = scan_library(self.con, self.d, self.cfg, full=True)
        self.assertFalse(res.suspect)
        self.assertEqual(self.rows(), [])


class DownloadsTests(ScannerCase):
    def locs(self):
        return {"d": self.d, "win_dl": self.dl}

    def test_routes_downloads(self):
        big = self.cfg.win_downloads / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(400 * MIB + 1)
        age(big)
        for name in ("small.mp4", "pic.png", "pic.webp", "doc.pdf", "dl.mp4.crdownload"):
            write(self.cfg.win_downloads / name)
        write(self.cfg.win_downloads / "sub" / "inner.png")
        res = scan_downloads(self.con, self.dl, self.locs(), self.cfg)
        moved = self.cfg.d_root / "_TO_CONVERT" / "big.mp4"
        self.assertEqual(moved.stat().st_size, 400 * MIB + 1)
        moved.unlink()  # free the temp space right away
        self.assertFalse(big.exists())
        rows = self.rows()
        self.assertEqual([r["relpath"] for r in rows], ["pic.png", "pic.webp", "small.mp4"])
        for r in rows:
            self.assertEqual((r["location"], r["dest_location"], r["dest_dir"], r["priority"],
                              r["source"]), ("win_dl", "mega", "TBC", 0, "downloads"))
        self.assertEqual(self.rows()[1]["kind"], "image")
        self.assertTrue((self.cfg.win_downloads / "sub" / "inner.png").exists())
        self.assertEqual((res.moved, res.queued), (1, 3))

    def test_compressed_name_left_alone(self):
        write(self.cfg.win_downloads / "foo_compressed.png")
        scan_downloads(self.con, self.dl, self.locs(), self.cfg)
        self.assertEqual(self.rows(), [])
        self.assertTrue((self.cfg.win_downloads / "foo_compressed.png").exists())

    def test_young_files_wait(self):
        write(self.cfg.win_downloads / "small.mp4", mtime=time.time())
        scan_downloads(self.con, self.dl, self.locs(), self.cfg)
        self.assertEqual(self.rows(), [])

    def test_name_collision(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        write(cfg.d_root / "_TO_CONVERT" / "big.mp4")
        write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertEqual((cfg.d_root / "_TO_CONVERT" / "big (1).mp4").stat().st_size, 2 * MIB)
        self.assertFalse((cfg.win_downloads / "big.mp4").exists())

    def test_no_space_leaves_file(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        src = write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        with mock.patch.object(self.d, "free_bytes", return_value=1024 ** 3):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(src.exists())
        self.assertEqual((res.waiting, res.moved), (1, 0))
        self.assertFalse((cfg.d_root / "_TO_CONVERT").exists())

    def test_unreachable_d_waits(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        src = write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        with mock.patch.object(self.d, "reachable", return_value=False):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(src.exists())
        self.assertEqual(res.waiting, 1)

    def big_pair(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        a = write(cfg.win_downloads / "a.mp4", data=b"y" * (2 * MIB))
        b = write(cfg.win_downloads / "b.mp4", data=b"z" * (2 * MIB))
        return cfg, a, b

    def test_source_rewritten_during_copy_is_kept(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        src = write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        real = self.d.publish

        def publish_then_touch(*args, **kw):
            out = real(*args, **kw)
            age(src, OLD + 100)
            return out

        with mock.patch.object(self.d, "publish", side_effect=publish_then_touch):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(src.exists())
        self.assertEqual(list((cfg.d_root / "_TO_CONVERT").iterdir()), [])
        self.assertEqual((res.waiting, res.moved), (1, 0))

    def test_returned_size_mismatch_keeps_source(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        src = write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        real = self.d.publish
        with mock.patch.object(self.d, "publish",
                               side_effect=lambda *a, **k: real(*a, **k) + 1):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(src.exists())
        self.assertEqual(list((cfg.d_root / "_TO_CONVERT").iterdir()), [])
        self.assertEqual((res.waiting, res.moved), (1, 0))

    def test_publish_oserror_skips_that_file_only(self):
        cfg, a, b = self.big_pair()
        real = self.d.publish
        calls = []

        def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("source grew")
            return real(*args, **kw)

        with mock.patch.object(self.d, "publish", side_effect=flaky):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual([p.name for p in (cfg.d_root / "_TO_CONVERT").iterdir()], ["b.mp4"])
        self.assertEqual((res.waiting, res.moved), (1, 1))

    def test_publish_oserror_removes_leftovers(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        src = write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))

        def leave_part(src_path, target, *a, **k):
            part = cfg.d_root / (target + ".mo-part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"half")
            raise OSError("boom")

        with mock.patch.object(self.d, "publish", side_effect=leave_part):
            res = scan_downloads(self.con, self.dl, self.locs(), cfg)
        self.assertTrue(src.exists())
        self.assertEqual(list((cfg.d_root / "_TO_CONVERT").iterdir()), [])
        self.assertEqual(res.waiting, 1)

    def test_mac_unreachable_still_propagates(self):
        cfg = replace(self.cfg, size_threshold_mb=1)
        write(cfg.win_downloads / "big.mp4", data=b"y" * (2 * MIB))
        with mock.patch.object(self.d, "publish", side_effect=MacUnreachable("down")):
            with self.assertRaises(MacUnreachable):
                scan_downloads(self.con, self.dl, self.locs(), cfg)


class RefreshFoldersTests(ScannerCase):
    def test_folder_stats(self):
        write(self.cfg.d_root / "A" / "p_compressed.mp4", b"1" * 10)
        write(self.cfg.d_root / "A" / "q.mp4", b"2" * 20)
        refresh_folders(self.con, self.d)
        rows = [tuple(r) for r in self.con.execute(
            "SELECT folder, bytes, files, compressed_files FROM folders WHERE location='d'")]
        self.assertEqual(rows, [("A", 30, 2, 1)])
        snap = self.con.execute("SELECT * FROM snapshots").fetchone()
        self.assertEqual((snap["location"], snap["total_bytes"]), ("d", 30))

    def test_done_row_counts_and_root_files(self):
        write(self.cfg.d_root / "top.mp4", b"1" * 10)
        db.upsert_candidate(self.con, location="d", relpath="top.mp4", size=10, mtime=OLD,
                            kind="video", source="library", priority=3, settings="s")
        db.set_status(self.con, self.rows()[0]["id"], "done")
        refresh_folders(self.con, self.d)
        row = self.con.execute("SELECT * FROM folders").fetchone()
        self.assertEqual((row["folder"], row["files"], row["compressed_files"]), (".", 1, 1))


if __name__ == "__main__":
    unittest.main()
