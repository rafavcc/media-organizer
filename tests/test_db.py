import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import db


def add(con, relpath, *, location="d", size=10, mtime=1.0, kind="video", source="library",
        priority=3, settings="s1", **kw):
    return db.upsert_candidate(con, location=location, relpath=relpath, size=size, mtime=mtime,
                               kind=kind, source=source, priority=priority, settings=settings, **kw)


def row(con, relpath, location="d"):
    return con.execute("SELECT * FROM items WHERE location=? AND relpath=?",
                       (location, relpath)).fetchone()


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "state.db"
        self.con = db.open_db(self.path)
        self.addCleanup(self.con.close)


class UpsertTests(DbTestCase):
    def test_queued_then_known(self):
        self.assertEqual(add(self.con, "a.mp4"), "queued")
        self.assertEqual(add(self.con, "a.mp4"), "known")

    def test_open_db_twice(self):
        con2 = db.open_db(self.path)
        self.addCleanup(con2.close)
        add(self.con, "a.mp4")
        self.assertEqual(add(con2, "a.mp4"), "known")

    def test_rejected_same_settings_known(self):
        add(self.con, "a.mp4")
        db.set_status(self.con, row(self.con, "a.mp4")["id"], "rejected", reason="big")
        self.assertEqual(add(self.con, "a.mp4"), "known")

    def test_rejected_new_settings_requeued(self):
        add(self.con, "a.mp4")
        db.set_status(self.con, row(self.con, "a.mp4")["id"], "rejected", reason="big", attempts=2)
        self.assertEqual(add(self.con, "a.mp4", settings="s2"), "requeued")
        r = row(self.con, "a.mp4")
        self.assertEqual(r["status"], "queued")
        self.assertIsNone(r["reason"])
        self.assertEqual(r["settings"], "s2")
        self.assertEqual(r["attempts"], 2)

    def test_rejected_new_size_requeued(self):
        add(self.con, "a.mp4")
        db.set_status(self.con, row(self.con, "a.mp4")["id"], "rejected")
        self.assertEqual(add(self.con, "a.mp4", size=11), "requeued")
        r = row(self.con, "a.mp4")
        self.assertEqual((r["status"], r["size"]), ("queued", 11))

    def test_error_known_until_retry(self):
        add(self.con, "a.mp4")
        db.set_status(self.con, row(self.con, "a.mp4")["id"], "error", error="boom")
        self.assertEqual(add(self.con, "a.mp4"), "known")
        self.assertEqual(db.retry_errors(self.con), 1)
        self.assertEqual(row(self.con, "a.mp4")["status"], "queued")

    def test_queued_changed_file_requeued(self):
        add(self.con, "a.mp4")
        self.assertEqual(add(self.con, "a.mp4", mtime=2.0), "requeued")
        self.assertEqual(row(self.con, "a.mp4")["mtime"], 2.0)

    def test_queued_changed_file_drops_recorded_publish(self):
        add(self.con, "a.mp4")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        db.set_status(self.con, item["id"], "queued", final_relpath="a_compressed.mp4",
                      final_size=5)
        self.assertEqual(add(self.con, "a.mp4"), "known")
        self.assertEqual(row(self.con, "a.mp4")["final_relpath"], "a_compressed.mp4")
        self.assertEqual(add(self.con, "a.mp4", size=11), "requeued")
        r = row(self.con, "a.mp4")
        self.assertEqual((r["final_relpath"], r["final_size"]), (None, None))

    def test_running_and_published_left_alone(self):
        add(self.con, "a.mp4")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        self.assertEqual(add(self.con, "a.mp4", size=99), "known")
        self.assertEqual(row(self.con, "a.mp4")["size"], 10)
        db.set_status(self.con, item["id"], "published", not_before=0)
        self.assertEqual(add(self.con, "a.mp4", size=99), "known")
        self.assertEqual(row(self.con, "a.mp4")["status"], "published")


