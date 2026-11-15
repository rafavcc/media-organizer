"""Convert one local file into a smaller one and verify the result. Never touches the source."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import encode, probe, rules
from .config import Config

MAX_DURATION_DRIFT_S = 1.0
VIA_IM_EXT = frozenset({".heic", ".heif", ".tif", ".tiff", ".bmp"})


@dataclass(frozen=True)
class Outcome:
    status: str  # "ok" | "efficient" | "no-gain" | "low-quality" | "multi-frame" | "error"
    message: str
    out: Path | None = None  # set only when status == "ok"


def _temp(work: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(dir=work, prefix="mo-", suffix=suffix)
    os.close(fd)
    return Path(name)


def _tail(text: str, default: str) -> str:
    return (text or default).strip()[:150]


def _convert_video(src: Path, orig_size: int, work: Path, cfg: Config,
                   on_progress: Callable[[float], None] | None) -> Outcome:
    info = probe.probe_video(src)
    if info is None:
        return Outcome("error", "ffprobe could not read the video")
    bpp = rules.bits_per_pixel(info.bitrate, info.width, info.height, info.fps_value)
    if rules.video_decision(info.codec, bpp, cfg) == "efficient":
        return Outcome("efficient", f"already {info.codec}")

    tmp = _temp(work, ".mp4")
    keep = False
    try:
        rc, err = encode.run_video(src, tmp, info, cfg, on_progress)
        out = probe.probe_video(tmp) if rc == 0 and tmp.stat().st_size else None
        if (out is None or (out.width, out.height) != (info.width, info.height)
                or out.audio != info.audio
                or abs(out.duration - info.duration) > MAX_DURATION_DRIFT_S):
            return Outcome("error", _tail(err, "verification failed"))
        new = tmp.stat().st_size
        if new > orig_size * cfg.min_gain_ratio:
            return Outcome("no-gain", f"{new}/{orig_size}")
        keep = True
        return Outcome("ok", f"{orig_size} -> {new}", tmp)
    finally:
        if not keep:
            tmp.unlink(missing_ok=True)


FRAME_COUNT_EXT = frozenset({".tif", ".tiff", ".heic", ".heif", ".bmp"})


def _identify_cmd() -> list[str] | None:
    im = probe.imagemagick()
    if not im:
        return None
    if os.path.basename(im) == "magick":
        return [im, "identify"]
    ident = shutil.which("identify")
    return [ident] if ident else None


def _png_is_animated(src: Path) -> bool:
    """True when an acTL chunk comes before the first IDAT (APNG)."""
    with open(src, "rb") as f:
        head = f.read(65536)
    pos = 8
    while pos + 8 <= len(head):
        length = int.from_bytes(head[pos:pos + 4], "big")
        kind = head[pos + 4:pos + 8]
        if kind == b"acTL":
            return True
        if kind == b"IDAT":
            return False
        pos += 12 + length
    return False


def _multi_frame(src: Path, ext: str) -> Outcome | None:
    """An Outcome when the image has more than one frame/page (or we cannot tell), else None."""
    if ext == ".png":
        return Outcome("multi-frame", "animated PNG") if _png_is_animated(src) else None
    if ext not in FRAME_COUNT_EXT:
        return None
    cmd = _identify_cmd()
    if cmd is None:
        return Outcome("error", "ImageMagick identify not found")
    r = subprocess.run([*cmd, "-format", "%n\n", str(src.absolute())], capture_output=True,
                       text=True)
    try:
        frames = int(r.stdout.splitlines()[0])
    except (IndexError, ValueError):
        return Outcome("error", "could not count image frames " + r.stderr.strip()[:100])
    return Outcome("multi-frame", f"{frames} pages") if frames > 1 else None


def _convert_image(src: Path, ext: str, orig_size: int, work: Path, cfg: Config) -> Outcome:
    if ext != ".gif":
        early = _multi_frame(src, ext)
        if early is not None:
            return early
    tmp = _temp(work, ".webp")
    png = None  # lossless PNG stand-in for formats cwebp can't read
    keep = False
    try:
        encode_src = ref = src
        if ext in VIA_IM_EXT:
            im = probe.imagemagick()
            if not im:
                return Outcome("error", "ImageMagick not found")
            png = _temp(work, ".png")
            r = subprocess.run([im, f"{src.absolute()}[0]", "-auto-orient", str(png)],
                               capture_output=True, text=True)
            if r.returncode or not png.stat().st_size:
                return Outcome("error", "ImageMagick conversion failed " + r.stderr.strip()[:100])
            encode_src = ref = png

        r = subprocess.run(encode.image_cmd(ext, encode_src, tmp), capture_output=True, text=True)
        ok = r.returncode == 0 and tmp.stat().st_size > 0
        if ok and ext == ".gif":
            ok = probe.gif_frames(src) == probe.webp_frames(tmp)
        elif ok:
            ok = probe.dims(encode_src) == probe.dims(tmp)
        if not ok:
            return Outcome("error", _tail(r.stderr, "verification failed"))

        new = tmp.stat().st_size
        if new > orig_size * cfg.min_gain_ratio:
            return Outcome("no-gain", f"{new}/{orig_size}")

        if ext != ".gif":
            score = probe.ssim(ref, tmp)
            if score is None:
                return Outcome("error", "could not compute SSIM")
            if score < cfg.min_ssim:
                return Outcome("low-quality", f"SSIM {score:.3f}, {new}/{orig_size}")

        keep = True
        return Outcome("ok", f"{orig_size} -> {new}", tmp)
    finally:
        if not keep:
            tmp.unlink(missing_ok=True)
        if png:
            png.unlink(missing_ok=True)


def convert(src: Path, name: str, orig_size: int, work: Path, cfg: Config,
            on_progress: Callable[[float], None] | None = None) -> Outcome:
    """Convert src (whose original file name is `name`) inside `work`; verify before reporting ok."""
    kind = rules.kind_of(name)
    if kind == "video":
        return _convert_video(src, orig_size, work, cfg, on_progress)
    if kind == "image":
        return _convert_image(src, os.path.splitext(name)[1].lower(), orig_size, work, cfg)
    return Outcome("error", f"unsupported file type: {name}")
