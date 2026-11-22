"""Shrink single image files in place, outside the service (e.g. from a gallery-dl exec hook).

Each image goes through the same convert() and checks as the service. On success the .webp
lands next to the original (never overwriting an existing file), takes over its mtime, and the
original is deleted. Anything else leaves the original untouched. No database is involved.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import rules
from .config import Config
from .convert import convert


@dataclass(frozen=True)
class ShrinkResult:
    status: str  # "ok" | "skipped" | "no-gain" | "low-quality" | "multi-frame" | "error"
    message: str
    path: Path  # where the file is now


def shrink_file(src: Path, cfg: Config) -> ShrinkResult:
    if not src.is_file():
        return ShrinkResult("error", "not a file", src)
    if rules.kind_of(src.name) != "image":
        return ShrinkResult("skipped", "not a convertible image", src)

    st = src.stat()
    outcome = convert(src, src.name, st.st_size, src.parent, cfg)
    if outcome.status != "ok":
        return ShrinkResult(outcome.status, outcome.message, src)

    name = rules.free_name(src.stem + ".webp", lambda n: (src.parent / n).exists())
    target = src.parent / name
    try:
        os.replace(outcome.out, target)
        os.chmod(target, stat.S_IMODE(st.st_mode))  # temp files are created 0600
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError as e:
        outcome.out.unlink(missing_ok=True)
        return ShrinkResult("error", f"could not place the result: {e}", src)
    src.unlink()
    return ShrinkResult("ok", outcome.message, target)
