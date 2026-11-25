import unittest

from media_organizer.config import Config
from media_organizer.rules import (
    final_name, free_name, is_candidate, kind_of, route_download,
)

CFG = Config()
MB = 1024 * 1024


class KindAndCandidateTests(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(kind_of("A.MKV"), "video")
        self.assertEqual(kind_of("a.HEIC"), "image")
        self.assertIsNone(kind_of("a.webp"))
        self.assertIsNone(kind_of("a.txt"))

    def test_is_candidate(self):
        self.assertFalse(is_candidate("x_compressed.mp4"))
        self.assertFalse(is_candidate("x_COMPRESSED.mp4"))
        self.assertFalse(is_candidate(".x.mp4"))
        self.assertFalse(is_candidate("x.mp4.part"))
        self.assertTrue(is_candidate("x.mp4"))



class RouteDownloadTests(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(route_download("a.mp4", 400 * MB + 1, CFG), "d")
        self.assertEqual(route_download("a.mp4", 400 * MB, CFG), "mega")
        self.assertEqual(route_download("a.png", 900 * MB, CFG), "mega")
        self.assertEqual(route_download("a.webp", 1, CFG), "mega")
        self.assertIsNone(route_download("a.pdf", 1, CFG))
        self.assertIsNone(route_download("a.mp4.crdownload", 1, CFG))
        self.assertIsNone(route_download(".a.mp4", 1, CFG))

    def test_compressed_names_never_routed(self):
        self.assertIsNone(route_download("foo_compressed.png", 1, CFG))
        self.assertIsNone(route_download("FOO_COMPRESSED.MP4", 1, CFG))


class NamingTests(unittest.TestCase):
    def test_final_name(self):
        self.assertEqual(final_name("clip.mov", ".mp4", lambda n: False), "clip_compressed.mp4")
        taken = {"clip_compressed.mp4"}
        self.assertEqual(final_name("clip.mov", ".mp4", taken.__contains__), "clip_mov_compressed.mp4")
        taken.add("clip_mov_compressed.mp4")
        self.assertEqual(final_name("clip.mov", ".mp4", taken.__contains__), "clip_mov2_compressed.mp4")

    def test_free_name(self):
        taken = {"a.mp4", "a (1).mp4"}
        self.assertEqual(free_name("a.mp4", taken.__contains__), "a (2).mp4")
        self.assertEqual(free_name("b.mp4", taken.__contains__), "b.mp4")


if __name__ == "__main__":
    unittest.main()