class ClaimTests(DbTestCase):
    def test_order(self):
        add(self.con, "a", size=10, priority=3)
        add(self.con, "b", size=1, priority=0)
        add(self.con, "c", size=99, priority=3)
        got = [db.claim_next(self.con, kind="video", exclude_locations=())["relpath"]
               for _ in range(3)]
        self.assertEqual(got, ["b", "c", "a"])
        self.assertIsNone(db.claim_next(self.con, kind="video", exclude_locations=()))

    def test_claim_sets_running(self):
        add(self.con, "a")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        r = row(self.con, "a")
        self.assertEqual((r["status"], r["attempts"]), ("running", 1))
        self.assertIsNotNone(r["started_at"])
        self.assertEqual(item["id"], r["id"])

    def test_kind_filter(self):
        add(self.con, "v.mp4", kind="video")
        self.assertIsNone(db.claim_next(self.con, kind="image", exclude_locations=()))
        add(self.con, "i.jpg", kind="image")
        self.assertEqual(db.claim_next(self.con, kind="image", exclude_locations=())["relpath"],
                         "i.jpg")

    def test_exclude_locations(self):
        add(self.con, "a", location="mega", priority=0)
        add(self.con, "b", location="d", priority=1, dest_location="mega")
        add(self.con, "c", location="d", priority=2)
        item = db.claim_next(self.con, kind="video", exclude_locations={"mega"})
        self.assertEqual(item["relpath"], "c")
        self.assertIsNone(db.claim_next(self.con, kind="video", exclude_locations={"mega"}))

    def test_reset_running(self):
        add(self.con, "a")
        db.claim_next(self.con, kind="video", exclude_locations=())
        self.assertEqual(db.reset_running(self.con), 1)
        self.assertEqual(row(self.con, "a")["status"], "queued")

    def test_reset_running_keeps_recorded_publish(self):
        add(self.con, "a")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        db.set_status(self.con, item["id"], "running", final_relpath="a_c", final_size=4)
        db.reset_running(self.con)
        r = row(self.con, "a")
        self.assertEqual((r["status"], r["final_relpath"], r["final_size"]), ("queued", "a_c", 4))

    def test_published_survives_restart(self):
        add(self.con, "a")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        db.set_status(self.con, item["id"], "published", final_relpath="x_compressed.mp4",
                      final_size=3, not_before=0)
        self.con.close()
        con2 = db.open_db(self.path)
        self.addCleanup(con2.close)
        self.assertEqual(db.reset_running(con2), 0)
        self.assertEqual(row(con2, "a")["status"], "published")

    def test_due_published(self):
        add(self.con, "a")
        item = db.claim_next(self.con, kind="video", exclude_locations=())
        db.set_status(self.con, item["id"], "published", not_before=100)
        self.assertEqual(len(db.due_published(self.con, 100)), 1)
        self.assertEqual(db.due_published(self.con, 99), [])

    def test_set_status_rejects_unknown_column(self):
        add(self.con, "a")
        with self.assertRaises(ValueError):
            db.set_status(self.con, row(self.con, "a")["id"], "done", bogus=1)

    def test_delete_item(self):
        add(self.con, "a")
        db.delete_item(self.con, row(self.con, "a")["id"])
        self.assertIsNone(row(self.con, "a"))


class BackoffTests(DbTestCase):
    def test_claim_skips_future_not_before(self):
        add(self.con, "big", size=99)
        add(self.con, "small", size=1)
        db.set_status(self.con, row(self.con, "big")["id"], "queued", not_before=5000.0,
                      reason="no-space")
        got = db.claim_next(self.con, kind="video", exclude_locations=(), now=1000.0)
        self.assertEqual(got["relpath"], "small")
        self.assertIsNone(db.claim_next(self.con, kind="video", exclude_locations=(), now=1000.0))
        got = db.claim_next(self.con, kind="video", exclude_locations=(), now=5000.0)
        self.assertEqual(got["relpath"], "big")

    def test_claim_default_now_is_time_time(self):
        add(self.con, "a")
        db.set_status(self.con, row(self.con, "a")["id"], "queued", not_before=time.time() + 3600)
        self.assertIsNone(db.claim_next(self.con, kind="video", exclude_locations=()))
        with mock.patch("media_organizer.db.time.time", return_value=time.time() + 7200):
            self.assertIsNotNone(db.claim_next(self.con, kind="video", exclude_locations=()))

    def test_upsert_requeued_clears_not_before(self):
        add(self.con, "a")
        db.set_status(self.con, row(self.con, "a")["id"], "queued", not_before=9e9,
                      reason="no-space")
        self.assertEqual(add(self.con, "a", size=11), "requeued")
        self.assertIsNone(row(self.con, "a")["not_before"])
        db.set_status(self.con, row(self.con, "a")["id"], "rejected", not_before=9e9)
        self.assertEqual(add(self.con, "a", size=12), "requeued")
        self.assertIsNone(row(self.con, "a")["not_before"])

    def test_retry_errors_clears_not_before_and_fails(self):
        add(self.con, "a")
        db.set_status(self.con, row(self.con, "a")["id"], "error", not_before=9e9,
                      finalize_fails=5, error="x")
        self.assertEqual(db.retry_errors(self.con), 1)
        r = row(self.con, "a")
        self.assertEqual((r["status"], r["not_before"], r["finalize_fails"]), ("queued", None, 0))


