"""Encoder command lines and the ffmpeg runner."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable

from .config import Config
from .probe import VideoInfo
from .rules import MP4_AUDIO

STDERR_TAIL = 4096


def _cwebp(quality: int) -> list[str]:
    return ["cwebp", "-quiet", "-q", str(quality), "-m", "6", "-sharp_yuv", "-metadata", "icc,exif"]


RECIPES = {
    ".png": _cwebp(90),
    ".jpg": _cwebp(85),
    ".jpeg": _cwebp(85),
    ".gif": ["gif2webp", "-quiet", "-lossy", "-q", "75", "-m", "6", "-mt"],
}
for _e in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # converted to a temp PNG first
    RECIPES[_e] = _cwebp(90)


def video_cmd(src: Path, out: Path, info: VideoInfo, cfg: Config) -> list[str]:
    """x265 10-bit encode; audio is copied when the MP4 container accepts it, else AAC."""
    audio = ["-c:a", "copy"] if set(info.acodecs) <= MP4_AUDIO else ["-c:a", "aac", "-b:a", "192k"]
    return ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src.absolute()), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx265", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p10le",
            "-x265-params", f"pools={cfg.x265_pools}",
            "-r", info.fps, "-fps_mode", "cfr", "-tag:v", "hvc1", *audio,
            "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", str(out.absolute())]


def image_cmd(ext: str, src: Path, out: Path) -> list[str]:
    return RECIPES[ext] + [str(src.absolute()), "-o", str(out.absolute())]


def run_video(src: Path, out: Path, info: VideoInfo, cfg: Config,
              on_progress: Callable[[float], None] | None = None) -> tuple[int, str]:
    """Run the encode; returns (returncode, last few KB of stderr). Never leaves ffmpeg running."""
    proc = subprocess.Popen(video_cmd(src, out, info, cfg), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace")
    tail: list[str] = [""]

    def drain() -> None:
        for chunk in iter(lambda: proc.stderr.read(1024), ""):
            tail[0] = (tail[0] + chunk)[-STDERR_TAIL:]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if on_progress and key == "out_time_us" and info.duration > 0:
                try:
                    done = int(value) / 1e6 / info.duration
                except ValueError:
                    continue
                on_progress(min(1.0, max(0.0, done)))
        rc = proc.wait()
        reader.join(timeout=5)
        return rc, tail[0]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
