import tempfile
import unittest
from pathlib import Path

from media_organizer.config import Config, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(Config().settings, "r75s95c30b70")
        self.assertEqual(Config().size_threshold_mb, 400)
        self.assertEqual(Config().db_path.name, "organizer.db")

    def test_load_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('size_threshold_mb = 300\nd_root = "/tmp/x"\n')
            cfg = load_config(p)
        self.assertEqual(cfg.size_threshold_mb, 300)
        self.assertEqual(cfg.d_root, Path("/tmp/x"))

    def test_tilde_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('state_dir = "~/somewhere"\n')
            cfg = load_config(p)
        self.assertEqual(cfg.state_dir, Path.home() / "somewhere")

    def test_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text("foo = 1\n")
            with self.assertRaises(ValueError) as cm:
                load_config(p)
        self.assertIn("foo", str(cm.exception))

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_config(Path("/nonexistent/x.toml")), Config())


def load_text(text):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.toml"
        p.write_text(text)
        return load_config(p)


class DownloadsPathTests(unittest.TestCase):
    def rejects(self, text, key):
        with self.assertRaises(ValueError) as cm:
            load_text(text)
        self.assertIn(key, str(cm.exception))

    def test_defaults_accepted(self):
        load_text('web_password = "x"\n')
        load_text('win_downloads = "/mnt/c/Users/rafavcc/Downloads"\nmac_downloads = "Downloads"\n')

    def test_broad_windows_paths_rejected(self):
        for bad in ("/", "/mnt/c", "/mnt/c/", "/mnt/d", "/mnt/c/Users", "/mnt/c/Users/",
                    "/mnt/c/Users/rafavcc", "/mnt/c/Users/rafavcc/", str(Path.home()),
                    "/mnt/c/Users/rafavcc/Downloads/.."):
            with self.subTest(bad=bad):
                self.rejects(f'win_downloads = "{bad}"\n', "win_downloads")

    def test_broad_mac_paths_rejected(self):
        for bad in ("", ".", "/", "Library", "Library/Mobile Documents", "Desktop", "Documents",
                    "./", "Downloads/.."):
            with self.subTest(bad=bad):
                self.rejects(f'mac_downloads = "{bad}"\n', "mac_downloads")

    def test_overlaps_rejected(self):
        self.rejects('win_downloads = "/mnt/d/7_XYZ/dl"\n', "win_downloads")  # inside d_root
        self.rejects('d_root = "/mnt/c/Users/rafavcc/Downloads/lib"\n', "win_downloads")
        self.rejects('win_downloads = "/mnt/d"\nd_root = "/mnt/d/lib"\n', "win_downloads")
        self.rejects('state_dir = "/mnt/c/Users/rafavcc/Downloads/state"\n', "win_downloads")
        self.rejects('win_downloads = "/tmp/x"\nstate_dir = "/tmp/x"\n', "win_downloads")
        self.rejects('mac_downloads = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/dl"\n',
                     "mac_downloads")
        self.rejects('mac_mega = "Downloads/mega"\n', "mac_downloads")
        self.rejects('mac_mega = "Downloads"\n', "mac_downloads")

    def test_readme_trial_configs_load(self):
        import re
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        blocks = [b for b in re.findall(r"```toml\n(.*?)```", readme, re.S) if "d_root" in b]
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            with self.subTest(block=block[:60]):
                load_text(block.replace("<that folder>", "Person"))


if __name__ == "__main__":
    unittest.main()