class SchemaMigrationTests(unittest.TestCase):
    OLD = """CREATE TABLE items(
  id INTEGER PRIMARY KEY, location TEXT NOT NULL, relpath TEXT NOT NULL,
  size INTEGER NOT NULL, mtime REAL NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', reason TEXT, settings TEXT,
  dest_location TEXT, dest_dir TEXT, final_relpath TEXT, final_size INTEGER,
  was_online_only INTEGER NOT NULL DEFAULT 0, not_before REAL, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, started_at REAL, finished_at REAL, UNIQUE(location, relpath));"""

    def test_old_database_gets_new_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.execute(self.OLD)
            old.execute("INSERT INTO items(location, relpath, size, mtime, kind, source, priority)"
                        " VALUES('d','a.mp4',1,1.0,'video','library',3)")
            old.commit()
            old.close()
            con = db.open_db(path)
            try:
                r = con.execute("SELECT * FROM items").fetchone()
                self.assertEqual((r["evict_pending"], r["finalize_fails"]), (0, 0))
                db.set_status(con, r["id"], "done", evict_pending=1, finalize_fails=2)
            finally:
                con.close()
            con = db.open_db(path)  # opening again must not fail
            con.close()


class PlannedNamesTests(DbTestCase):
    def test_planned_names_casefolded(self):
        add(self.con, "a")
        db.set_status(self.con, row(self.con, "a")["id"], "queued",
                      final_relpath="out/IMG_Compressed.WEBP", final_size=1)
        self.assertEqual(db.planned_names(self.con, "d", "out"), {"img_compressed.webp"})


    def test_planned_names(self):
        for rel, status in (("a", "queued"), ("b", "running"), ("c", "published"),
                            ("d", "done"), ("e", "error")):
            add(self.con, rel)
            r = row(self.con, rel)
            db.set_status(self.con, r["id"], status, final_relpath=f"out/{rel}.webp", final_size=1)
        add(self.con, "f")
        add(self.con, "g", location="win_dl", dest_location="mega", dest_dir="TBC")
        db.set_status(self.con, row(self.con, "g", "win_dl")["id"], "queued",
                      final_relpath="TBC/g.webp", final_size=1)
        db.set_status(self.con, row(self.con, "f")["id"], "queued",
                      final_relpath="elsewhere/f.webp", final_size=1)
        self.assertEqual(db.planned_names(self.con, "d", "out"), {"a.webp", "b.webp", "c.webp"})
        self.assertEqual(db.planned_names(self.con, "d", "out",
                                          exclude_id=row(self.con, "a")["id"]),
                         {"b.webp", "c.webp"})
        self.assertEqual(db.planned_names(self.con, "mega", "TBC"), {"g.webp"})
        self.assertEqual(db.planned_names(self.con, "d", ""), set())


class RenameTests(DbTestCase):
    def test_rename_replaces_stale_row_at_target(self):
        add(self.con, "old/a.mp4")
        add(self.con, "new/a.mp4", size=99)
        add(self.con, "new/a.mp4", location="mega", size=7)
        cur = row(self.con, "old/a.mp4")
        db.set_status(self.con, cur["id"], "done", reason="r", error=None)
        db.rename_item(self.con, cur["id"], "new/a.mp4")
        r = row(self.con, "new/a.mp4")
        self.assertEqual((r["id"], r["status"], r["reason"], r["size"]), (cur["id"], "done", "r", 10))
        self.assertIsNone(row(self.con, "old/a.mp4"))
        self.assertEqual(row(self.con, "new/a.mp4", location="mega")["size"], 7)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_rename_without_conflict(self):
        add(self.con, "a")
        cur = row(self.con, "a")
        db.rename_item(self.con, cur["id"], "b")
        self.assertEqual(row(self.con, "b")["id"], cur["id"])


