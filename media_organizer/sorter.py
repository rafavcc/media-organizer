"""Sort finished videos from _CONVERTED into D:/<performer>/ (the logic of the old fileSortingv6.py).

A file is matched by performer name (whole words, case and punctuation ignored, "Pmv Compilation"
first, then alphabetical). If the performer folder already holds a file with the same name, the
incoming file replaces it (the newer one wins). Files with no match stay in _CONVERTED; a later
sort picks them up once their name is added to the performers file.
"""
from __future__ import annotations

import logging
import posixpath
import re
import threading
from pathlib import Path

from . import db
from .config import Config
from .locations import Location

log = logging.getLogger(__name__)

PMV = "Pmv Compilation"  # always checked first
VIDEO_EXTS = frozenset({".mp4", ".avi", ".mkv", ".icloud", ".wmv", ".m4v", ".mov", ".webm"})
IN_FLIGHT = ("queued", "running", "published")  # never move a file the worker may still touch

_lock = threading.Lock()  # one sort at a time (a worker thread and the main loop both sort)
_cache: dict = {"key": None, "matchers": None, "missing_logged": False}

Matchers = list[tuple[str, re.Pattern]]


def normalize(text: str) -> str:
    """Lowercase, turn every run of non-alphanumerics (. - _ + , & ( ) ...) into one space."""
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def load_names(path: Path, write: bool = True) -> list[str]:
    """Read names, title-case, de-duplicate, sort with PMV first; rewrite the file if that changed it."""
    original = path.read_text(encoding="utf-8")
    names = sorted({line.strip().title() for line in original.splitlines() if line.strip()} - {PMV})
    names.insert(0, PMV)
    cleaned = "\n".join(names) + "\n"
    if write and cleaned != original:
        path.write_text(cleaned, encoding="utf-8")
        log.info("cleaned up %s (%d names)", path.name, len(names))
    return names


def build_matchers(names: list[str]) -> Matchers:
    """One whole-word pattern per name, in priority order: 'Ai' does not match inside 'maid'."""
    return [(name, re.compile(rf"(?<!\w){re.escape(normalize(name))}(?!\w)"))
            for name in names if normalize(name)]


def match_person(matchers: Matchers, filename: str) -> str | None:
    text = normalize(posixpath.splitext(filename)[0])
    return next((name for name, pattern in matchers if pattern.search(text)), None)


def matchers_for(cfg: Config) -> Matchers | None:
    """The performer matchers, re-read only when the file changes. None when the file is missing."""
    path = cfg.performers_file
    try:
        st = path.stat()
    except OSError:
        if not _cache["missing_logged"]:
            log.error("performers file missing: %s (nothing is sorted until it exists)", path)
            _cache["missing_logged"] = True
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache["key"] != key:
        names = load_names(path)
        st = path.stat()  # load_names may have rewritten it
        _cache.update(key=(str(path), st.st_mtime_ns, st.st_size),
                      matchers=build_matchers(names), missing_logged=False)
    return _cache["matchers"]


def _busy(con, location: str, relpath: str) -> bool:
    """True if a row the worker may still act on owns this path (as source or planned output)."""
    marks = ",".join("?" * len(IN_FLIGHT))
    return con.execute(
        f"SELECT 1 FROM items WHERE status IN ({marks}) AND "
        f"((location=? AND relpath=?) OR (COALESCE(dest_location, location)=? AND final_relpath=?)) "
        f"LIMIT 1", (*IN_FLIGHT, location, relpath, location, relpath)).fetchone() is not None


def sort_file(con, loc: Location, relpath: str, matchers: Matchers,
              dry_run: bool = False) -> str | None:
    """Move one file into its performer's folder. Returns the destination relpath, or None."""
    name = posixpath.basename(relpath)
    if name.startswith("._") or posixpath.splitext(name)[1].lower() not in VIDEO_EXTS:
        return None
    person = match_person(matchers, name)
    if person is None:
        return None
    dest = f"{person}/{name}"
    if _busy(con, loc.name, relpath) or _busy(con, loc.name, dest):
        return None  # try again on a later pass
    replaced = loc.exists(dest)
    if dry_run:
        return dest
    loc.move_within(relpath, dest)  # an atomic rename; an existing file of that name is replaced
    row = con.execute("SELECT id FROM items WHERE location=? AND relpath=?",
                      (loc.name, relpath)).fetchone()
    if row is not None:
        db.rename_item(con, row["id"], dest)  # also drops the replaced file's row
    else:
        db.forget_path(con, loc.name, dest)
    log.info("SORTED %s -> %s%s", relpath, dest, " (replaced the older file)" if replaced else "")
    return dest


def sort_one(con, loc: Location, cfg: Config, relpath: str) -> str | None:
    """Sort a single file right after it reached _CONVERTED. Never raises."""
    try:
        with _lock:
            matchers = matchers_for(cfg)
            return None if matchers is None else sort_file(con, loc, relpath, matchers)
    except Exception:
        log.exception("sorting %s failed", relpath)
        return None


def sort_converted(con, loc: Location, cfg: Config,
                   dry_run: bool = False) -> list[tuple[str, str]]:
    """Sort every matching video under _CONVERTED (sub-folders too). Returns (from, to) pairs.

    A dry run moves nothing and does not tidy the performers file."""
    if not loc.exists(cfg.d_converted):
        return []
    moved = []
    with _lock:
        if dry_run:
            path = cfg.performers_file
            matchers = build_matchers(load_names(path, write=False)) if path.exists() else None
        else:
            matchers = matchers_for(cfg)
        if matchers is None:
            return []
        for entry in list(loc.walk(cfg.d_converted, quiet=True)):
            try:
                dest = sort_file(con, loc, entry.relpath, matchers, dry_run)
            except Exception:
                log.exception("sorting %s failed", entry.relpath)
                continue
            if dest:
                moved.append((entry.relpath, dest))
    return moved
