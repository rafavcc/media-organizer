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
        # a done row is never pruned: its original is gone because we deleted it
        self.assertEqual(sorted(r["relpath"] for r in self.rows()), ["gone.mp4", "keep.mp4"])
        self.assertFalse((self.cfg.d_root / "stale.mp4.mo-part").exists())
        self.assertTrue((self.cfg.d_root / "fresh.mp4.mo-part").exists())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_full_scan_still_prunes_skipped_and_rejected_rows_whose_file_is_gone(self):
        age(make_video(self.cfg.d_root / "keep.mp4"))
        for rel, status in (("skip.mp4", "skipped"), ("rej.mp4", "rejected"),
                            ("done.mp4", "done")):
            db.upsert_candidate(self.con, location="d", relpath=rel, size=5, mtime=1.0,
                                kind="video", source="library", priority=3, settings="s")
            rid = self.con.execute("SELECT id FROM items WHERE relpath=?", (rel,)).fetchone()["id"]
            db.set_status(self.con, rid, status)
        scan_library(self.con, self.d, self.cfg, full=True)
        self.assertEqual(sorted(r["relpath"] for r in self.rows()), ["done.mp4", "keep.mp4"])

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
        self.assertEqual([r["relpath"] for r in rows],
                         ["pic.png", "pic.webp", "small.mp4", "sub/inner.png"])
        for r in rows:
            self.assertEqual((r["location"], r["dest_location"], r["dest_dir"], r["priority"],
                              r["source"]), ("win_dl", "mega", "TBC", 0, "downloads"))
        self.assertEqual(self.rows()[1]["kind"], "image")
        self.assertEqual((res.moved, res.queued), (1, 4))
        self.assertEqual(res.moved, 1)

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


class DryRunTests(ScannerCase):
    def locs(self):
        return {"d": self.d, "win_dl": self.dl}

    def test_library_dry_run_keeps_parts_and_rows(self):
        gone = self.cfg.d_root / "a" / "gone.mp4"
        write(gone)
        scan_library(self.con, self.d, self.cfg, full=True)
        gone.unlink()
        stale = write(self.cfg.d_root / "a" / "x.mp4.mo-part", mtime=time.time() - 3 * 86400)
        write(self.cfg.d_root / "a" / "new.png")
        self.con.execute("DELETE FROM folders")
        res = scan_library(self.con, self.d, self.cfg, full=True, dry_run=True)
        self.assertTrue(stale.exists())
        self.assertEqual(res.queued, 1)
        rels = [r["relpath"] for r in self.rows()]
        self.assertIn("a/gone.mp4", rels)  # not pruned
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM folders").fetchone()[0], 0)

    def test_downloads_dry_run_moves_nothing(self):
        big = self.cfg.win_downloads / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(400 * MIB + 1)
        age(big)
        write(self.cfg.win_downloads / "pic.png")
        res = scan_downloads(self.con, self.dl, self.locs(), self.cfg, dry_run=True)
        self.assertTrue(big.exists())
        self.assertFalse((self.cfg.d_root / "_TO_CONVERT").exists())
        self.assertEqual(res.would_move, [("big.mp4", "d/_TO_CONVERT/big.mp4")])
        self.assertEqual((res.moved, res.queued), (0, 1))
        self.assertEqual([r["relpath"] for r in self.rows()], ["pic.png"])

    def test_downloads_dry_run_avoids_name_collision(self):
        big = self.cfg.win_downloads / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(400 * MIB + 1)
        age(big)
        write(self.cfg.d_root / "_TO_CONVERT" / "big.mp4")
        res = scan_downloads(self.con, self.dl, self.locs(), self.cfg, dry_run=True)
        self.assertEqual(res.would_move, [("big.mp4", "d/_TO_CONVERT/big (1).mp4")])


