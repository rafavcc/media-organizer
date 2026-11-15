"""Locations: one file interface over a local folder and over a Mac (sshfs mount + ssh commands)."""
from __future__ import annotations

import errno
import functools
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from media_organizer import rules
from media_organizer.config import Config

log = logging.getLogger(__name__)

DISCONNECT_ERRNOS = {errno.ENOTCONN, errno.ETIMEDOUT, errno.ECONNABORTED, errno.ECONNRESET,
                     errno.EHOSTDOWN, errno.EHOSTUNREACH}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/media-organizer-%C", "-o", "ControlPersist=120"]
SF_DATALESS = 0x40000000
PART_SUFFIX = ".mo-part"


class MacUnreachable(Exception):
    """The Mac (ssh or the sshfs mount) cannot be reached."""


class NoSpace(Exception):
    """Not enough free space to publish a file."""


@dataclass(frozen=True)
class Entry:
    relpath: str
    size: int
    mtime: float


class Location:
    name: str
    root: Path
    is_mac: bool = False


class LocalLocation(Location):
    retry_delay = 10.0  # seconds multiplied by the attempt number between fetch retries

    def __init__(self, name: str, root: Path):
        self.name = name
        self.root = Path(root)

    # -- hooks for subclasses --
    def _fatal(self, exc: OSError) -> bool:
        """True when a copy error should not be retried."""
        return False

    # -- interface --
    def now(self) -> float:
        return time.time()

    def reachable(self) -> bool:
        return True

    def path(self, relpath: str) -> Path:
        return self.root / relpath

    def _skip(self, e: OSError, rel: str, skipped: list[str] | None, quiet: bool) -> None:
        """Handle an error on a folder: raise it when the Mac is gone (the guard turns it into
        MacUnreachable), else log it and note the folder as skipped."""
        if self._fatal(e) or (self.is_mac and not isinstance(e, PermissionError)
                              and not self.session.ssh_alive()):
            raise e
        if not quiet:
            log.warning("cannot read %s %s: %r (skipped)", self.name, rel or ".", e)
        if skipped is not None:
            skipped.append(rel)

    def _listing(self, folder: Path, rel: str, skipped: list[str] | None, quiet: bool = False):
        """Sorted entries of a folder, or None when it vanished or cannot be listed."""
        try:
            with os.scandir(folder) as it:
                return sorted(it, key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            self._skip(e, rel, skipped, quiet)
            return None

    def walk(self, sub: str = "", recursive: bool = True, skipped: list[str] | None = None,
             quiet: bool = False) -> Iterator[Entry]:
        """Every file under `sub`. A folder that cannot be read (listing or stat-ing its entries
        fails) is skipped whole, logged (unless `quiet`) and added to `skipped`."""
        stack = [(self.path(sub) if sub else self.root, sub)]
        while stack:
            folder, rel = stack.pop()
            items = self._listing(folder, rel, skipped, quiet)
            if items is None:
                continue
            subdirs, files = [], []
            try:
                for item in items:
                    if item.name.startswith("."):
                        continue
                    child = f"{rel}/{item.name}" if rel else item.name
                    try:  # symlinks and junctions are never followed
                        if item.is_dir(follow_symlinks=False):
                            if recursive and not rules.is_skipped_dir(item.name):
                                subdirs.append((Path(item.path), child))
                        elif item.is_file(follow_symlinks=False):
                            st = item.stat(follow_symlinks=False)
                            files.append(Entry(child, st.st_size, st.st_mtime))
                    except FileNotFoundError:
                        continue  # vanished while walking
            except OSError as e:
                self._skip(e, rel, skipped, quiet)
                continue
            yield from files
            stack.extend(reversed(subdirs))

    def walk_dirs(self, sub: str = "", skipped: list[str] | None = None,
                  quiet: bool = False) -> Iterator[Entry]:
        """Every real folder under `sub` (never the start folder, dot-folders, package folders or
        symlinks), children before their parents; size is 0 and mtime is the folder's own, read
        before any removal. A folder that cannot be read is left out together with its ancestors
        (it is not known to be empty) and added to `skipped`."""
        found: list[Entry] = []
        bad: list[str] = []
        stack = [(self.path(sub) if sub else self.root, sub)]
        while stack:
            folder, rel = stack.pop()
            items = self._listing(folder, rel, bad, quiet)
            if items is None:
                continue
            here: list[tuple[Entry, Path]] = []
            try:
                for item in items:
                    if rules.is_skipped_dir(item.name):
                        continue
                    try:
                        if not item.is_dir(follow_symlinks=False):
                            continue
                        child = f"{rel}/{item.name}" if rel else item.name
                        entry = Entry(child, 0, item.stat(follow_symlinks=False).st_mtime)
                    except FileNotFoundError:
                        continue
                    here.append((entry, Path(item.path)))
            except OSError as e:
                self._skip(e, rel, bad, quiet)
                continue
            for entry, path in here:
                found.append(entry)
                stack.append((path, entry.relpath))
        if skipped is not None:
            skipped.extend(bad)
        blocked = lambda r: any(r == b or b.startswith(r + "/") for b in bad)
        # a parent is always listed before its children
        return iter([e for e in reversed(found) if not blocked(e.relpath)])

    def stat(self, relpath: str) -> Entry | None:
        try:
            st = self.path(relpath).stat()
        except FileNotFoundError:
            return None
        return Entry(relpath, st.st_size, st.st_mtime)

    def exists(self, relpath: str) -> bool:
        return self.stat(relpath) is not None

    def list_names(self, folder: str) -> set[str]:
        """Names of the entries directly inside `folder` (empty when it does not exist)."""
        try:
            with os.scandir(self.path(folder) if folder else self.root) as it:
                return {e.name for e in it}
        except (FileNotFoundError, NotADirectoryError):
            return set()

    def fetch(self, relpath: str, dest: Path) -> None:
        src = self.path(relpath)
        err = ""
        for attempt in range(1, 4):
            try:
                want = src.stat().st_size
                shutil.copyfile(src, dest)
                got = Path(dest).stat().st_size
                if got == want:
                    return
                err = f"got {got} of {want} bytes"
            except OSError as e:
                if self._fatal(e):
                    Path(dest).unlink(missing_ok=True)
                    raise
                err = repr(e)
            if attempt < 3:
                time.sleep(self.retry_delay * attempt)
        Path(dest).unlink(missing_ok=True)
        raise OSError(f"could not copy {relpath}: {err}")

    def publish(self, src: Path, relpath: str, mtime: float | None, min_free: int) -> int:
        size = Path(src).stat().st_size
        if self.free_bytes() < size + min_free:
            raise NoSpace(f"{self.name}: need {size + min_free} bytes free for {relpath}")
        final = self.path(relpath)
        if os.path.lexists(final):
            raise FileExistsError(f"{self.name}: {relpath} already exists")
        # unique per call, so two publishes aimed at one name never share a part file
        part = final.with_name(f"{final.name}.{secrets.token_hex(4)}{PART_SUFFIX}")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, part)
            got = part.stat().st_size
            if got != size:
                raise OSError(f"{relpath}: wrote {got} of {size} bytes")
            if mtime is not None:
                os.utime(part, (mtime, mtime))
            os.replace(part, final)
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        got = final.stat().st_size
        if got != size:
            final.unlink()
            raise OSError(f"{relpath}: wrote {got} of {size} bytes")
        return size

    def delete(self, relpath: str) -> None:
        self.path(relpath).unlink()

    def rmdir(self, relpath: str) -> None:
        """Remove one empty folder (fails when anything is inside). Never the location root."""
        if not relpath.strip("/"):
            raise ValueError("refusing to remove the location root")
        os.rmdir(self.path(relpath))

    def is_plain_file(self, relpath: str) -> bool:
        """A regular file: not a symlink, not a folder."""
        try:
            return stat.S_ISREG(os.lstat(self.path(relpath)).st_mode)
        except FileNotFoundError:
            return False

    def move_within(self, relpath: str, new_relpath: str) -> None:
        new = self.path(new_relpath)
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.path(relpath), new)

    def free_bytes(self) -> int:
        probe = self.root
        while not probe.exists() and probe != probe.parent:  # root may not be created yet
            probe = probe.parent
        return shutil.disk_usage(probe).free

    def is_online_only(self, relpath: str) -> bool:
        return False

    def evict(self, relpath: str) -> bool:
        return False


