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

    def test_h264_mp4_is_replaced_in_place_by_hevc_mp4(self):
        src = make_video(self.dir / "v.mp4", seconds=2)
        os.utime(src, (1_600_000_000, 1_600_000_000))
        with mock.patch("media_organizer.shrink.convert") as conv:
            tmp = self.dir / "mo-fake.mp4"
            tmp.write_bytes(b"small")
            conv.return_value = Outcome("ok", "x -> 5", tmp)
            res = shrink_file(src, self.cfg)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(res.path, src)
        self.assertEqual(src.read_bytes(), b"small")
        self.assertEqual(int(src.stat().st_mtime), 1_600_000_000)
        self.assertEqual(self.leftovers(), [])

    def test_non_mp4_video_becomes_mp4_and_original_is_removed(self):
        src = self.dir / "v.webm"
        src.write_bytes(b"orig")
        (self.dir / "v.mp4").write_bytes(b"keep me")
        tmp = self.dir / "mo-fake.mp4"
        tmp.write_bytes(b"small")
        with mock.patch("media_organizer.shrink.convert",
                        return_value=Outcome("ok", "x", tmp)):
            res = shrink_file(src, self.cfg)
        self.assertEqual(res.path, self.dir / "v (1).mp4")
        self.assertFalse(src.exists())
        self.assertEqual((self.dir / "v.mp4").read_bytes(), b"keep me")

    def test_efficient_video_is_left_untouched(self):
        src = make_video(self.dir / "v.mp4", seconds=1, codec="libx265",
                         extra=["-tag:v", "hvc1", "-b:v", "10k"])
        before = src.read_bytes()
        res = shrink_file(src, self.cfg)
        self.assertNotEqual(res.status, "ok")
        self.assertEqual(src.read_bytes(), before)

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