def _big(path: Path, size=400 * MIB + 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return age(path)


class RecursiveSweepTests(ScannerCase):
    def locs(self):
        return {"d": self.d, "win_dl": self.dl}

    def scan(self, cfg=None, **kw):
        return scan_downloads(self.con, self.dl, self.locs(), cfg or self.cfg, **kw)

    def test_nested_files_routed_like_top_level(self):
        dl = self.cfg.win_downloads
        _big(dl / "a" / "b" / "big.mp4")
        write(dl / "a" / "small.mp4")
        write(dl / "x" / "pic.png")
        res = self.scan()
        self.assertTrue((self.cfg.d_root / "_TO_CONVERT" / "big.mp4").exists())
        self.assertFalse((dl / "a" / "b" / "big.mp4").exists())
        rows = self.rows()
        self.assertEqual([r["relpath"] for r in rows], ["a/small.mp4", "x/pic.png"])
        self.assertTrue(all(r["dest_dir"] == "TBC" and r["priority"] == 0 for r in rows))
        self.assertEqual((res.found, res.moved, res.queued), (3, 1, 2))

    def test_collision_between_subfolders_gets_free_names(self):
        dl = self.cfg.win_downloads
        _big(dl / "a" / "big.mp4")
        _big(dl / "b" / "big.mp4", 400 * MIB + 2)
        res = self.scan()
        self.assertEqual(res.moved, 2)
        names = sorted(p.name for p in (self.cfg.d_root / "_TO_CONVERT").iterdir())
        self.assertEqual(names, ["big (1).mp4", "big.mp4"])

    def test_nested_rules_still_apply(self):
        dl = self.cfg.win_downloads
        write(dl / "a" / "foo_compressed.png")
        write(dl / "a" / "young.png", mtime=time.time())
        write(dl / "a" / "empty.png", data=b"")
        write(dl / "a" / "x.mp4.part")
        write(dl / "a" / ".dot.png")
        write(dl / "a" / "doc.pdf")
        self.scan()
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(list((dl / "a").iterdir())), 6)

    def test_dot_folder_untouched(self):
        dl = self.cfg.win_downloads
        write(dl / ".hid" / "pic.png")
        write(dl / ".hid" / "a.nfo")
        (dl / ".hid" / "empty").mkdir()
        age((dl / ".hid" / "empty"))
        (dl / ".hid2").mkdir()
        age(dl / ".hid2")
        res = self.scan()
        self.assertEqual(self.rows(), [])
        self.assertTrue((dl / ".hid" / "a.nfo").exists())
        self.assertTrue((dl / ".hid" / "empty").is_dir())
        self.assertTrue((dl / ".hid2").is_dir())
        self.assertEqual((res.nfo_deleted, res.dirs_removed), (0, 0))

    def test_nfo_deleted_in_any_case_and_subfolder_only_when_old(self):
        dl = self.cfg.win_downloads
        write(dl / "a.nfo")
        write(dl / "s" / "t" / "B.NFO")
        write(dl / "s" / "c.Nfo")
        write(dl / "young.nfo", mtime=time.time())
        write(dl / "keep.txt")
        write(dl / "nfo")
        write(dl / "x.nfo.txt")
        res = self.scan()
        self.assertEqual(res.nfo_deleted, 3)
        self.assertEqual(sorted(p.name for p in dl.rglob("*") if p.is_file()),
                         ["keep.txt", "nfo", "x.nfo.txt", "young.nfo"])

    def test_other_files_left_and_their_folder_kept(self):
        dl = self.cfg.win_downloads
        write(dl / "docs" / "sub" / "a.pdf")
        write(dl / "docs" / "b.txt")
        age(dl / "docs" / "sub")
        age(dl / "docs")
        res = self.scan()
        self.assertEqual(res.dirs_removed, 0)
        self.assertTrue((dl / "docs" / "sub" / "a.pdf").exists())

    def test_empty_folders_removed_bottom_up(self):
        dl = self.cfg.win_downloads
        for rel in ("a/b/c", "a/d", "e"):
            (dl / rel).mkdir(parents=True)
        for rel in ("a/b/c", "a/b", "a/d", "a", "e"):
            age(dl / rel)
        res = self.scan()
        self.assertEqual(res.dirs_removed, 5)
        self.assertEqual(list(dl.iterdir()), [])
        self.assertTrue(dl.is_dir())

    def test_folder_emptied_by_nfo_is_left_for_the_next_sweep(self):
        dl = self.cfg.win_downloads
        write(dl / "a" / "b" / "x.nfo")
        age(dl / "a" / "b")
        age(dl / "a")
        res = self.scan()
        self.assertEqual((res.nfo_deleted, res.dirs_removed), (1, 0))  # mtime changed by delete
        self.assertTrue((dl / "a" / "b").exists())

    def test_junk_files_removed_with_folder(self):
        dl = self.cfg.win_downloads
        for n in (".DS_Store", "Thumbs.db", "DESKTOP.INI"):
            write(dl / "j" / n)
        write(dl / "k" / ".DS_Store")
        write(dl / "k" / "real.txt")
        for rel in ("j", "k"):
            age(dl / rel)
        res = self.scan()
        self.assertEqual(res.dirs_removed, 1)
        self.assertFalse((dl / "j").exists())
        self.assertTrue((dl / "k" / ".DS_Store").exists())

    def test_junk_in_young_folder_is_kept(self):
        dl = self.cfg.win_downloads
        write(dl / "j" / ".DS_Store")
        (dl / "j").touch()  # folder mtime now
        os.utime(dl / "j")
        res = self.scan()
        self.assertEqual(res.dirs_removed, 0)
        self.assertTrue((dl / "j" / ".DS_Store").exists())

    def test_young_folder_kept(self):
        dl = self.cfg.win_downloads
        (dl / "new").mkdir()
        res = self.scan()
        self.assertEqual(res.dirs_removed, 0)
        self.assertTrue((dl / "new").is_dir())

    def test_root_never_removed(self):
        dl = self.cfg.win_downloads
        age(dl)
        self.scan()
        self.assertTrue(dl.is_dir())

    def test_dry_run_changes_nothing_and_lists(self):
        dl = self.cfg.win_downloads
        write(dl / "a" / "x.NFO")
        (dl / "e" / "f").mkdir(parents=True)
        age(dl / "e" / "f")
        age(dl / "e")
        write(dl / "j" / "Thumbs.db")
        age(dl / "j")
        res = self.scan(dry_run=True)
        self.assertEqual(res.would_delete, ["a/x.NFO"])
        self.assertEqual(sorted(res.would_remove), ["e/f", "j"])
        self.assertTrue((dl / "a" / "x.NFO").exists())
        self.assertTrue((dl / "e" / "f").is_dir())
        self.assertTrue((dl / "j" / "Thumbs.db").exists())
        self.assertEqual((res.nfo_deleted, res.dirs_removed), (0, 0))

    def test_symlinks_are_not_followed(self):
        dl = self.cfg.win_downloads
        outside = self.root / "outside"
        write(outside / "pic.png")
        write(outside / "x.nfo")
        age(outside)
        write(dl / "real" / "pic2.png")
        try:
            os.symlink(outside, dl / "link")
            os.symlink(dl, dl / "real" / "loop")
            os.symlink(outside / "pic.png", dl / "real" / "pic_link.png")
            os.symlink(outside / "x.nfo", dl / "real" / "n.nfo")
        except OSError:
            self.skipTest("symlinks unavailable")
        res = self.scan()
        self.assertEqual([r["relpath"] for r in self.rows()], ["real/pic2.png"])
        self.assertTrue((outside / "pic.png").exists())
        self.assertTrue((outside / "x.nfo").exists())
        self.assertTrue(os.path.islink(dl / "real" / "n.nfo"))
        self.assertTrue(os.path.islink(dl / "link"))
        self.assertEqual((res.nfo_deleted, res.dirs_removed), (0, 0))

    def test_symlink_only_folder_is_not_empty(self):
        dl = self.cfg.win_downloads
        (dl / "f").mkdir()
        try:
            os.symlink(self.root, dl / "f" / "l")
        except OSError:
            self.skipTest("symlinks unavailable")
        age(dl / "f")
        res = self.scan()
        self.assertEqual(res.dirs_removed, 0)
        self.assertTrue((dl / "f" / "l").is_symlink())

    def test_unreadable_folder_is_skipped_and_kept(self):
        dl = self.cfg.win_downloads
        write(dl / "ok" / "pic.png")
        write(dl / "locked" / "inner" / "x.png")
        (dl / "empty").mkdir()
        for rel in ("locked/inner", "locked", "empty"):
            age(dl / rel)
        os.chmod(dl / "locked" / "inner", 0)
        self.addCleanup(os.chmod, dl / "locked" / "inner", 0o755)
        if os.access(dl / "locked" / "inner", os.R_OK):
            self.skipTest("permissions not enforced")
        with self.assertLogs("media_organizer.locations", "WARNING") as logs:
            res = self.scan()
        self.assertIn("locked/inner", logs.output[0])
        self.assertEqual([r["relpath"] for r in self.rows()], ["ok/pic.png"])
        self.assertTrue((dl / "locked" / "inner").is_dir())
        self.assertFalse((dl / "empty").exists())  # the rest of the sweep still ran
        self.assertEqual(res.dirs_removed, 1)

    def test_library_full_scan_does_not_prune_under_unreadable_folder(self):
        write(self.cfg.d_root / "a" / "x.mp4")
        write(self.cfg.d_root / "b" / "y.mp4")
        write(self.cfg.d_root / "c" / "z.mp4")
        scan_library(self.con, self.d, self.cfg, full=True)
        os.chmod(self.cfg.d_root / "a", 0)
        self.addCleanup(os.chmod, self.cfg.d_root / "a", 0o755)
        if os.access(self.cfg.d_root / "a", os.R_OK):
            self.skipTest("permissions not enforced")
        (self.cfg.d_root / "b" / "y.mp4").unlink()
        with self.assertLogs("media_organizer.locations", "WARNING") as logs:
            scan_library(self.con, self.d, self.cfg, full=True)
        self.assertEqual(len(logs.output), 1)  # logged once per scan, not again by refresh_folders
        self.assertEqual([r["relpath"] for r in self.rows("d")], ["a/x.mp4", "c/z.mp4"])

    def test_folder_without_search_permission_is_kept(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        dl = self.cfg.win_downloads
        write(dl / "ok" / "pic.png")
        write(dl / "closed" / "x.txt")
        (dl / "closed" / "empty").mkdir()
        (dl / "j").mkdir()
        write(dl / "j" / "Thumbs.db")
        for rel in ("closed/empty", "closed", "j"):
            age(dl / rel)
        os.chmod(dl / "closed", 0o644)
        self.addCleanup(os.chmod, dl / "closed", 0o755)
        os.chmod(dl / "j", 0o644)
        self.addCleanup(os.chmod, dl / "j", 0o755)
        with self.assertLogs("media_organizer.locations", "WARNING"):
            res = self.scan()
        self.assertEqual([r["relpath"] for r in self.rows()], ["ok/pic.png"])
        self.assertEqual(res.dirs_removed, 0)
        os.chmod(dl / "closed", 0o755)
        os.chmod(dl / "j", 0o755)
        self.assertTrue((dl / "closed" / "empty").is_dir())
        self.assertTrue((dl / "j" / "Thumbs.db").exists())

    def test_package_folders_are_untouched(self):
        dl = self.cfg.win_downloads
        write(dl / "Foo.APP" / "Contents" / "Resources" / "icon.png")
        write(dl / "Foo.APP" / "Contents" / "Resources" / "i.nfo")
        (dl / "Foo.APP" / "Contents" / "Frameworks").mkdir()
        (dl / "Lib.photoslibrary").mkdir()
        write(dl / "x.pkg")  # a file, not a package folder: not media, left alone
        for p in dl.rglob("*"):
            if p.is_dir():
                age(p)
        res = self.scan()
        self.assertEqual(self.rows(), [])
        self.assertEqual((res.nfo_deleted, res.dirs_removed), (0, 0))
        self.assertTrue((dl / "Foo.APP" / "Contents" / "Resources" / "icon.png").exists())
        self.assertTrue((dl / "Foo.APP" / "Contents" / "Resources" / "i.nfo").exists())
        self.assertTrue((dl / "Foo.APP" / "Contents" / "Frameworks").is_dir())
        self.assertTrue((dl / "Lib.photoslibrary").is_dir())

    def test_junk_directory_named_like_junk_keeps_folder(self):
        dl = self.cfg.win_downloads
        write(dl / "j" / "Thumbs.db")
        (dl / "j" / ".DS_Store").mkdir()
        age(dl / "j")
        res = self.scan()
        self.assertEqual(res.dirs_removed, 0)
        self.assertTrue((dl / "j" / "Thumbs.db").exists())

    def test_mac_rmdir_failure_is_unreachable(self):
        from tests.test_locations import mac_location
        loc, _ = mac_location(self.root)
        (loc.root / "e").mkdir()
        age(loc.root / "e")
        with mock.patch("media_organizer.locations.os.rmdir",
                        side_effect=OSError(107, "not connected")):
            with self.assertRaises(MacUnreachable):
                scan_downloads(self.con, loc, {"d": self.d, "mac_dl": loc}, self.cfg)
        self.assertTrue((loc.root / "e").is_dir())