class MacSession:
    """One ssh host and its sshfs mount, shared by every Mac location."""

    def __init__(self, host: str, mount: Path, runner: Callable = subprocess.run):
        self.host = host
        self.mount = Path(mount)
        self._run = runner
        self._offset = 0.0

    def ssh(self, command: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return self._run(["ssh", *SSH_OPTS, self.host, command],
                         capture_output=True, text=True, timeout=timeout)

    def _mounts_text(self) -> str:
        return Path("/proc/self/mounts").read_text()

    def _is_mounted(self) -> bool:
        unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
        for line in self._mounts_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "fuse.sshfs" and Path(unescape(parts[1])) == self.mount:
                return True
        return False

    def require_mounted(self) -> None:
        """Raise MacUnreachable unless the sshfs mount is up. Never mounts; only reachable() does."""
        if not self._is_mounted():
            raise MacUnreachable(f"{self.mount} is not mounted")

    def ssh_alive(self) -> bool:
        """Quick non-mounting check: does `ssh <host> true` succeed within 10 s?"""
        try:
            return self.ssh("true", timeout=10).returncode == 0
        except Exception:
            return False

    def _mount_alive(self) -> bool:
        try:
            os.stat(self.mount)
            return True
        except OSError:
            return False

    def reachable(self) -> bool:
        """True when ssh works and the sshfs mount is up (mounting it if needed). Never raises."""
        try:
            if self.ssh("true", timeout=20).returncode != 0:
                return False
            if self._is_mounted() and not self._mount_alive():
                self.unmount()
            if not (self._is_mounted() and self._mount_alive()):
                self.mount.mkdir(parents=True, exist_ok=True)
                r = self._run(["sshfs", f"{self.host}:", str(self.mount), "-o",
                               "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3"],
                              capture_output=True, text=True, timeout=60)
                if r.returncode != 0 or not (self._is_mounted() and self._mount_alive()):
                    return False
            self._measure_offset()
            return True
        except Exception:
            return False

    def _measure_offset(self) -> None:
        """The Mac's clock minus WSL's, which can drift; keeps the old offset on failure."""
        try:
            before = time.time()
            r = self.ssh("date +%s", timeout=20)
            if r.returncode == 0:
                self._offset = int(r.stdout.strip()) - before
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass

    def unmount(self) -> None:
        try:
            self._run(["fusermount", "-u", "-z", str(self.mount)],
                      capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    def now(self) -> float:
        return time.time() + self._offset


def _mac_guard(fn):
    """Turn disconnect errors (or a dead mount) into MacUnreachable."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self.session.require_mounted()
        try:
            return fn(self, *args, **kwargs)
        except OSError as e:
            if self._fatal(e):
                raise MacUnreachable(repr(e)) from e
            # EIO and friends with the mount still answering: only an unreachable Mac is a
            # reason to wait; an ssh that works means the error is real
            if not isinstance(e, FileExistsError) and not self.session.ssh_alive():
                raise MacUnreachable(f"ssh check failed after {e!r}") from e
            raise
    return wrapper


class MacLocation(LocalLocation):
    is_mac = True

    def __init__(self, name: str, session: MacSession, home_rel: str):
        super().__init__(name, session.mount / home_rel)
        self.session = session
        self.home_rel = home_rel.strip("/")

    def _fatal(self, exc: OSError) -> bool:
        if exc.errno in DISCONNECT_ERRNOS:
            return True
        try:
            os.stat(self.session.mount)
        except OSError:
            return True
        return False

    def _remote(self, relpath: str) -> str:
        return shlex.quote(f"{self.home_rel}/{relpath}")

    def now(self) -> float:
        return self.session.now()

    def reachable(self) -> bool:
        return self.session.reachable()

    @_mac_guard
    def walk(self, sub: str = "", recursive: bool = True, skipped: list[str] | None = None,
             quiet: bool = False) -> Iterator[Entry]:
        # materialised so that errors surface here, inside the guard
        return iter(list(super().walk(sub, recursive, skipped, quiet)))

    @_mac_guard
    def walk_dirs(self, sub: str = "", skipped: list[str] | None = None,
                  quiet: bool = False) -> Iterator[Entry]:
        return iter(list(super().walk_dirs(sub, skipped, quiet)))

    stat = _mac_guard(LocalLocation.stat)
    rmdir = _mac_guard(LocalLocation.rmdir)
    is_plain_file = _mac_guard(LocalLocation.is_plain_file)
    list_names = _mac_guard(LocalLocation.list_names)
    exists = _mac_guard(LocalLocation.exists)
    delete = _mac_guard(LocalLocation.delete)
    move_within = _mac_guard(LocalLocation.move_within)
    publish = _mac_guard(LocalLocation.publish)

    @_mac_guard
    def free_bytes(self) -> int:
        # strict: never fall back to a local ancestor, which would measure the wrong disk
        return shutil.disk_usage(self.root).free

    @_mac_guard
    def fetch(self, relpath: str, dest: Path) -> None:
        try:  # best effort: ask the Mac to download an iCloud-only file first
            self.session.ssh(f"brctl download {self._remote(relpath)}", timeout=300)
        except (subprocess.TimeoutExpired, OSError):
            pass
        super().fetch(relpath, dest)

    def is_online_only(self, relpath: str) -> bool:
        try:
            r = self.session.ssh(f"stat -f %f {self._remote(relpath)}")
        except (subprocess.TimeoutExpired, OSError):
            return False
        if r.returncode == 255:
            raise MacUnreachable("ssh failed")
        try:
            return bool(int(r.stdout.strip()) & SF_DATALESS)
        except ValueError:
            return False

    def evict(self, relpath: str) -> bool:
        try:
            return self.session.ssh(f"brctl evict {self._remote(relpath)}").returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def build_locations(cfg: Config) -> dict[str, Location]:
    session = MacSession(cfg.mac_host, cfg.mac_mount)
    return {
        "d": LocalLocation("d", cfg.d_root),
        "win_dl": LocalLocation("win_dl", cfg.win_downloads),
        "mega": MacLocation("mega", session, cfg.mac_mega),
        "mac_dl": MacLocation("mac_dl", session, cfg.mac_downloads),
    }
