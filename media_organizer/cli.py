"""Command line: run the service, show status, dry-run a scan, pause/resume, and housekeeping."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import db, scanner, stats
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .legacy import import_legacy
from .locations import Location, MacUnreachable, build_locations
from .probe import check_tools
from .service import SCANS, Service
from .web import serve_in_thread

GIB = 1024 ** 3
MIB = 1024 ** 2
SCAN_LABELS = {"d-intake": "D: intake", "d-full": "D: full", "mega-intake": "MEGA intake",
               "mega-full": "MEGA full", "win-downloads": "Win Downloads",
               "mac-downloads": "Mac Downloads"}
MAC_LOCATIONS = {"mega", "mac_dl"}


def _gb(n: float) -> str:
    return f"{n / GIB:.1f} GB"


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _scan_text(scan: dict, definition, cfg: Config) -> str:
    label = SCAN_LABELS.get(scan["name"], scan["name"])
    if scan["finished"] is None:
        return f"{label} never"
    if scan["ok"]:
        return f"{label} {_hhmm(scan['finished'])} ok"
    text = f"{label} {_hhmm(scan['finished'])} {scan['message'] or 'failed'}"
    if definition.location in MAC_LOCATIONS:
        text += f", retry {_hhmm(scan['finished'] + cfg.mac_retry_h * 3600)}"
    return text


def format_status(s: dict, cfg: Config) -> str:
    lines = []
    if s["paused"]:
        lines.append("paused")
    fp, freed, disk = s["first_pass"], s["freed"], s["disk"]
    lines.append(f"First pass   D: library   {fp['d']['done']:,} / {fp['d']['total']:,} items   "
                 f"freed {freed['d'] / GIB:5.1f} GB   D: free {disk['d_free'] / GIB:.0f} GB")
    lines.append(f"             MEGA          {fp['mega']['done']:,} / {fp['mega']['total']:,} "
                 f"files  freed {freed['mega'] / GIB:5.1f} GB")
    q = s["queue"]
    queue = (f"Queue        {q['downloads']:,} Downloads · {q['intake']:,} inbox · "
             f"{q['mega']:,} MEGA · {q['library']:,} library")
    if q["no_space"]:
        queue += f" · {q['no_space']:,} waiting for space"
    if q["evict_pending"]:
        queue += f" · {q['evict_pending']:,} to release on the Mac"
    lines.append(queue)
    now = s["now"]
    if now:
        pct = f"  {now.get('progress', 0) * 100:.0f}%" if "progress" in now else ""
        lines.append(f"Now          encoding \"{now.get('name', '?')}\" "
                     f"({_gb(now.get('size', 0))}){pct}")
    else:
        lines.append("Now          idle")
    scans = {d.name: d for d in SCANS}
    texts = [_scan_text(x, scans[x["name"]], cfg) for x in s["scans"]]
    lines.append("Last scans   " + " · ".join(texts))
    p = s["problems"]
    problems = (f"Problems     {p['error']} errors · {p['no-gain']} no-gain · "
                f"{p['low-quality']} low-quality · {p['changed']} changed · "
                f"{p['multi-frame']} multi-frame")
    if p["stuck_published"]:
        problems += f" · {p['stuck_published']} stuck (original not deleted)"
    lines.append(problems + "      (media-organizer status --errors)")
    return "\n".join(lines)


def _status(cfg: Config, args) -> int:
    con = db.open_db(cfg.db_path)
    try:
        s = stats.summary(con, cfg)
    finally:
        con.close()
    if args.json:
        print(json.dumps(s, indent=2))
    elif args.errors:
        items = s["problems"]["items"]
        for item in items:
            print(f"{item['location']} {item['relpath']}: {item['error'] or item['reason'] or ''}")
        if not items:
            print("no errors")
    else:
        print(format_status(s, cfg))
    return 0


def _meta_command(cfg: Config, key: str, value: str) -> int:
    con = db.open_db(cfg.db_path)
    try:
        db.meta_set(con, key, value)
    finally:
        con.close()
    print("paused" if value == "1" else "resumed")
    return 0


def _with_db(cfg: Config, fn):
    con = db.open_db(cfg.db_path)
    try:
        return fn(con)
    finally:
        con.close()


def _already_up(loc: Location) -> bool:
    """Reachable without mounting anything (Location.reachable() would run sshfs)."""
    session = getattr(loc, "session", None)
    if session is None:
        return loc.reachable()
    return session._is_mounted() and session._mount_alive()


def _dry_run(cfg: Config) -> int:
    locs = build_locations(cfg)
    con = db.open_db(Path(":memory:"))  # a throwaway copy of the schema; nothing touches disk
    try:
        for name in ("d", "mega", "win_dl", "mac_dl"):
            loc = locs[name]
            if not loc.is_mac and not loc.root.exists():
                print(f"SKIP {name} (missing)")
                continue
            if not _already_up(loc):
                print(f"SKIP {name} (unreachable)")
                continue
            try:
                if name in ("d", "mega"):
                    res = scanner.scan_library(con, loc, cfg, full=True, dry_run=True)
                else:
                    res = scanner.scan_downloads(con, loc, locs, cfg, dry_run=True)
            except MacUnreachable:
                print(f"SKIP {name} (unreachable)")
                continue
            queued = con.execute("SELECT relpath, size FROM items WHERE location=? "
                                 "AND status='queued' ORDER BY priority, size DESC, relpath",
                                 (name,)).fetchall()
            for row in queued:
                print(f"WOULD QUEUE {name} {row['relpath']} {row['size'] / MIB:.1f} MB")
            for source, target in res.would_move:
                print(f"WOULD MOVE {name} {source} -> {target}")
            extra = ""
            if name in ("win_dl", "mac_dl"):
                extra = f", {len(res.would_delete)} .nfo, {len(res.would_remove)} folders"
            for relpath in res.would_delete:
                print(f"WOULD DELETE {name} {relpath}")
            for relpath in res.would_remove:
                print(f"WOULD REMOVE FOLDER {name} {relpath}")
            print(f"{name}: {len(queued)} would queue, {len(res.would_move)} would move{extra}")
    finally:
        con.close()
    return 0


def _run(cfg: Config, args) -> int:
    missing = check_tools()
    if missing:
        print("missing tools: " + ", ".join(missing))
        return 1
    server = None if args.no_web else serve_in_thread(cfg)
    try:
        Service(cfg).run_forever()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-organizer")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="path to the TOML config file")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the service")
    run.add_argument("--no-web", action="store_true", help="do not start the dashboard")
    status = sub.add_parser("status", help="show progress")
    status.add_argument("--errors", action="store_true", help="list items in error")
    status.add_argument("--json", action="store_true", help="dump the summary as JSON")
    scan = sub.add_parser("scan", help="scan (only --dry-run is available from the command line)")
    scan.add_argument("--dry-run", action="store_true", help="report what would be queued or moved")
    sub.add_parser("pause", help="pause encoding")
    sub.add_parser("resume", help="resume encoding")
    sub.add_parser("retry-errors", help="requeue every item in error")
    sub.add_parser("import-legacy", help="import flags from the old compactor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "run":
        return _run(cfg, args)
    if args.command == "status":
        return _status(cfg, args)
    if args.command == "scan":
        if not args.dry_run:
            print("scan only works as --dry-run here; the running service does the real scans")
            return 2
        return _dry_run(cfg)
    if args.command == "pause":
        return _meta_command(cfg, "paused", "1")
    if args.command == "resume":
        return _meta_command(cfg, "paused", "0")
    if args.command == "retry-errors":
        print(_with_db(cfg, db.retry_errors))
        return 0
    counts = _with_db(cfg, lambda con: import_legacy(con, cfg))
    print(f"imported {counts['imported']}, skipped {counts['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
