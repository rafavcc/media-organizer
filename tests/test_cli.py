import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import cli, db
from tests.helpers import test_config

MIB = 1024 * 1024
GIB = 1024 ** 3


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = test_config(self.root)
        self.cfg.d_root.mkdir()
        self.cfg.win_downloads.mkdir()
        self.conf = self.root / "config.toml"
        self.conf.write_text(
            f'd_root = "{self.cfg.d_root}"\nwin_downloads = "{self.cfg.win_downloads}"\n'
            f'state_dir = "{self.cfg.state_dir}"\nsorter = "{self.cfg.sorter}"\n'
            'mac_host = "nonexistent-test-host"\n')

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(self.conf), *args])
        return code, out.getvalue()

    def db(self):
        con = db.open_db(self.cfg.db_path)
        self.addCleanup(con.close)
        return con


class StatusTests(CliCase):
    def test_empty_status(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("First pass", out)
        self.assertIn("Queue", out)
        self.assertNotIn("paused", out)

    def test_json(self):
        code, out = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertIn("freed", json.loads(out))

    def test_pause_resume(self):
        self.assertEqual(self.run_cli("pause")[0], 0)
        self.assertIn("paused", self.run_cli("status")[1].splitlines()[:3])
        self.assertEqual(self.run_cli("resume")[0], 0)
        self.assertNotIn("paused", self.run_cli("status")[1])

    def test_errors_listing_and_retry(self):
        con = self.db()
        db.upsert_candidate(con, location="d", relpath="a/x.mp4", size=5, mtime=1.0,
                            kind="video", source="library", priority=3, settings="s")
        item = con.execute("SELECT id FROM items").fetchone()["id"]
        db.set_status(con, item, "error", error="boom")
        _, out = self.run_cli("status", "--errors")
        self.assertIn("a/x.mp4", out)
        self.assertIn("boom", out)
        self.assertIn("1 errors", self.run_cli("status")[1])
        code, out = self.run_cli("retry-errors")
        self.assertEqual((code, out.strip()), (0, "1"))

    def test_layout_with_data(self):
        con = self.db()
        now = time.time()
        db.log_conversion(con, ts=now, location="d", relpath="a.mp4", kind="video",
                          bytes_before=3 * GIB, bytes_after=GIB)
        db.meta_set(con, "current", json.dumps({"name": "x_1080p.mp4", "size": 2 * GIB,
                                                 "progress": 0.37}))
        db.record_scan(con, name="d-full", started=now, finished=now, ok=True, message="")
        db.record_scan(con, name="mac-downloads", started=now, finished=now, ok=False,
                       message="unreachable")
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("freed   2.0 GB", out)
        self.assertIn("37%", out)
        self.assertIn("Last scans", out)
        self.assertIn("unreachable", out)
        self.assertIn("Problems", out)


class StatusLineTests(CliCase):
    def test_queue_and_problem_lines_show_the_new_counts(self):
        con = self.db()
        now = time.time()
        for rel in ("a.mp4", "b.mp4", "c.mp4", "d.tif", "e.mp4"):
            db.upsert_candidate(con, location="d", relpath=rel, size=5, mtime=1.0,
                                kind="video", source="library", priority=3, settings="s")
        ids = {r["relpath"]: r["id"] for r in con.execute("SELECT id, relpath FROM items")}
        db.set_status(con, ids["a.mp4"], "queued", reason="no-space", not_before=now + 3000)
        db.set_status(con, ids["b.mp4"], "queued", reason="no-space", not_before=now + 3000)
        db.set_status(con, ids["c.mp4"], "done", evict_pending=1, finished_at=now)
        db.set_status(con, ids["d.tif"], "rejected", reason="changed")
        db.set_status(con, ids["e.mp4"], "published", not_before=now - 7200)
        out = self.run_cli("status")[1]
        queue = [l for l in out.splitlines() if l.startswith("Queue")][0]
        self.assertIn("· 2 waiting for space", queue)
        self.assertIn("· 1 to release on the Mac", queue)
        problems = [l for l in out.splitlines() if l.startswith("Problems")][0]
        self.assertIn("1 changed", problems)
        self.assertIn("1 stuck", problems)
        self.assertIn("items", out.split("MEGA")[0])
        self.assertNotIn("videos", out)

    def test_zero_counts_are_not_shown(self):
        out = self.run_cli("status")[1]
        self.assertNotIn("waiting for space", out)
        self.assertNotIn("to release on the Mac", out)
        self.assertNotIn("stuck", out)


class RunTests(CliCase):
    def test_missing_tools(self):
        with mock.patch("media_organizer.cli.check_tools", return_value=["ffmpeg"]), \
                mock.patch("media_organizer.cli.Service") as svc:
            code, out = self.run_cli("run", "--no-web")
        self.assertEqual(code, 1)
        self.assertIn("ffmpeg", out)
        svc.assert_not_called()

    def test_run_starts_service(self):
        with mock.patch("media_organizer.cli.check_tools", return_value=[]), \
                mock.patch("media_organizer.cli.Service") as svc:
            code, _ = self.run_cli("run", "--no-web")
        self.assertEqual(code, 0)
        svc.return_value.run_forever.assert_called_once()

    def test_run_starts_and_stops_web(self):
        with mock.patch("media_organizer.cli.check_tools", return_value=[]), \
                mock.patch("media_organizer.cli.Service") as svc, \
                mock.patch("media_organizer.cli.serve_in_thread") as serve:
            code, _ = self.run_cli("run")
        self.assertEqual(code, 0)
        serve.assert_called_once()
        svc.return_value.run_forever.assert_called_once()
        serve.return_value.shutdown.assert_called_once()
        serve.return_value.server_close.assert_called_once()

    def test_run_without_web_server_object(self):
        with mock.patch("media_organizer.cli.check_tools", return_value=[]), \
                mock.patch("media_organizer.cli.Service"), \
                mock.patch("media_organizer.cli.serve_in_thread", return_value=None):
            code, _ = self.run_cli("run")
        self.assertEqual(code, 0)

    def test_no_web_skips_server(self):
        with mock.patch("media_organizer.cli.check_tools", return_value=[]), \
                mock.patch("media_organizer.cli.Service"), \
                mock.patch("media_organizer.cli.serve_in_thread") as serve:
            self.run_cli("run", "--no-web")
        serve.assert_not_called()


class ImportLegacyTests(CliCase):
    def test_prints_counts(self):
        with mock.patch("media_organizer.cli.import_legacy",
                        return_value={"imported": 2, "skipped": 1}):
            code, out = self.run_cli("import-legacy")
        self.assertEqual(code, 0)
        self.assertIn("imported 2", out)
        self.assertIn("skipped 1", out)


class ScanTests(CliCase):
    def test_scan_without_dry_run_rejected(self):
        code, out = self.run_cli("scan")
        self.assertEqual(code, 2)
        self.assertIn("service", out)

    def test_dry_run_missing_local_root_says_skip(self):
        shutil.rmtree(self.cfg.d_root, ignore_errors=True)
        code, out = self.run_cli("scan", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("SKIP d (missing)", out)
        self.assertNotIn("d: 0 would queue", out)

    def test_dry_run(self):
        old = time.time() - 3600
        vid = self.cfg.d_root / "A" / "x.mp4"
        vid.parent.mkdir()
        vid.write_bytes(b"x" * (3 * MIB))
        os.utime(vid, (old, old))
        big = self.cfg.win_downloads / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(400 * MIB + 1)
        os.utime(big, (old, old))
        code, out = self.run_cli("scan", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("WOULD QUEUE d A/x.mp4 3.0 MB", out)
        self.assertIn("WOULD MOVE win_dl big.mp4 -> d/_TO_CONVERT/big.mp4", out)
        self.assertIn("d: 1 would queue, 0 would move", out)
        self.assertIn("win_dl: 0 would queue, 1 would move", out)
        self.assertIn("SKIP mega (unreachable)", out)
        self.assertIn("SKIP mac_dl (unreachable)", out)
        self.assertTrue(big.exists())
        self.assertFalse((self.cfg.d_root / "_TO_CONVERT").exists())
        self.assertFalse(self.cfg.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
