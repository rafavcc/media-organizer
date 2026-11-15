import json
import tempfile
import unittest
from pathlib import Path

from media_organizer import db
from media_organizer.legacy import import_legacy
from tests.helpers import test_config


def flag(status, size=100, mtime=5.0, settings="r75s95"):
    return {"status": status, "settings": settings, "size": size, "mtime": mtime}


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = test_config(self.root)
        self.con = db.open_db(self.root / "state" / "o.db")
        self.addCleanup(self.con.close)
        d = str(self.cfg.d_root)
        flags = {
            f"{d}/a/no_gain.mp4": flag("no-gain"),
            f"{d}/a/low.jpg": flag("low-quality"),
            f"{d}/a/err.png": flag("error"),
            f"{d}/a/eff.mp4": flag("efficient"),
            "/home/u/icloud-mac/MEGA/big/a.jpg": flag("no-gain", size=7, mtime=9.0),
            "/somewhere/else/b.jpg": flag("no-gain"),
        }
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"flags": flags, "pending": {"x": 1}}))

    def row(self, location, relpath):
        return self.con.execute("SELECT * FROM items WHERE location=? AND relpath=?",
                                (location, relpath)).fetchone()

    def test_imports_mapped_flags(self):
        res = import_legacy(self.con, self.cfg, [self.state])
        self.assertEqual(res, {"imported": 4, "skipped": 2})
        r = self.row("d", "a/no_gain.mp4")
        self.assertEqual((r["status"], r["reason"], r["source"], r["priority"], r["size"],
                          r["mtime"], r["settings"], r["kind"]),
                         ("rejected", "no-gain", "library", 3, 100, 5.0, self.cfg.settings, "video"))
        self.assertEqual(self.row("d", "a/low.jpg")["reason"], "low-quality")
        self.assertEqual(self.row("d", "a/err.png")["status"], "error")
        m = self.row("mega", "big/a.jpg")
        self.assertEqual((m["status"], m["priority"], m["size"]), ("rejected", 2, 7))
        self.assertIsNone(self.row("d", "a/eff.mp4"))

    def test_second_run_imports_nothing(self):
        import_legacy(self.con, self.cfg, [self.state])
        self.assertEqual(import_legacy(self.con, self.cfg, [self.state])["imported"], 0)

    def test_existing_row_untouched(self):
        db.upsert_candidate(self.con, location="d", relpath="a/no_gain.mp4", size=1, mtime=1.0,
                            kind="video", source="library", priority=3, settings="x")
        import_legacy(self.con, self.cfg, [self.state])
        self.assertEqual(self.row("d", "a/no_gain.mp4")["status"], "queued")

    def test_missing_file_is_not_an_error(self):
        res = import_legacy(self.con, self.cfg, [self.root / "nope.json"])
        self.assertEqual(res, {"imported": 0, "skipped": 0})

    def test_other_settings_or_config_skipped(self):
        self.state.write_text(json.dumps({"flags": {
            f"{self.cfg.d_root}/a.mp4": flag("no-gain", settings="r50s90")}}))
        self.assertEqual(import_legacy(self.con, self.cfg, [self.state])["imported"], 0)
        self.state.write_text(json.dumps({"flags": {f"{self.cfg.d_root}/a.mp4": flag("no-gain")}}))
        from dataclasses import replace
        cfg = replace(self.cfg, min_ssim=0.9)
        self.assertEqual(import_legacy(self.con, cfg, [self.state])["imported"], 0)

    def test_name_without_kind_skipped(self):
        self.state.write_text(json.dumps({"flags": {f"{self.cfg.d_root}/a.txt": flag("no-gain")}}))
        self.assertEqual(import_legacy(self.con, self.cfg, [self.state]),
                         {"imported": 0, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
