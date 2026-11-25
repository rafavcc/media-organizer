"""Configuration: defaults plus optional flat TOML overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    d_root: Path = Path("/mnt/d/7_XYZ")
    d_intake: str = "_TO_CONVERT"
    d_converted: str = "_CONVERTED"
    win_downloads: Path = Path("/mnt/c/Users/rafavcc/Downloads")
    mac_host: str = "mac"  # ssh config alias
    mac_mega: str = "Library/Mobile Documents/com~apple~CloudDocs/MEGA"  # relative to Mac home
    mac_downloads: str = "Downloads"  # relative to Mac home
    mega_intake: str = "TBC"
    performers_file: Path = Path(__file__).resolve().parent.parent / "performers.txt"
    state_dir: Path = Path.home() / ".local/share/media-organizer"
    size_threshold_mb: int = 400
    scan_interval_h: float = 6.0
    full_scan_interval_h: float = 168.0  # D: whole-library scans
    mega_full_scan_interval_h: float = 0.5  # MEGA: whole-library scans (only while the Mac is up)
    mac_retry_h: float = 0.25  # a failed scan is retried this soon (or as soon as the Mac is back)
    folders_refresh_min: float = 5.0  # D: folder sizes for the dashboard (0 = only at scans)
    mac_folders_refresh_min: float = 60.0  # the same for MEGA, only while the Mac is up
    min_age_min: int = 10
    mac_delete_delay_s: int = 5
    image_workers: int = 4
    min_gain_ratio: float = 0.90
    min_ssim: float = 0.95
    crf: int = 29
    preset: str = "fast"
    max_video_mb: int = 1024  # TEMPORARY: bigger videos wait in the queue (0 = no limit)
    x265_pools: int = 8
    encode_stall_min: float = 30.0  # kill ffmpeg when its progress has not moved this long
    min_free_gb: float = 2.0
    web_port: int = 8002
    web_password: str = ""

    @property
    def db_path(self) -> Path:
        return self.state_dir / "organizer.db"

    @property
    def mac_mount(self) -> Path:
        return self.state_dir / "mac"

    @property
    def work_dir(self) -> Path:
        return self.state_dir / "work"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "organizer.log"

    @property
    def settings(self) -> str:
        return (
            f"r{self.min_gain_ratio * 100:.0f}s{self.min_ssim * 100:.0f}"
            f"c{self.crf}{self.preset}"
        )


DEFAULT_CONFIG_PATH = Path.home() / ".config/media-organizer/config.toml"

_PATH_FIELDS = {f.name for f in fields(Config) if f.type in (Path, "Path")}
_FIELD_NAMES = {f.name for f in fields(Config)}


def load_config(path: Path | None = None) -> Config:
    """Load config from a flat TOML file; a missing file yields the defaults."""
    path = DEFAULT_CONFIG_PATH if path is None else path
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return Config()
    unknown = sorted(set(data) - _FIELD_NAMES)
    if unknown:
        raise ValueError(f"unknown config key(s) in {path}: {', '.join(unknown)}")
    for name in _PATH_FIELDS & set(data):
        data[name] = Path(data[name]).expanduser()
    stall = data.get("encode_stall_min", 30)
    if isinstance(stall, bool) or not isinstance(stall, (int, float)) or not stall >= 1:
        raise ValueError(f"encode_stall_min in {path} must be a number of at least 1 (minutes)")
    cfg = Config(**data)
    _check_downloads(cfg)
    return cfg


def _under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _resolved(cfg: Config, key: str) -> str:
    """The absolute, symlink-resolved form of a local path key (it need not exist yet)."""
    path = getattr(cfg, key)
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute path, got {str(path)!r}")
    return os.path.normpath(str(path.resolve(strict=False)))


def _check_downloads(cfg: Config) -> None:
    """Refuse Downloads paths so broad that the sweep (which deletes) could do real damage."""
    win = _resolved(cfg, "win_downloads")
    others = {key: _resolved(cfg, key) for key in ("d_root", "state_dir")}
    parts = win.strip("/").split("/")
    drive = len(parts) >= 2 and parts[0] == "mnt" and len(parts[1]) == 1
    if (win == "/" or parts == ["mnt"] or (drive and len(parts) == 2) or
            (drive and len(parts) in (3, 4) and parts[2].lower() == "users") or
            win == os.path.normpath(str(Path.home().resolve(strict=False)))):
        raise ValueError(f"win_downloads is too broad: {cfg.win_downloads}")
    for key, other in others.items():
        if _under(win, other) or _under(other, win):
            raise ValueError(f"win_downloads ({cfg.win_downloads}) overlaps {key} ({other})")
    mac = os.path.normpath(cfg.mac_downloads.strip().strip("/") or ".").casefold()
    if (mac in (".", "..", "desktop", "documents", "library")
            or mac.startswith(("library/", "../"))):
        raise ValueError(f"mac_downloads is too broad: {cfg.mac_downloads!r}")
    mega = os.path.normpath(cfg.mac_mega.strip().strip("/") or ".").casefold()
    if _under(mac, mega) or _under(mega, mac):
        raise ValueError(f"mac_downloads ({cfg.mac_downloads}) overlaps mac_mega ({cfg.mac_mega})")
