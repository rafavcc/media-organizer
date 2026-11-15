import errno
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_organizer import locations
from media_organizer.locations import (LocalLocation, MacLocation, MacSession, MacUnreachable,
                                       NoSpace, build_locations)
from tests.helpers import test_config


class FakeRunner:
    def __init__(self, rc=0, stdout=""):
        self.calls = []
        self.rc = rc
        self.stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def mac_location(tmp, runner=None):
    runner = runner or FakeRunner()
    session = MacSession("mac", Path(tmp) / "mnt", runner)
    loc = MacLocation("mega", session, "home/MEGA")
    loc.root.mkdir(parents=True)
    loc.retry_delay = 0
    return loc, runner


class LocalTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.loc = LocalLocation("d", self.tmp / "root")
        self.loc.retry_delay = 0

    def test_walk(self):
        r = self.loc.root
        for rel in ["a.mp4", "sub/b.mp4", "sub/deep/c.mp4", ".hidden/x.mp4", ".x.mp4"]:
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_bytes(b"123")
        self.assertEqual(sorted(e.relpath for e in self.loc.walk()),
                         ["a.mp4", "sub/b.mp4", "sub/deep/c.mp4"])
        self.assertEqual([e.relpath for e in self.loc.walk(recursive=False)], ["a.mp4"])
        self.assertEqual(sorted(e.relpath for e in self.loc.walk("sub")),
                         ["sub/b.mp4", "sub/deep/c.mp4"])
        self.assertEqual(list(self.loc.walk("missing")), [])

    def test_publish(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"x" * 100)
        self.assertEqual(self.loc.publish(src, "a/b/out.bin", 1_000_000.0, 0), 100)
        out = self.loc.root / "a/b/out.bin"
        self.assertEqual(out.read_bytes(), b"x" * 100)
        self.assertEqual(out.stat().st_mtime, 1_000_000.0)
        self.assertEqual([p.name for p in out.parent.iterdir()], ["out.bin"])

    def test_publish_no_space(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"x" * 100)
        with mock.patch.object(self.loc, "free_bytes", return_value=150):
            with self.assertRaises(NoSpace):
                self.loc.publish(src, "out.bin", None, 100)
        self.assertEqual(list(self.loc.root.rglob("*")), [])

    def test_publish_removes_part_on_failure(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"x" * 10)
        with mock.patch("media_organizer.locations.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.loc.publish(src, "out.bin", None, 0)
        self.assertEqual(list(self.loc.root.rglob("*")), [])

    def test_fetch_and_size_mismatch(self):
        (self.loc.root).mkdir()
        (self.loc.root / "f.bin").write_bytes(b"abcdef")
        dest = self.tmp / "dest.bin"
        self.loc.fetch("f.bin", dest)
        self.assertEqual(dest.read_bytes(), b"abcdef")

        def truncating(src, d):
            Path(d).write_bytes(b"abc")
        with mock.patch("media_organizer.locations.shutil.copyfile", truncating):
            with self.assertRaises(OSError):
                self.loc.fetch("f.bin", dest)
        self.assertFalse(dest.exists())

    def test_move_delete_stat(self):
        self.loc.root.mkdir()
        (self.loc.root / "a.bin").write_bytes(b"1")
        self.loc.move_within("a.bin", "x/y/b.bin")
        self.assertFalse(self.loc.exists("a.bin"))
        self.assertEqual(self.loc.stat("x/y/b.bin").size, 1)
        self.loc.delete("x/y/b.bin")
        self.assertIsNone(self.loc.stat("x/y/b.bin"))
        self.assertFalse(self.loc.is_online_only("a.bin"))
        self.assertFalse(self.loc.evict("a.bin"))


class MacTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_unreachable_when_ssh_fails(self):
        runner = FakeRunner(rc=255)
        session = MacSession("mac", self.tmp / "mnt", runner)
        self.assertFalse(session.reachable())
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0], "ssh")

    def test_reachable_mounts_and_reads_clock(self):
        runner = FakeRunner(stdout="2000000000\n")
        session = MacSession("mac", self.tmp / "mnt", runner)
        mounted = []
        session._is_mounted = lambda: bool(mounted)

        def run(argv, **kw):
            if argv[0] == "sshfs":
                mounted.append(argv)
            return runner(argv, **kw)
        session._run = run
        self.assertTrue(session.reachable())
        self.assertEqual(mounted[0][:3], ["sshfs", "mac:", str(self.tmp / "mnt")])
        self.assertAlmostEqual(session.now(), 2_000_000_000, delta=5)

    def test_online_only(self):
        loc, runner = mac_location(self.tmp, FakeRunner(stdout="1073741888\n"))
        self.assertTrue(loc.is_online_only("a.mp4"))
        runner.stdout = "64\n"
        self.assertFalse(loc.is_online_only("a.mp4"))

    def test_fetch_requests_download_with_quoted_path(self):
        loc, runner = mac_location(self.tmp)
        (loc.root / "it's [a] file.mp4").write_bytes(b"data")
        loc.fetch("it's [a] file.mp4", self.tmp / "out.mp4")
        cmd = runner.calls[0][-1]
        self.assertTrue(cmd.startswith("brctl download "))
        self.assertIn("'home/MEGA/it'\"'\"'s [a] file.mp4'", cmd)
        self.assertEqual((self.tmp / "out.mp4").read_bytes(), b"data")

    def test_disconnect_becomes_mac_unreachable(self):
        loc, _ = mac_location(self.tmp)
        (loc.root / "f.mp4").write_bytes(b"data")
        src = self.tmp / "src.bin"
        src.write_bytes(b"1234")
        boom = OSError(errno.ENOTCONN, "not connected")
        with mock.patch("media_organizer.locations.shutil.copyfile", side_effect=boom):
            with self.assertRaises(MacUnreachable):
                loc.fetch("f.mp4", self.tmp / "d.mp4")
            with self.assertRaises(MacUnreachable):
                loc.publish(src, "out/new.mp4", None, 0)
        self.assertEqual([e.relpath for e in loc.walk()], ["f.mp4"])
        self.assertFalse((self.tmp / "d.mp4").exists())

    def test_dead_mount_is_unreachable(self):
        loc, _ = mac_location(self.tmp)
        with mock.patch("media_organizer.locations.os.scandir",
                        side_effect=OSError(errno.EIO, "io")), \
                mock.patch("media_organizer.locations.os.stat",
                           side_effect=OSError(errno.ENOTCONN, "x")):
            with self.assertRaises(MacUnreachable):
                list(loc.walk())

    def test_evict(self):
        loc, runner = mac_location(self.tmp)
        self.assertTrue(loc.evict("a b.mp4"))
        self.assertEqual(runner.calls[0][-1], "brctl evict 'home/MEGA/a b.mp4'")
        runner.rc = 1
        self.assertFalse(loc.evict("a.mp4"))

    def test_build_locations(self):
        locs = build_locations(test_config(self.tmp))
        self.assertEqual(set(locs), {"d", "mega", "win_dl", "mac_dl"})
        self.assertIs(locs["mega"].session, locs["mac_dl"].session)
        self.assertTrue(locs["mega"].is_mac)
        self.assertFalse(locs["d"].is_mac)


if __name__ == "__main__":
    unittest.main()
