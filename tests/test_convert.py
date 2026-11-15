import tempfile
import unittest
from pathlib import Path

from media_organizer import rules
from media_organizer.convert import convert
from media_organizer.encode import image_cmd, video_cmd
from media_organizer.probe import VideoInfo, check_tools, gif_frames, probe_video, webp_frames, dims

from tests.helpers import make_gif, make_image, make_video, test_config


def fake_info(acodecs):
    return VideoInfo(codec="h264", width=320, height=240, fps="30/1", fps_value=30.0,
                     duration=2.0, audio=1, acodecs=acodecs, bitrate=1_000_000)


def contains_in_order(cmd, seq):
    for i in range(len(cmd) - len(seq) + 1):
        if cmd[i:i + len(seq)] == seq:
            return True
    return False


class CommandTests(unittest.TestCase):
    def test_video_cmd_matches_recipe(self):
        cfg = test_config(Path("/tmp"))
        cmd = video_cmd(Path("in.mp4"), Path("out.mp4"), fake_info(("aac",)), cfg)
        for seq in (["-c:v", "libx265", "-preset", "fast", "-crf", "30", "-pix_fmt", "yuv420p10le"],
                    ["-r", "30/1", "-fps_mode", "cfr", "-tag:v", "hvc1"],
                    ["-c:a", "copy"], ["-movflags", "+faststart"],
                    ["-x265-params", "pools=10"], ["-progress", "pipe:1", "-nostats"]):
            self.assertTrue(contains_in_order(cmd, seq), seq)
        self.assertTrue(cmd[cmd.index("-i") + 1].startswith("/"))
        self.assertTrue(cmd[-1].startswith("/"))
        cmd = video_cmd(Path("in.mp4"), Path("out.mp4"), fake_info(("pcm_s16le",)), cfg)
        self.assertTrue(contains_in_order(cmd, ["-c:a", "aac", "-b:a", "192k"]))

    def test_image_cmd(self):
        out = Path("o.webp")
        jpg = image_cmd(".jpg", Path("a.jpg"), out)
        self.assertEqual(jpg[0], "cwebp")
        self.assertTrue(contains_in_order(jpg, ["-q", "85"]))
        self.assertTrue(contains_in_order(image_cmd(".png", Path("a.png"), out), ["-q", "90"]))
        gif = image_cmd(".gif", Path("a.gif"), out)
        self.assertEqual(gif[0], "gif2webp")
        self.assertIn("-lossy", gif)
        self.assertTrue(contains_in_order(gif, ["-q", "75"]))

    def test_check_tools_lists_names(self):
        self.assertIsInstance(check_tools(), list)


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src_dir = self.tmp / "src dir"
        self.work = self.tmp / "work"
        self.src_dir.mkdir()
        self.work.mkdir()
        self.cfg = test_config(self.tmp)

    def run_convert(self, src, cfg=None, on_progress=None):
        return convert(src, src.name, src.stat().st_size, self.work, cfg or self.cfg, on_progress)

    def test_h264_is_converted(self):
        src = make_video(self.src_dir / "a.mp4", extra=["-b:v", "4M"])
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)
        a, b = probe_video(src), probe_video(res.out)
        self.assertEqual(b.codec, "hevc")
        self.assertEqual((b.width, b.height), (a.width, a.height))
        self.assertLessEqual(res.out.stat().st_size, 0.75 * src.stat().st_size)
        self.assertTrue(res.out.resolve().is_relative_to(self.work.resolve()))

    def test_hevc_low_bitrate_is_efficient(self):
        src = make_video(self.src_dir / "a.mp4", codec="libx265", extra=["-crf", "40"])
        res = self.run_convert(src)
        self.assertEqual(res.status, "efficient")
        self.assertIsNone(res.out)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_hevc_bloated_is_encoded(self):
        src = make_video(self.src_dir / "a.mp4", codec="libx265",
                         extra=["-vf", "noise=alls=40:allf=t", "-b:v", "6M"])
        info = probe_video(src)
        self.assertGreater(rules.bits_per_pixel(info.bitrate, info.width, info.height, info.fps_value), 0.07)
        self.assertIn(self.run_convert(src).status, ("ok", "no-gain"))

    def test_no_gain(self):
        src = make_video(self.src_dir / "a.mp4", extra=["-b:v", "4M"])
        res = self.run_convert(src, test_config(self.tmp, min_gain_ratio=0.01))
        self.assertEqual(res.status, "no-gain")
        self.assertIsNone(res.out)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_unreadable_video(self):
        src = self.src_dir / "x.mp4"
        src.write_text("not a video")
        res = self.run_convert(src)
        self.assertEqual(res.status, "error")
        self.assertIsNone(res.out)

    def test_png_to_webp(self):
        src = make_image(self.src_dir / "a.png")
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(dims(res.out), dims(src))
        self.assertEqual([p for p in self.work.iterdir()], [res.out])

    def test_bmp_via_imagemagick(self):
        src = make_image(self.src_dir / "a.bmp")
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(dims(res.out), dims(src))
        self.assertEqual([p for p in self.work.iterdir()], [res.out])

    def test_gif_keeps_frames(self):
        src = make_gif(self.src_dir / "a.gif")
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(webp_frames(res.out), gif_frames(src))

    def test_mkv_avi_inputs(self):
        for name, kw in (("a.mkv", {}), ("a.avi", {"codec": "mpeg4"})):
            with self.subTest(name=name):
                src = make_video(self.src_dir / name, extra=["-b:v", "4M"], **kw)
                res = self.run_convert(src)
                self.assertEqual(res.status, "ok", res.message)
                self.assertEqual(res.out.suffix, ".mp4")
                self.assertEqual(probe_video(res.out).codec, "hevc")

    def test_awkward_names(self):
        src = make_video(self.src_dir / "-we'ird [x] \"q\" ñ.mp4", extra=["-b:v", "4M"])
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)

    def test_progress_callback(self):
        src = make_video(self.src_dir / "a.mp4", extra=["-b:v", "4M"])
        seen = []
        res = self.run_convert(src, on_progress=seen.append)
        self.assertEqual(res.status, "ok", res.message)
        self.assertTrue(any(0 < v <= 1 for v in seen), seen)


if __name__ == "__main__":
    unittest.main()
