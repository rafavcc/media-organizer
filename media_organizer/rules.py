"""Pure decision rules: file kinds, eligibility, Downloads routing, output names."""
from __future__ import annotations

import posixpath
from typing import Callable

from .config import Config

VIDEO_EXT = frozenset({".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v"})
IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".tif", ".tiff", ".bmp"})
PARTIAL_EXT = frozenset({".part", ".crdownload", ".download", ".mo-part"})
SUFFIX = "_compressed"
MP4_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})


def _split(name: str) -> tuple[str, str]:
    stem, ext = posixpath.splitext(name)
    return stem, ext.lower()


def kind_of(name: str) -> str | None:
    ext = _split(name)[1]
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


def is_compressed_name(name: str) -> bool:
    return _split(name)[0].lower().endswith(SUFFIX)


def is_ignored(name: str) -> bool:
    return name.startswith(".") or _split(name)[1] in PARTIAL_EXT


def is_candidate(name: str) -> bool:
    return kind_of(name) is not None and not is_compressed_name(name) and not is_ignored(name)


def out_ext(name: str) -> str:
    return ".mp4" if kind_of(name) == "video" else ".webp"


def bits_per_pixel(bitrate: int | None, width: int, height: int, fps: float) -> float | None:
    if bitrate is None or not width or not height or not fps:
        return None
    return bitrate / (width * height * fps)


def video_decision(codec: str, bpp: float | None, cfg: Config) -> str:
    """HEVC is re-encoded only when its bits per pixel exceed the bloat threshold; AV1 never."""
    codec = codec.lower()
    if codec == "av1":
        return "efficient"
    if codec == "hevc":
        return "encode" if bpp is not None and bpp > cfg.hevc_bloat_bpp else "efficient"
    return "encode"


def route_download(name: str, size: int, cfg: Config) -> str | None:
    if is_ignored(name):
        return None
    kind = kind_of(name)
    if kind is None and _split(name)[1] != ".webp":
        return None
    if kind == "video" and size > cfg.size_threshold_mb * 1024 * 1024:
        return "d"
    return "mega"


def final_name(name: str, ext: str, taken: Callable[[str], bool]) -> str:
    """Pick a free <stem>_compressed<ext>; a.png and a.jpg must not collide."""
    stem, src_ext = posixpath.splitext(name)
    candidate = f"{stem}{SUFFIX}{ext}"
    n = 0
    while taken(candidate):
        n += 1
        candidate = f"{stem}_{src_ext[1:].lower()}{'' if n == 1 else n}{SUFFIX}{ext}"
    return candidate


def free_name(name: str, taken: Callable[[str], bool]) -> str:
    stem, ext = posixpath.splitext(name)
    candidate = name
    n = 0
    while taken(candidate):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    return candidate
