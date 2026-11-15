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
    session._is_mounted = lambda: True  # the fake mount is a plain temp folder
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

    def test_walk_never_follows_symlinks(self):
        r = self.loc.root
        (r / "real").mkdir(parents=True)
        (r / "real" / "a.mp4").write_bytes(b"1")
        os.symlink(r, r / "real" / "loop")
        os.symlink(r / "real" / "a.mp4", r / "link.mp4")
        os.symlink(r / "real", r / "dirlink")
        self.assertEqual([e.relpath for e in self.loc.walk()], ["real/a.mp4"])
        self.assertEqual([e.relpath for e in self.loc.walk_dirs()], ["real"])

    def test_walk_dirs_bottom_up_skips_dot_dirs_and_root(self):
        r = self.loc.root
        for rel in ["a/b/c", "a/d", ".h/x", "e"]:
            (r / rel).mkdir(parents=True)
        rels = [e.relpath for e in self.loc.walk_dirs()]
        self.assertEqual(sorted(rels), ["a", "a/b", "a/b/c", "a/d", "e"])
        for rel in rels:
            for other in rels:
                if other.startswith(rel + "/"):
                    self.assertLess(rels.index(other), rels.index(rel))
        self.assertEqual(list(self.loc.walk_dirs("missing")), [])

    def test_rmdir_only_empty_and_never_root(self):
        r = self.loc.root
        (r / "a").mkdir(parents=True)
        (r / "b").mkdir()
        (r / "b" / "f").write_bytes(b"1")
        self.loc.rmdir("a")
        self.assertFalse((r / "a").exists())
        with self.assertRaises(OSError):
            self.loc.rmdir("b")
        with self.assertRaises(ValueError):
            self.loc.rmdir("")
        self.assertTrue((r / "b" / "f").exists())

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

    def test_publish_short_copy_leaves_nothing(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"x" * 10)

        def truncating(s, d):
            Path(d).write_bytes(b"xx")
        with mock.patch("media_organizer.locations.shutil.copyfile", truncating), \
                mock.patch("media_organizer.locations.os.replace") as replace:
            with self.assertRaises(OSError):
                self.loc.publish(src, "out.bin", None, 0)
        replace.assert_not_called()  # a short part must never reach the final name
        self.assertEqual(list(self.loc.root.rglob("*.bin*")), [])

    def test_publish_part_names_are_unique_and_end_in_mo_part(self):
        import re
        src = self.tmp / "src.bin"
        src.write_bytes(b"x" * 10)
        seen = []
        real = locations.shutil.copyfile

        def spy(s, d):
            seen.append(Path(d).name)
            return real(s, d)
        with mock.patch("media_organizer.locations.shutil.copyfile", spy):
            self.loc.publish(src, "out.bin", None, 0)
            self.loc.publish(src, "out2.bin", None, 0)
            (self.loc.root / "out.bin").unlink()
            self.loc.publish(src, "out.bin", None, 0)
        self.assertTrue(all(re.fullmatch(r"out2?\.bin\.[0-9a-f]{8}\.mo-part", n) for n in seen), seen)
        self.assertEqual(len(set(seen)), 3)

    def test_list_names(self):
        (self.loc.root / "a").mkdir(parents=True)
        (self.loc.root / "a" / "IMG.JPG").write_bytes(b"1")
        self.assertEqual(self.loc.list_names("a"), {"IMG.JPG"})
        self.assertEqual(self.loc.list_names("missing"), set())

    def test_publish_never_overwrites(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"new")
        (self.loc.root).mkdir()
        (self.loc.root / "out.bin").write_bytes(b"precious")
        with self.assertRaises(FileExistsError):
            self.loc.publish(src, "out.bin", None, 0)
        self.assertEqual((self.loc.root / "out.bin").read_bytes(), b"precious")
        self.assertEqual([p.name for p in self.loc.root.iterdir()], ["out.bin"])

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

    def test_unmounted_mount_point_is_unreachable(self):
        loc, _ = mac_location(self.tmp)
        loc.session._is_mounted = lambda: False
        (loc.root / "f.mp4").write_bytes(b"data")  # shadow file on the local disk
        src = self.tmp / "src.bin"
        src.write_bytes(b"1234")
        before = sorted(self.tmp.rglob("*"))
        for call in (lambda: list(loc.walk()), lambda: loc.stat("f.mp4"),
                     lambda: loc.exists("f.mp4"), lambda: loc.free_bytes(),
                     lambda: loc.publish(src, "new/out.mp4", None, 0),
                     lambda: loc.delete("f.mp4"), lambda: loc.rmdir("sub"),
                     lambda: list(loc.walk_dirs()), lambda: loc.is_symlink("f.mp4"),
                     lambda: loc.move_within("f.mp4", "g.mp4"),
                     lambda: loc.fetch("f.mp4", self.tmp / "d.mp4")):
            with self.assertRaises(MacUnreachable):
                call()
        self.assertEqual(sorted(self.tmp.rglob("*")), before)

    def test_non_disconnect_error_stays_oserror(self):
        loc, _ = mac_location(self.tmp)
        src = self.tmp / "src.bin"
        src.write_bytes(b"1234")
        with mock.patch("media_organizer.locations.shutil.copyfile",
                        side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(PermissionError):
                loc.publish(src, "out.mp4", None, 0)

    def test_eio_with_ssh_down_is_mac_unreachable(self):
        runner = FakeRunner(rc=255)
        loc, _ = mac_location(self.tmp, runner)
        (loc.root / "f.mp4").write_bytes(b"data")
        src = self.tmp / "src.bin"
        src.write_bytes(b"1234")
        boom = OSError(errno.EIO, "input/output error")
        with mock.patch("media_organizer.locations.shutil.copyfile", side_effect=boom):
            with self.assertRaises(MacUnreachable):
                loc.fetch("f.mp4", self.tmp / "d.mp4")
            with self.assertRaises(MacUnreachable):
                loc.publish(src, "out.mp4", None, 0)
        with mock.patch("media_organizer.locations.os.scandir", side_effect=boom):
            with self.assertRaises(MacUnreachable):
                list(loc.walk())
        self.assertIn(["ssh", *locations.SSH_OPTS, "mac", "true"], runner.calls)

    def test_eio_with_ssh_up_stays_oserror(self):
        loc, _ = mac_location(self.tmp)
        (loc.root / "f.mp4").write_bytes(b"data")
        boom = OSError(errno.EIO, "input/output error")
        with mock.patch("media_organizer.locations.shutil.copyfile", side_effect=boom):
            with self.assertRaises(OSError) as cm:
                loc.fetch("f.mp4", self.tmp / "d.mp4")
        self.assertNotIsInstance(cm.exception, MacUnreachable)

    def test_ssh_check_uses_ten_second_timeout(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        loc, _ = mac_location(self.tmp, runner)
        self.assertTrue(loc.session.ssh_alive())
        self.assertEqual(seen["timeout"], 10)

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
