"""Test helpers: tiny synthetic media and a Config rooted in a temp folder."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from media_organizer.config import Config


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def make_video(path: Path, *, codec="libx264", seconds=2, size="320x240", audio=True,
               extra: list[str] = ()) -> Path:
    inputs = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration={seconds}"]
    if audio:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    _ffmpeg(*inputs, "-c:v", codec, "-pix_fmt", "yuv420p", *extra, str(path))
    return path


def make_image(path: Path, *, size="640x480") -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}", "-frames:v", "1", str(path))
    return path


def make_gif(path: Path) -> Path:
    _ffmpeg("-f", "lavfi", "-i", "gradients=size=320x240:rate=10:duration=1", str(path))
    return path


def test_config(tmp: Path, **overrides) -> Config:
    base = replace(Config(), d_root=tmp / "d", win_downloads=tmp / "downloads",
                   state_dir=tmp / "state", sorter=tmp / "sorter.py", preset="fast", crf=30)  # tiny test clips need it to shrink 25%
    return replace(base, **overrides)
test_config.__test__ = False  # not a test, despite the name
