import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import cli
from media_organizer.convert import Outcome
from media_organizer.shrink import shrink_file
from tests.helpers import make_image, make_video, test_config


class ShrinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = test_config(self.root)
        self.dir = self.root / "gallery"
        self.dir.mkdir()

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name.startswith("mo-"))


class ShrinkFileTests(ShrinkCase):
    def test_png_is_replaced_by_webp_keeping_mtime(self):
        src = make_image(self.dir / "a.png")
        os.utime(src, (1_600_000_000, 1_600_000_000))
        res = shrink_file(src, self.cfg)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(res.path, self.dir / "a.webp")
        self.assertFalse(src.exists())
        self.assertTrue(res.path.exists())
        self.assertEqual(int(res.path.stat().st_mtime), 1_600_000_000)
        self.assertEqual(self.leftovers(), [])

    def test_result_takes_the_originals_permissions(self):
        src = make_image(self.dir / "a.png")
        os.chmod(src, 0o644)
        res = shrink_file(src, self.cfg)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(res.path.stat().st_mode & 0o777, 0o644)

    def test_existing_webp_name_is_not_overwritten(self):
        (self.dir / "a.webp").write_bytes(b"keep me")
        src = make_image(self.dir / "a.png")
        res = shrink_file(src, self.cfg)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(res.path, self.dir / "a (1).webp")
        self.assertEqual((self.dir / "a.webp").read_bytes(), b"keep me")

    def test_rejected_conversion_keeps_the_original(self):
        src = make_image(self.dir / "a.png")
        before = src.read_bytes()
        with mock.patch("media_organizer.shrink.convert",
                        return_value=Outcome("low-quality", "SSIM 0.9")):
            res = shrink_file(src, self.cfg)
        self.assertEqual(res.status, "low-quality")
        self.assertEqual(res.path, src)
        self.assertEqual(src.read_bytes(), before)

    def test_video_is_skipped_untouched(self):
        src = make_video(self.dir / "v.mp4", seconds=1)
        with mock.patch("media_organizer.shrink.convert") as conv:
            res = shrink_file(src, self.cfg)
        conv.assert_not_called()
        self.assertEqual(res.status, "skipped")
        self.assertTrue(src.exists())

    def test_webp_and_unknown_files_are_skipped(self):
        for name in ("x.webp", "notes.txt"):
            p = self.dir / name
            p.write_bytes(b"data")
            self.assertEqual(shrink_file(p, self.cfg).status, "skipped")
            self.assertTrue(p.exists())

    def test_missing_file_is_an_error(self):
        res = shrink_file(self.dir / "nope.jpg", self.cfg)
        self.assertEqual(res.status, "error")


class ShrinkCliTests(ShrinkCase):
    def run_cli(self, *args):
        conf = self.root / "config.toml"
        conf.write_text(f'state_dir = "{self.cfg.state_dir}"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(conf), "shrink", *args])
        return code, out.getvalue()

    def test_shrinks_several_files_and_reports_each(self):
        a = make_image(self.dir / "a.png")
        b = make_image(self.dir / "b.bmp")
        code, out = self.run_cli(str(a), str(b))
        self.assertEqual(code, 0, out)
        self.assertIn("ok", out)
        self.assertTrue((self.dir / "a.webp").exists())
        self.assertTrue((self.dir / "b.webp").exists())

    def test_skips_exit_zero(self):
        p = self.dir / "x.webp"
        p.write_bytes(b"data")
        code, out = self.run_cli(str(p))
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)

    def test_error_exits_one(self):
        code, out = self.run_cli(str(self.dir / "nope.jpg"))
        self.assertEqual(code, 1)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
