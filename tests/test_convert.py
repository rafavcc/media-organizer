import os
import subprocess
import tempfile
import time
from unittest import mock
import unittest
from pathlib import Path

from media_organizer import probe, rules
from media_organizer.convert import convert, video_mismatch
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

    def test_encoder_defaults_are_crf27_slower(self):
        from media_organizer.config import Config
        self.assertEqual((Config().crf, Config().preset), (27, "slower"))
        cmd = video_cmd(Path("in.mp4"), Path("out.mp4"), fake_info(("aac",)), Config())
        self.assertTrue(contains_in_order(cmd, ["-preset", "slower", "-crf", "27"]))

    def test_preset_change_counts_as_a_settings_change(self):
        self.assertNotEqual(test_config(Path("/tmp"), preset="slow").settings,
                            test_config(Path("/tmp"), preset="slower").settings)

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


class OutputCheckTests(unittest.TestCase):
    def out(self, **changes):
        from dataclasses import replace
        return replace(fake_info(("aac",)), **{"codec": "hevc", **changes})

    def test_matching_output_passes(self):
        self.assertIsNone(video_mismatch(fake_info(("aac",)), self.out()))
        self.assertIsNone(video_mismatch(fake_info(("aac",)), self.out(fps_value=30.0001)))

    def test_each_mismatch_is_named(self):
        src = fake_info(("aac",))
        for changes, word in (({"codec": "h264"}, "codec h264"),
                              ({"width": 160}, "resolution"),
                              ({"fps_value": 25.0}, "frame rate"),
                              ({"fps_value": 29.97}, "frame rate"),
                              ({"audio": 0}, "audio"),
                              ({"duration": 4.0}, "duration")):
            self.assertIn(word, video_mismatch(src, self.out(**changes)), changes)

    def test_wrong_codec_fails_the_conversion_and_keeps_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name) / "work"
        work.mkdir()
        src = make_video(Path(tmp.name) / "a.mp4", extra=["-b:v", "4M"])
        from dataclasses import replace

        def h264_output(p):  # the source probes truly; the encoded file claims to be H.264
            info = probe_video(p)
            return info if info is None or p == src else replace(info, codec="h264")

        with mock.patch("media_organizer.probe.probe_video", side_effect=h264_output):
            res = convert(src, src.name, src.stat().st_size, work, test_config(Path(tmp.name)))
        self.assertEqual(res.status, "error")
        self.assertIn("codec h264", res.message)
        self.assertEqual(list(work.iterdir()), [])


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

    def _tiff(self, name, pages):
        pngs = [make_image(self.src_dir / f"p{i}.png") for i in range(pages)]
        out = self.src_dir / name
        subprocess.run([probe.imagemagick(), *map(str, pngs), str(out)], check=True,
                       capture_output=True)
        return out

    def test_multi_page_tiff_is_multi_frame(self):
        src = self._tiff("a.tif", 2)
        before = src.read_bytes()
        res = self.run_convert(src)
        self.assertEqual(res.status, "multi-frame", res.message)
        self.assertIsNone(res.out)
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_single_page_tiff_converts(self):
        src = self._tiff("a.tiff", 1)
        res = self.run_convert(src)
        self.assertEqual(res.status, "ok", res.message)

    def test_apng_is_multi_frame(self):
        src = self.src_dir / "a.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=64x64:rate=5:duration=1", "-f", "apng", str(src)],
                       check=True, capture_output=True)
        res = self.run_convert(src)
        self.assertEqual(res.status, "multi-frame", res.message)
        self.assertTrue(src.exists())

    def test_plain_png_converts(self):
        self.assertEqual(self.run_convert(make_image(self.src_dir / "p.png")).status, "ok")

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


class StallAndTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "work"
        self.work.mkdir()

    def fake_ffmpeg(self, script):
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        f = bindir / "ffmpeg"
        f.write_text(script)
        f.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_config_default_and_example(self):
        from media_organizer.config import Config
        self.assertEqual(Config().encode_stall_min, 30)
        example = Path(__file__).resolve().parents[1] / "deploy" / "config.example.toml"
        self.assertIn("encode_stall_min", example.read_text())

    def test_stalled_encoder_is_killed_and_reported(self):
        src = make_video(self.tmp / "a.mp4", extra=["-b:v", "4M"])
        self.fake_ffmpeg("#!/bin/sh\necho out_time_us=1000\nexec sleep 60\n")
        cfg = test_config(self.tmp, encode_stall_min=0.03)  # about two seconds
        started = time.monotonic()
        res = convert(src, "a.mp4", src.stat().st_size, self.work, cfg)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(res.status, "error")
        self.assertIn("encoder stalled", res.message)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_progressing_encoder_is_not_killed(self):
        src = make_video(self.tmp / "a.mp4", extra=["-b:v", "4M"])
        cfg = test_config(self.tmp, encode_stall_min=0.05)
        res = convert(src, "a.mp4", src.stat().st_size, self.work, cfg)
        self.assertEqual(res.status, "ok", res.message)

    def test_cwebp_timeout_is_an_error_outcome(self):
        src = make_image(self.tmp / "a.png")
        real = subprocess.run

        def run(cmd, *a, **kw):
            if cmd[0] == "cwebp":
                self.assertIn("timeout", kw)
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return real(cmd, *a, **kw)
        with mock.patch("subprocess.run", run):
            res = convert(src, "a.png", src.stat().st_size, self.work, test_config(self.tmp))
        self.assertEqual(res.status, "error")
        self.assertIn("cwebp", res.message)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_ffprobe_and_ssim_timeouts_are_error_outcomes(self):
        src = make_image(self.tmp / "a.png")
        real = subprocess.run

        def run_for(tool):
            def run(cmd, *a, **kw):
                if cmd[0] == tool:
                    raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
                return real(cmd, *a, **kw)
            return run
        for tool in ("ffprobe", "ffmpeg"):
            with self.subTest(tool=tool), mock.patch("subprocess.run", run_for(tool)):
                res = convert(src, "a.png", src.stat().st_size, self.work, test_config(self.tmp))
                self.assertEqual(res.status, "error")
                self.assertIn(tool, res.message)
        vid = make_video(self.tmp / "v.mp4")
        with mock.patch("subprocess.run", run_for("ffprobe")):
            res = convert(vid, "v.mp4", vid.stat().st_size, self.work, test_config(self.tmp))
        self.assertEqual((res.status, "ffprobe" in res.message), ("error", True))

    def test_timeouts_are_set(self):
        seen = {}
        real = subprocess.run

        def run(cmd, *a, **kw):
            seen[cmd[0]] = kw.get("timeout")
            return real(cmd, *a, **kw)
        src = make_image(self.tmp / "a.png")
        with mock.patch("subprocess.run", run):
            convert(src, "a.png", src.stat().st_size, self.work, test_config(self.tmp))
        self.assertEqual(seen["cwebp"], 1800)
        self.assertEqual(seen["ffprobe"], 120)
        self.assertEqual(seen["ffmpeg"], 1800)  # the SSIM run

    def test_apng_walk_survives_truncated_and_oversized_chunks(self):
        from media_organizer.convert import _png_is_animated
        sig = b"\x89PNG\r\n\x1a\n"
        p = self.tmp / "t.png"
        for data in (sig, sig + b"\0\0", sig + (13).to_bytes(4, "big") + b"IHDR" + b"\0" * 5,
                     sig + (2 ** 31).to_bytes(4, "big") + b"tEXt" + b"abc", b""):
            p.write_bytes(data)
            self.assertFalse(_png_is_animated(p))

    def test_stall_below_one_minute_is_rejected_at_load(self):
        from media_organizer.config import load_config
        c = self.tmp / "c.toml"
        c.write_text("encode_stall_min = 0.5\n")
        with self.assertRaises(ValueError) as cm:
            load_config(c)
        self.assertIn("encode_stall_min", str(cm.exception))
        c.write_text("encode_stall_min = 1\n")
        self.assertEqual(load_config(c).encode_stall_min, 1)

    def test_only_out_time_us_counts_as_progress(self):
        src = make_video(self.tmp / "a.mp4", extra=["-b:v", "4M"])
        self.fake_ffmpeg("#!/bin/sh\nwhile true; do echo out_time_ms=$(date +%s%N); "
                         "echo out_time=00:00:01; sleep 0.2; done\n")
        cfg = test_config(self.tmp, encode_stall_min=0.03)
        res = convert(src, "a.mp4", src.stat().st_size, self.work, cfg)
        self.assertIn("encoder stalled", res.message)

    def test_apng_actl_after_64k_of_ancillary_chunks_is_detected(self):
        def chunk(kind, data):
            return len(data).to_bytes(4, "big") + kind + data + b"\0\0\0\0"
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13)
               + chunk(b"tEXt", b"k\0" + b"x" * 70000) + chunk(b"acTL", b"\0" * 8)
               + chunk(b"IDAT", b"\0" * 4))
        p = self.tmp / "a.png"
        p.write_bytes(png)
        from media_organizer.convert import _png_is_animated
        self.assertTrue(_png_is_animated(p))
        p.write_bytes(png.replace(b"acTL", b"aaaa"))
        self.assertFalse(_png_is_animated(p))
