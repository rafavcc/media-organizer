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


if __name__ == "__main__":
    unittest.main()
