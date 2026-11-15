"""One-off import of the old compactor tool's state files (its rejected/error flags)."""
from __future__ import annotations

import json
from pathlib import Path

from . import db, rules
from .config import Config

DEFAULT_STATE_FILES = [Path.home() / ".local/share/compactor" / n
                       for n in ("state.json", "state-mac.json")]
OLD_SETTINGS = "r75s95"
PRIORITY = {"d": 3, "mega": 2}


def _map_path(path: str, cfg: Config) -> tuple[str, str] | None:
    d_root = str(cfg.d_root).rstrip("/") + "/"
    if path.startswith(d_root):
        return "d", path[len(d_root):]
    if "/MEGA/" in path:
        return "mega", path.rsplit("/MEGA/", 1)[1]
    return None


def import_legacy(con, cfg: Config, state_files: list[Path] | None = None) -> dict[str, int]:
    """Import old no-gain, low-quality and error flags as rejected/error rows."""
    counts = {"imported": 0, "skipped": 0}
    if not (cfg.min_gain_ratio == 0.75 and cfg.min_ssim == 0.95):
        return counts  # the old verdicts were reached under other thresholds
    for state_file in DEFAULT_STATE_FILES if state_files is None else state_files:
        try:
            data = json.loads(Path(state_file).read_text())
        except FileNotFoundError:
            continue
        for path, flag in data.get("flags", {}).items():
            if _import_flag(con, cfg, path, flag):
                counts["imported"] += 1
            else:
                counts["skipped"] += 1
    return counts


def _import_flag(con, cfg: Config, path: str, flag: dict) -> bool:
    status = flag.get("status")
    if status not in ("no-gain", "low-quality", "error") or flag.get("settings") != OLD_SETTINGS:
        return False
    mapped = _map_path(path, cfg)
    if mapped is None:
        return False
    location, relpath = mapped
    kind = rules.kind_of(relpath)
    if kind is None:
        return False
    if con.execute("SELECT 1 FROM items WHERE location=? AND relpath=?",
                   (location, relpath)).fetchone():
        return False
    db.upsert_candidate(con, location=location, relpath=relpath, size=flag["size"],
                        mtime=flag["mtime"], kind=kind, source="library",
                        priority=PRIORITY[location], settings=cfg.settings)
    item_id = con.execute("SELECT id FROM items WHERE location=? AND relpath=?",
                          (location, relpath)).fetchone()["id"]
    if status == "error":
        db.set_status(con, item_id, "error", error="imported from compactor")
    else:
        db.set_status(con, item_id, "rejected", reason=status)
    return True