class AdoptAndPruneTests(DbTestCase):
    def _rejected(self):
        add(self.con, "a/x.mp4", size=5, mtime=7.0)
        db.set_status(self.con, row(self.con, "a/x.mp4")["id"], "rejected")

    def test_adopt_moved(self):
        self._rejected()
        ok = db.adopt_moved(self.con, location="d", relpath="b/x.mp4", size=5, mtime=7.0,
                            exists=lambda p: False)
        self.assertTrue(ok)
        self.assertIsNone(row(self.con, "a/x.mp4"))
        self.assertEqual(row(self.con, "b/x.mp4")["status"], "rejected")

    def test_adopt_moved_refusals(self):
        self._rejected()
        self.assertFalse(db.adopt_moved(self.con, location="d", relpath="b/x.mp4", size=5,
                                        mtime=7.0, exists=lambda p: True))
        self.assertFalse(db.adopt_moved(self.con, location="d", relpath="b/x.mp4", size=6,
                                        mtime=7.0, exists=lambda p: False))
        self.assertFalse(db.adopt_moved(self.con, location="d", relpath="b/y.mp4", size=5,
                                        mtime=7.0, exists=lambda p: False))

    def test_prune_missing(self):
        add(self.con, "keep.mp4")
        add(self.con, "gone.mp4")
        add(self.con, "done.mp4")
        add(self.con, "pub.mp4")
        db.set_status(self.con, row(self.con, "gone.mp4")["id"], "skipped")
        db.set_status(self.con, row(self.con, "done.mp4")["id"], "done")
        db.set_status(self.con, row(self.con, "pub.mp4")["id"], "published", not_before=0)
        add(self.con, "rej.mp4")
        db.set_status(self.con, row(self.con, "rej.mp4")["id"], "rejected")
        n = db.prune_missing(self.con, location="d", under="", seen={"keep.mp4"})
        self.assertEqual(n, 2)  # skipped and rejected rows are pruned, done rows never
        self.assertIsNone(row(self.con, "gone.mp4"))
        self.assertIsNone(row(self.con, "rej.mp4"))
        self.assertIsNotNone(row(self.con, "done.mp4"))
        self.assertIsNotNone(row(self.con, "keep.mp4"))
        self.assertIsNotNone(row(self.con, "pub.mp4"))

    def test_prune_under(self):
        add(self.con, "sub/a.mp4")
        add(self.con, "subway/b.mp4")
        add(self.con, "sub/c.mp4")
        n = db.prune_missing(self.con, location="d", under="sub", seen={"sub/c.mp4"})
        self.assertEqual(n, 1)
        self.assertIsNone(row(self.con, "sub/a.mp4"))
        self.assertIsNotNone(row(self.con, "subway/b.mp4"))


class MiscTests(DbTestCase):
    def test_meta(self):
        self.assertIsNone(db.meta_get(self.con, "k"))
        self.assertEqual(db.meta_get(self.con, "k", "d"), "d")
        db.meta_set(self.con, "k", "1")
        db.meta_set(self.con, "k", "2")
        self.assertEqual(db.meta_get(self.con, "k"), "2")

    def test_scans(self):
        self.assertIsNone(db.last_scan(self.con, "s"))
        db.record_scan(self.con, name="s", started=1, finished=2, ok=True, message="a")
        db.record_scan(self.con, name="s", started=3, finished=4, ok=False, message="b")
        db.record_scan(self.con, name="t", started=5, finished=6, ok=True, message="c")
        r = db.last_scan(self.con, "s")
        self.assertEqual((r["message"], r["ok"]), ("b", 0))

    def test_log_snapshot_folders(self):
        db.log_conversion(self.con, ts=1, location="d", relpath="a", kind="video",
                          bytes_before=10, bytes_after=5)
        db.add_snapshot(self.con, ts=1, location="d", total_bytes=100, free_bytes=50)
        db.replace_folders(self.con, "d", [("f1", 1, 2, 1)])
        db.replace_folders(self.con, "d", [("f2", 3, 4, 2)])
        rows = self.con.execute("SELECT folder FROM folders").fetchall()
        self.assertEqual([r["folder"] for r in rows], ["f2"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM conversions").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
