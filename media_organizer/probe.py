"""Probing media files with ffprobe, webpmux and ffmpeg; tool availability."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int
    height: int
    fps: str
    fps_value: float
    duration: float
    audio: int
    acodecs: tuple[str, ...]
    bitrate: int | None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def imagemagick() -> str | None:
    return shutil.which("magick") or shutil.which("convert")


def check_tools() -> list[str]:
    """Names of missing tools; an empty list means everything is available."""
    missing = [t for t in ("cwebp", "gif2webp", "webpmux", "ffmpeg", "ffprobe", "exiftool", "sshfs", "ssh")
               if not shutil.which(t)]
    if not imagemagick():
        missing.append("imagemagick")
    if shutil.which("ffmpeg") and "libx265" not in _run(["ffmpeg", "-hide_banner", "-encoders"]).stdout:
        missing.append("ffmpeg with libx265")
    return missing


def dims(p: Path) -> str:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "stream=width,height", "-of", "csv=p=0", str(p.absolute())])
    return r.stdout.strip()


def gif_frames(p: Path) -> str:
    r = _run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(p.absolute())])
    return r.stdout.strip()


def webp_frames(p: Path) -> str:
    m = re.search(r"Number of frames: (\d+)", _run(["webpmux", "-info", str(p.absolute())]).stdout)
    return m.group(1) if m else "1"  # static WebP has no frame count


def ssim(ref: Path, other: Path) -> float | None:
    r = _run(["ffmpeg", "-hide_banner", "-i", str(ref.absolute()), "-i", str(other.absolute()),
              "-lavfi", "ssim", "-f", "null", "-"])
    m = re.search(r"SSIM.*?All:([\d.]+)", r.stderr)
    return float(m.group(1)) if m else None


def _fps_value(fps: str) -> float:
    try:
        num, _, den = fps.partition("/")
        return float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _int_or_none(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def probe_video(p: Path) -> VideoInfo | None:
    """Codec, size, fps, duration, audio streams and bitrate, or None if unreadable."""
    r = _run(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate"
              ":format=duration,bit_rate",
              "-of", "json", str(p.absolute())])
    try:
        j = json.loads(r.stdout)
        streams = j["streams"]
        v = next(s for s in streams if s["codec_type"] == "video")
        fps = v.get("avg_frame_rate", "0/0")
        if fps in ("0/0", "0"):
            fps = v["r_frame_rate"]
        bitrate = _int_or_none(v.get("bit_rate")) or _int_or_none(j["format"].get("bit_rate"))
        return VideoInfo(
            codec=v["codec_name"], width=int(v["width"]), height=int(v["height"]),
            fps=fps, fps_value=_fps_value(fps), duration=float(j["format"]["duration"]),
            audio=sum(s["codec_type"] == "audio" for s in streams),
            acodecs=tuple(s.get("codec_name", "") for s in streams if s["codec_type"] == "audio"),
            bitrate=bitrate)
    except (KeyError, StopIteration, ValueError, TypeError, json.JSONDecodeError):
        return None
