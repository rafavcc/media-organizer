import tempfile
import unittest
from pathlib import Path

from media_organizer import db, sorter
from media_organizer.locations import LocalLocation
from tests.helpers import test_config


class MatchingTests(unittest.TestCase):
    def matchers(self, *names):
        return sorter.build_matchers([sorter.PMV, *sorted(names)])

    def test_case_and_punctuation_are_ignored(self):
        m = self.matchers("Jane Doe")
        for name in ("jane.doe.scene.mp4", "JANE_DOE-x.mp4", "x+Jane+Doe+(1).mp4", "Jane Doe.mp4"):
            self.assertEqual(sorter.match_person(m, name), "Jane Doe", name)

    def test_whole_words_only(self):
        m = self.matchers("Ai")
        self.assertIsNone(sorter.match_person(m, "maid.kincaid.mp4"))
        self.assertEqual(sorter.match_person(m, "ai.scene.mp4"), "Ai")

    def test_pmv_first_then_alphabetical(self):
        m = self.matchers("Zed Zulu", "Amy Adams")
        self.assertEqual(sorter.match_person(m, "Zed Zulu Amy Adams.mp4"), "Amy Adams")
        self.assertEqual(sorter.match_person(m, "Amy Adams pmv compilation.mp4"), "Pmv Compilation")

    def test_extension_is_not_part_of_the_name(self):
        self.assertIsNone(sorter.match_person(self.matchers("Mp4"), "clip.mp4"))


class NamesFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "performers.txt"

    def test_tidied_like_the_old_sorter(self):
        self.path.write_text("bob jones\n\n  Amy adams \nBOB JONES\n")
        self.assertEqual(sorter.load_names(self.path),
                         ["Pmv Compilation", "Amy Adams", "Bob Jones"])
        self.assertEqual(self.path.read_text(), "Pmv Compilation\nAmy Adams\nBob Jones\n")

    def test_no_write_leaves_the_file_alone(self):
        self.path.write_text("bob jones\n")
        sorter.load_names(self.path, write=False)
        self.assertEqual(self.path.read_text(), "bob jones\n")


class SortFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = test_config(Path(tmp.name))
        self.cfg.d_root.mkdir()
        self.d = LocalLocation("d", self.cfg.d_root)
        self.con = db.open_db(self.cfg.db_path)
        self.addCleanup(self.con.close)
        self.cfg.performers_file.write_text("Jane Doe\n")
        (self.cfg.d_root / "_CONVERTED" / "sub").mkdir(parents=True)

    def put(self, rel, data=b"x"):
        (self.cfg.d_root / rel).write_bytes(data)

    def test_only_videos_are_sorted(self):
        for name in ("jane doe.webp", "jane doe.txt", "._jane doe.mp4", "jane doe.mkv"):
            self.put(f"_CONVERTED/{name}")
        moved = sorter.sort_converted(self.con, self.d, self.cfg)
        self.assertEqual(moved, [("_CONVERTED/jane doe.mkv", "Jane Doe/jane doe.mkv")])

    def test_sub_folders_are_sorted_too(self):
        self.put("_CONVERTED/sub/jane doe 2.mp4")
        sorter.sort_converted(self.con, self.d, self.cfg)
        self.assertTrue((self.cfg.d_root / "Jane Doe" / "jane doe 2.mp4").exists())

    def test_dry_run_moves_nothing_and_does_not_tidy_the_names(self):
        self.put("_CONVERTED/jane doe.mp4")
        moved = sorter.sort_converted(self.con, self.d, self.cfg, dry_run=True)
        self.assertEqual(moved, [("_CONVERTED/jane doe.mp4", "Jane Doe/jane doe.mp4")])
        self.assertTrue((self.cfg.d_root / "_CONVERTED" / "jane doe.mp4").exists())
        self.assertEqual(self.cfg.performers_file.read_text(), "Jane Doe\n")

    def test_destination_busy_with_the_worker_waits(self):
        (self.cfg.d_root / "Jane Doe").mkdir()
        self.put("Jane Doe/jane doe.mp4", b"old")
        db.upsert_candidate(self.con, location="d", relpath="Jane Doe/jane doe.mp4", size=3,
                            mtime=0, kind="video", source="library", priority=3,
                            settings="s")  # queued for compression
        self.put("_CONVERTED/jane doe.mp4", b"new")
        self.assertEqual(sorter.sort_converted(self.con, self.d, self.cfg), [])
        self.assertEqual((self.cfg.d_root / "Jane Doe" / "jane doe.mp4").read_bytes(), b"old")

    def test_sort_one_never_raises(self):
        self.put("_CONVERTED/jane doe.mp4")
        self.d.move_within = lambda *a: (_ for _ in ()).throw(OSError("disk gone"))
        with self.assertLogs("media_organizer.sorter", "ERROR"):
            self.assertIsNone(sorter.sort_one(self.con, self.d, self.cfg, "_CONVERTED/jane doe.mp4"))


if __name__ == "__main__":
    unittest.main()
