"""Configuration: defaults plus optional flat TOML overrides."""
from __future__ import annotations

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
    sorter: Path = Path(
        "/mnt/c/Users/rafavcc/iCloudDrive/Programming/Projects/fileSorting/fileSortingv6.py"
    )
    state_dir: Path = Path.home() / ".local/share/media-organizer"
    size_threshold_mb: int = 400
    scan_interval_h: float = 6.0
    full_scan_interval_h: float = 168.0
    mac_retry_h: float = 1.0
    min_age_min: int = 10
    mac_delete_delay_s: int = 60
    image_workers: int = 4
    min_gain_ratio: float = 0.75
    min_ssim: float = 0.95
    crf: int = 30
    preset: str = "fast"
    x265_pools: int = 10
    encode_stall_min: float = 30.0  # kill ffmpeg when its progress has not moved this long
    hevc_bloat_bpp: float = 0.07
    min_free_gb: float = 2.0
    web_port: int = 8765
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
            f"c{self.crf}b{self.hevc_bloat_bpp * 1000:.0f}"
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
    return Config(**data)
