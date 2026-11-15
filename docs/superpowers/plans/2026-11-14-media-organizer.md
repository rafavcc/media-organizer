# Media Organizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One always-on service on the gamer PC (WSL) that compresses every media file in two libraries, keeps compressing new arrivals, sweeps two Downloads folders into the libraries, and reports through a web dashboard and a `status` command.

**Architecture:** Scanners list files and put work items in a SQLite table. Worker threads take the highest-priority item, copy it to a local work folder, encode and verify it with the recipes ported from `app_compactor/compactor.py`, publish the result next to the original and delete the original. The Mac is reached through one sshfs mount of its home folder plus plain `ssh` commands. A small HTTP server in the same process serves a read-only dashboard from the database.

**Tech Stack:** Python 3.12 standard library only (`sqlite3`, `tomllib`, `http.server`, `unittest`, `threading`, `subprocess`). External programs already installed: `ffmpeg` with `libx265`, `ffprobe`, `cwebp`, `gif2webp`, `webpmux`, ImageMagick `convert`, `sshfs`, `ssh`. systemd user service.

**Spec:** `docs/superpowers/specs/2026-11-14-media-organizer-design.md`

**Reference code (read-only, do not modify):** `../app_compactor/compactor.py`, `../app_compactor/compactor_mac.py`, `../app_compactor/pipeline.py`, `../fileSorting/fileSortingv6.py`.

## Global Constraints

- Standard library only. No `pip install`. Tests use `unittest`; run all with `python3 -m unittest discover -s tests -v` from the repo root.
- Never run any code or test against `/mnt/d/7_XYZ`, `/mnt/c/Users/rafavcc/Downloads`, the Mac, or the real `~/.local/share/media-organizer`. Tests use `tempfile.TemporaryDirectory()` only. Tiny test media is generated with ffmpeg (`testsrc`, 2 seconds, 320x240).
- An original is deleted only after its replacement exists at the final path with the expected byte size and the original's size and mtime are unchanged since it was read.
- Nothing is ever written into a library except `<final name>.mo-part` (renamed to the final name when complete) and the final file.
- Converted files are named `<stem>_compressed<ext>`; any file whose stem ends in `_compressed` (case-insensitive) is never processed.
- Video: `libx265`, preset `fast`, CRF `30`, `yuv420p10le`, `-fps_mode cfr`, `-tag:v hvc1`, `-movflags +faststart`, container mp4; audio copied if every audio codec is in `{aac, mp3, ac3, eac3, alac}`, else `aac 192k`. Result must be ≤ 75% of the original size, same width and height, same audio stream count, duration within 1.0 s.
- Images: JPG/JPEG `cwebp -q 85`; PNG/TIFF/BMP/HEIC/HEIF `cwebp -q 90`; GIF `gif2webp -lossy -q 75`. Result must be ≤ 75% of the original, same dimensions (GIF: same frame count), SSIM ≥ 0.95 (not checked for GIF).
- Video extensions: `.mp4 .mov .mkv .avi .wmv .webm .m4v`. Image extensions: `.jpg .jpeg .png .gif .heic .heif .tif .tiff .bmp`. `.webp` is media only for Downloads routing (moved, never converted).
- HEVC is skipped unless bits per pixel per frame > `0.07`. AV1 is always skipped.
- Downloads: top level of the folder only (never recurse). Video with size > 400 MB (`400 * 1024 * 1024` bytes, measured before compression) goes to `D:\7_XYZ\_TO_CONVERT`; other videos and all images go to `MEGA/TBC`.
- Files modified less than 10 minutes ago, zero-byte files, dotfiles, and files ending `.part .crdownload .download .mo-part` are ignored.
- Location names are exactly `"d"`, `"mega"`, `"win_dl"`, `"mac_dl"`. Database paths are stored as `(location, relpath)` with `/` separators, relative to the location root.
- Priorities: Downloads `0`, intake (`_TO_CONVERT`, `MEGA/TBC`) `1`, MEGA library `2`, D: library `3`. Within a priority, largest file first.
- If the Mac is unreachable, items that need it stay `queued`, nothing is flagged and nothing is deleted.
- Dashboard: read-only, HTTP Basic auth with the configured password, refuses to start with an empty password.

## Review Focus

1. **D: nearly full (26 GB free).** A big Downloads video must not be copied to `_TO_CONVERT`, and a compressed file must not be published, unless the destination has at least `size + 2 GB` free; otherwise the item waits and is reported. (Tasks 5, 8, 9)
2. **Mac drops off mid-file.** Fetch, publish or delete raising a disconnect must leave the item `queued` with the original intact and no `.mo-part` treated as a result. (Tasks 5, 8)
3. **Crash between publish and delete.** After a restart, a `published` item is finalized exactly once; a `running` item is re-queued; no second `_compressed` copy is produced. (Tasks 3, 8)
4. **File moved by hand or by the sorter.** A rejected file that reappears under another folder with the same name, size and mtime is recognised and not re-encoded. (Task 7)
5. **Awkward names.** Names with spaces, quotes, `[]`, `'`, non-ASCII and leading `-` must survive every `ffmpeg`, `cwebp` and `ssh` call. (Tasks 4, 5)

## File Structure

```
media_organizer/
  pyproject.toml              console script `media-organizer`
  README.md                   install, operate, recover
  deploy/media-organizer.service
  deploy/config.example.toml
  media_organizer/
    __init__.py
    config.py                 Config dataclass, TOML loading
    db.py                     schema and every SQL statement
    rules.py                  pure decisions: kinds, eligibility, routing, names
    probe.py                  ffprobe / webpmux / SSIM wrappers
    encode.py                 command lines and running them
    convert.py                encode + verify one local file
    locations.py              LocalLocation, MacSession, MacLocation
    scanner.py                library scans, Downloads sweeps, folder stats
    worker.py                 process one item, finalize, intake post-step
    service.py                scheduler loop, worker threads, sorter call
    legacy.py                 import of app_compactor state files
    stats.py                  numbers for status and dashboard
    web.py                    HTTP server, auth, JSON
    dashboard.html            the page
    cli.py                    argument parsing, commands
  tests/
    helpers.py                make_video(), make_image(), temp config
    test_*.py                 one per module
```

---

### Task 1: Project skeleton and config

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `media_organizer/__init__.py`, `media_organizer/config.py`, `deploy/config.example.toml`, `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Config:
    d_root: Path = Path("/mnt/d/7_XYZ")
    d_intake: str = "_TO_CONVERT"
    d_converted: str = "_CONVERTED"
    win_downloads: Path = Path("/mnt/c/Users/rafavcc/Downloads")
    mac_host: str = "mac"                      # ssh config alias
    mac_mega: str = "Library/Mobile Documents/com~apple~CloudDocs/MEGA"   # relative to Mac home
    mac_downloads: str = "Downloads"           # relative to Mac home
    mega_intake: str = "TBC"
    sorter: Path = Path("/mnt/c/Users/rafavcc/iCloudDrive/Programming/Projects/fileSorting/fileSortingv6.py")
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
    hevc_bloat_bpp: float = 0.07
    min_free_gb: float = 2.0
    web_port: int = 8002
    web_password: str = ""
    # derived
    @property
    def db_path(self) -> Path          # state_dir / "organizer.db"
    @property
    def mac_mount(self) -> Path        # state_dir / "mac"
    @property
    def work_dir(self) -> Path         # state_dir / "work"
    @property
    def log_path(self) -> Path         # state_dir / "organizer.log"
    @property
    def settings(self) -> str          # f"r{gain*100:.0f}s{ssim*100:.0f}c{crf}b{bpp*1000:.0f}" -> "r75s95c30b70"

DEFAULT_CONFIG_PATH = Path.home() / ".config/media-organizer/config.toml"
def load_config(path: Path | None = None) -> Config
```

- [ ] **Step 1: Write failing tests** in `tests/test_config.py`:
  - `test_defaults`: `Config().settings == "r75s95c30b70"`, `Config().size_threshold_mb == 400`, `Config().db_path.name == "organizer.db"`.
  - `test_load_overrides`: a TOML file with `size_threshold_mb = 300` and `d_root = "/tmp/x"` gives `cfg.size_threshold_mb == 300` and `cfg.d_root == Path("/tmp/x")` (path fields are converted to `Path`, `~` expanded).
  - `test_unknown_key_rejected`: `foo = 1` raises `ValueError` whose message contains `foo`.
  - `test_missing_file_gives_defaults`: `load_config(Path("/nonexistent/x.toml")) == Config()`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_config -v`. Expected: import error.
- [ ] **Step 3: Implement** `config.py` (flat TOML keys, `tomllib`), `pyproject.toml` (`[project] name = "media-organizer"`, `requires-python = ">=3.11"`, `[project.scripts] media-organizer = "media_organizer.cli:main"`, setuptools backend, `package-data` including `dashboard.html`), `.gitignore` (`__pycache__/`, `*.egg-info/`, `.venv/`), `deploy/config.example.toml` with every key commented at its default and `web_password = "change-me"`.
- [ ] **Step 4: Run** the tests. Expected: 4 pass.
- [ ] **Step 5: Commit** `git init` (if needed), `git add -A`, `git commit -m "feat: project skeleton and config"`.

---

### Task 2: Rules (pure decisions)

**Files:**
- Create: `media_organizer/rules.py`, `tests/test_rules.py`

**Interfaces:**
- Consumes: `Config`.
- Produces:

```python
VIDEO_EXT: frozenset[str]; IMAGE_EXT: frozenset[str]; PARTIAL_EXT: frozenset[str]
SUFFIX = "_compressed"
MP4_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})

def kind_of(name: str) -> str | None              # "video" | "image" | None, by extension, case-insensitive
def is_compressed_name(name: str) -> bool         # stem ends with _compressed, case-insensitive
def is_ignored(name: str) -> bool                 # dotfile or PARTIAL_EXT
def is_candidate(name: str) -> bool               # kind_of and not compressed and not ignored
def out_ext(name: str) -> str                     # ".mp4" for video, ".webp" for image
def bits_per_pixel(bitrate: int | None, width: int, height: int, fps: float) -> float | None
def video_decision(codec: str, bpp: float | None, cfg: Config) -> str   # "encode" | "efficient"
def route_download(name: str, size: int, cfg: Config) -> str | None     # "d" | "mega" | None
def final_name(name: str, ext: str, taken: Callable[[str], bool]) -> str
def free_name(name: str, taken: Callable[[str], bool]) -> str
```

- [ ] **Step 1: Write failing tests** in `tests/test_rules.py`:
  - `kind_of("A.MKV") == "video"`, `kind_of("a.HEIC") == "image"`, `kind_of("a.webp") is None`, `kind_of("a.txt") is None`.
  - `is_candidate("x_compressed.mp4") is False`, `is_candidate("x_COMPRESSED.mp4") is False`, `is_candidate(".x.mp4") is False`, `is_candidate("x.mp4.part") is False`, `is_candidate("x.mp4") is True`.
  - `video_decision("h264", None, cfg) == "encode"`; `("av1", 9.0)` → `"efficient"`; `("hevc", 0.05)` → `"efficient"`; `("hevc", 0.071)` → `"encode"`; `("hevc", None)` → `"efficient"`.
  - `bits_per_pixel(8_000_000, 1920, 1080, 30.0)` ≈ `0.1286` (3 decimals); returns `None` if bitrate is `None` or any of width/height/fps is 0.
  - `route_download("a.mp4", 400*1024*1024 + 1, cfg) == "d"`; exactly `400*1024*1024` → `"mega"`; `"a.png"` of 900 MB → `"mega"`; `"a.webp"` → `"mega"`; `"a.pdf"` → `None`; `"a.mp4.crdownload"` → `None`; `".a.mp4"` → `None`.
  - `final_name("clip.mov", ".mp4", lambda n: False) == "clip_compressed.mp4"`; with `clip_compressed.mp4` taken → `"clip_mov_compressed.mp4"`; with both taken → `"clip_mov2_compressed.mp4"` (same scheme as `compactor.reserve_final`).
  - `free_name("a.mp4", taken={"a.mp4", "a (1).mp4"}) == "a (2).mp4"`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_rules -v`. Expected: fail on import.
- [ ] **Step 3: Implement** `rules.py`. No I/O in this module.
- [ ] **Step 4: Run.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: eligibility, routing and naming rules`.

---

### Task 3: Database

**Files:**
- Create: `media_organizer/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces (all functions take an open `sqlite3.Connection` with `row_factory = sqlite3.Row`; each commits before returning):

```python
def open_db(path: Path) -> sqlite3.Connection      # creates parent dir and schema; WAL; check_same_thread=False; busy_timeout 30 s

# items.status: queued | running | published | done | skipped | rejected | error
# items.source: library | intake | downloads
def upsert_candidate(con, *, location, relpath, size, mtime, kind, source, priority, settings,
                     dest_location=None, dest_dir=None) -> str
    # returns "queued" (new row), "known" (row matches size+mtime, and for skipped/rejected also settings),
    # "requeued" (file or settings changed: row reset to queued, attempts kept)
    # error rows with unchanged size+mtime are "known" (only retry_errors re-queues them)
def adopt_moved(con, *, location, relpath, size, mtime, exists: Callable[[str], bool]) -> bool
    # if a terminal row (done/skipped/rejected/error) in the same location has the same basename, size and mtime
    # and exists(row.relpath) is False, set its relpath to the new one and return True
def claim_next(con, *, kind: str, exclude_locations: Collection[str]) -> sqlite3.Row | None
    # oldest by (priority ASC, size DESC, id ASC) with status queued and kind match, where neither location
    # nor dest_location is excluded; atomically set running, attempts+1, started_at
def set_status(con, item_id: int, status: str, **fields) -> None   # fields are column names
def due_published(con, now: float) -> list[sqlite3.Row]            # status published and not_before <= now
def reset_running(con) -> int                                      # running -> queued, returns count
def retry_errors(con) -> int                                       # error -> queued
def delete_item(con, item_id: int) -> None
def prune_missing(con, *, location: str, under: str, seen: set[str]) -> int
    # delete rows of location whose relpath starts with under and is not in seen, unless status is running or published
def log_conversion(con, *, ts, location, relpath, kind, bytes_before, bytes_after) -> None
def meta_get(con, key: str, default: str | None = None) -> str | None
def meta_set(con, key: str, value: str) -> None
def record_scan(con, *, name: str, started: float, finished: float, ok: bool, message: str) -> None
def last_scan(con, name: str) -> sqlite3.Row | None                # most recent row for that scan name
def add_snapshot(con, *, ts, location, total_bytes, free_bytes) -> None
def replace_folders(con, location: str, rows: list[tuple[str, int, int, int]]) -> None  # (folder, bytes, files, compressed_files)
```

- Schema:

```sql
CREATE TABLE items(
  id INTEGER PRIMARY KEY, location TEXT NOT NULL, relpath TEXT NOT NULL,
  size INTEGER NOT NULL, mtime REAL NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', reason TEXT, settings TEXT,
  dest_location TEXT, dest_dir TEXT, final_relpath TEXT, final_size INTEGER,
  was_online_only INTEGER NOT NULL DEFAULT 0, not_before REAL, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, started_at REAL, finished_at REAL, UNIQUE(location, relpath));
CREATE INDEX items_queue ON items(status, kind, priority, size DESC);
CREATE TABLE conversions(id INTEGER PRIMARY KEY, ts REAL NOT NULL, location TEXT NOT NULL,
  relpath TEXT NOT NULL, kind TEXT NOT NULL, bytes_before INTEGER NOT NULL, bytes_after INTEGER NOT NULL);
CREATE TABLE scans(id INTEGER PRIMARY KEY, name TEXT NOT NULL, started REAL, finished REAL, ok INTEGER, message TEXT);
CREATE TABLE snapshots(ts REAL NOT NULL, location TEXT NOT NULL, total_bytes INTEGER, free_bytes INTEGER);
CREATE TABLE folders(location TEXT NOT NULL, folder TEXT NOT NULL, bytes INTEGER, files INTEGER, compressed_files INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
```

- [ ] **Step 1: Write failing tests** in `tests/test_db.py` (temp-dir database):
  - `upsert_candidate` returns `"queued"` then `"known"` for the same arguments.
  - A `rejected` row with the same size, mtime and settings → `"known"`; with a different `settings` → `"requeued"` and status `queued`; with a different size → `"requeued"`.
  - An `error` row with unchanged size and mtime → `"known"`; `retry_errors` returns 1 and the row is `queued`.
  - `claim_next` order: insert priority 3 size 10, priority 0 size 1, priority 3 size 99 → claimed in order p0, p3/99, p3/10; second call for the same item never returns it; `kind="image"` never returns a video.
  - `claim_next(exclude_locations={"mega"})` skips an item whose `location` is `"mega"` and an item whose `dest_location` is `"mega"`.
  - `reset_running` moves a claimed item back to `queued`.
  - `due_published`: `not_before=100` is returned for `now=100`, not for `now=99`.
  - `adopt_moved`: a `rejected` row `a/x.mp4` (size 5, mtime 7) with `exists` returning False is re-pointed to `b/x.mp4` and returns True; returns False when `exists` is True, or size differs.
  - `prune_missing(location="d", under="", seen={"keep.mp4"})` deletes a `done` row `gone.mp4`, keeps `keep.mp4`, keeps a `published` row not in `seen`.
  - Review Focus 3: after `claim_next` then `set_status(id, "published", final_relpath="x_compressed.mp4", final_size=3, not_before=0)`, closing and reopening the database and calling `reset_running` leaves the row `published` (count 0).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_db -v`. Expected: fail on import.
- [ ] **Step 3: Implement** `db.py`. `claim_next` uses `BEGIN IMMEDIATE`. `set_status` rejects unknown column names with `ValueError`.
- [ ] **Step 4: Run.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: sqlite work queue and state`.

---

### Task 4: Probe, encode, convert

**Files:**
- Create: `media_organizer/probe.py`, `media_organizer/encode.py`, `media_organizer/convert.py`, `tests/helpers.py`, `tests/test_convert.py`

**Interfaces:**
- Consumes: `Config`, `rules`.
- Produces:

```python
# probe.py
@dataclass(frozen=True)
class VideoInfo:
    codec: str; width: int; height: int; fps: str; fps_value: float
    duration: float; audio: int; acodecs: tuple[str, ...]; bitrate: int | None
def probe_video(p: Path) -> VideoInfo | None   # bitrate: video stream bit_rate, else format bit_rate, else None
def dims(p: Path) -> str
def gif_frames(p: Path) -> str
def webp_frames(p: Path) -> str
def ssim(ref: Path, other: Path) -> float | None
def check_tools() -> list[str]                 # names of missing tools; [] when all present

# encode.py
def video_cmd(src: Path, out: Path, info: VideoInfo, cfg: Config) -> list[str]
def image_cmd(ext: str, src: Path, out: Path) -> list[str]
def run_video(src: Path, out: Path, info: VideoInfo, cfg: Config,
              on_progress: Callable[[float], None] | None = None) -> tuple[int, str]   # (returncode, stderr tail)

# convert.py
@dataclass(frozen=True)
class Outcome:
    status: str          # "ok" | "efficient" | "no-gain" | "low-quality" | "error"
    message: str
    out: Path | None     # set only when status == "ok"
def convert(src: Path, name: str, orig_size: int, work: Path, cfg: Config,
            on_progress: Callable[[float], None] | None = None) -> Outcome

# tests/helpers.py
def make_video(path: Path, *, codec="libx264", seconds=2, size="320x240", audio=True, extra: list[str] = ()) -> Path
def make_image(path: Path, *, size="640x480") -> Path       # ffmpeg testsrc single frame; extension picks the format
def make_gif(path: Path) -> Path
def test_config(tmp: Path, **overrides) -> Config            # every path field under tmp
```

- `video_cmd` must equal the command built by `compactor.encode_video` (see `../app_compactor/compactor.py:192-198`) with these differences only: CRF and preset come from `cfg`; `-x265-params pools=<cfg.x265_pools>` is added; `-progress pipe:1 -nostats` is added; every path argument that could start with `-` is passed as an absolute path. `image_cmd` equals `compactor.RECIPES[ext] + [src, "-o", out]`; for `.heic .heif .tif .tiff .bmp` `convert()` first makes a PNG with ImageMagick as `compactor.process` does.
- `convert` never touches `src`; it writes only inside `work`. It calls `rules.video_decision` and returns `efficient` without encoding when told to. Progress is `out_time / duration` clamped to `[0, 1]`.

- [ ] **Step 1: Write failing tests** in `tests/test_convert.py`:
  - `test_video_cmd_matches_recipe`: for a fake `VideoInfo(fps="30/1", acodecs=("aac",))` the command contains, in order, `-c:v libx265 -preset fast -crf 30 -pix_fmt yuv420p10le`, `-r 30/1 -fps_mode cfr -tag:v hvc1`, `-c:a copy`, `-movflags +faststart`, and `pools=10`; with `acodecs=("pcm_s16le",)` it contains `-c:a aac -b:a 192k`.
  - `test_image_cmd`: `.jpg` → contains `cwebp` and `-q 85`; `.png` → `-q 90`; `.gif` → `gif2webp`, `-lossy`, `-q 75`.
  - `test_h264_is_converted`: `make_video` with high bitrate (`extra=["-b:v","4M"]`) → `convert(...).status == "ok"`, output probes as `hevc`, same width and height, output size ≤ 0.75 × original.
  - `test_hevc_low_bitrate_is_efficient`: a libx265 CRF 35 clip → `status == "efficient"`, no file left in `work`.
  - `test_hevc_bloated_is_encoded`: a libx265 clip with `-b:v 6M` at 320x240 → status is `"ok"` or `"no-gain"` (never `"efficient"`).
  - `test_no_gain`: with `cfg.min_gain_ratio = 0.01` an h264 clip → `"no-gain"` and `out is None`.
  - `test_unreadable_video`: a text file named `x.mp4` → `"error"`.
  - `test_png_to_webp`: `make_image("a.png")` → `"ok"`, output dims equal source dims.
  - `test_gif_keeps_frames`: `make_gif` → `"ok"` and `webp_frames(out) == gif_frames(src)`.
  - `test_mkv_avi_inputs`: `make_video("a.mkv")` and `make_video("a.avi", codec="mpeg4")` → `"ok"`, output is mp4/hevc.
  - Review Focus 5: `test_awkward_names`: source named `-we'ird [x] "q" ñ.mp4` in a folder with a space converts with status `"ok"`.
  - `test_progress_callback`: callback receives at least one value in `(0, 1]`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_convert -v`. Expected: fail on import.
- [ ] **Step 3: Implement** the three modules, porting from `compactor.py` (`probe_video`, `dims`, `gif_frames`, `webp_frames`, `ssim`, `RECIPES`, `encode_video`, and the verification order in `process` / `process_video`).
- [ ] **Step 4: Run.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: probe, encode and verified conversion`.

---

### Task 5: Locations (local disk and Mac)

**Files:**
- Create: `media_organizer/locations.py`, `tests/test_locations.py`

**Interfaces:**
- Consumes: `Config`.
- Produces:

```python
class MacUnreachable(Exception): ...
class NoSpace(Exception): ...

@dataclass(frozen=True)
class Entry:
    relpath: str; size: int; mtime: float

class Location:
    name: str; root: Path; is_mac: bool
    def now(self) -> float
    def reachable(self) -> bool
    def walk(self, sub: str = "", recursive: bool = True) -> Iterator[Entry]   # files only; skips dot dirs and dotfiles
    def path(self, relpath: str) -> Path
    def exists(self, relpath: str) -> bool
    def stat(self, relpath: str) -> Entry | None
    def fetch(self, relpath: str, dest: Path) -> None          # copy to local dest; verifies byte size; 3 tries
    def publish(self, src: Path, relpath: str, mtime: float | None, min_free: int) -> int
        # raise NoSpace unless free_bytes() >= size(src) + min_free; write relpath + ".mo-part", set mtime,
        # rename to relpath, verify size (delete and raise OSError on mismatch); creates parent dirs; returns size
    def delete(self, relpath: str) -> None
    def move_within(self, relpath: str, new_relpath: str) -> None    # os.replace, creates parent dirs
    def free_bytes(self) -> int
    def is_online_only(self, relpath: str) -> bool             # local: always False
    def evict(self, relpath: str) -> bool                      # local: no-op, returns False

class LocalLocation(Location): __init__(self, name: str, root: Path)

class MacSession:
    def __init__(self, host: str, mount: Path, runner: Callable = subprocess.run)
    def ssh(self, command: str, timeout: float = 30) -> subprocess.CompletedProcess   # BatchMode, ConnectTimeout=8, ControlMaster as compactor_mac.SSH_OPTS
    def reachable(self) -> bool         # `ssh host true` ok, and the sshfs mount of `host:` at `mount` is up (mounts it if not)
    def unmount(self) -> None
    def now(self) -> float              # time.time() + offset from `ssh host date +%s`, measured once per successful reachable()

class MacLocation(LocalLocation):
    def __init__(self, name: str, session: MacSession, home_rel: str)   # root = session.mount / home_rel
    # every filesystem method converts OSError with errno in compactor_mac.DISCONNECT_ERRNOS (or a dead mount) to MacUnreachable
    # fetch(): first `brctl download <quoted remote path>` (best effort, timeout 300 s)
    # is_online_only(): `stat -f %f <path>` on the Mac, True when flags & 0x40000000 (SF_DATALESS)
    # evict(): `brctl evict <path>`, True on exit status 0

def build_locations(cfg: Config) -> dict[str, Location]
    # "d": LocalLocation(cfg.d_root); "win_dl": LocalLocation(cfg.win_downloads);
    # "mega": MacLocation(session, cfg.mac_mega); "mac_dl": MacLocation(session, cfg.mac_downloads) sharing one MacSession
```

- sshfs mount command: `sshfs <host>: <mount> -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3`. Remote paths in ssh commands are built from the Mac home-relative path and quoted with `shlex.quote`.

- [ ] **Step 1: Write failing tests** in `tests/test_locations.py`:
  - `LocalLocation.walk` returns `/`-separated relpaths, skips `.hidden/x.mp4` and `.x.mp4`, and with `recursive=False` returns only top-level files.
  - `publish` writes the file, leaves no `.mo-part`, sets mtime to the given value, returns the size, and creates missing parent folders.
  - `publish` with `min_free` larger than the free space raises `NoSpace` and writes nothing (Review Focus 1; patch `free_bytes`).
  - `fetch` copies bytes and raises `OSError` when the copied size differs from the source size (patch `shutil.copyfile` to truncate).
  - `MacSession.reachable()` is False when the injected runner returns exit status 255 for `ssh … true`; no mount attempt is made.
  - `MacLocation.is_online_only` is True when the fake runner prints `1073741888` (0x40000040) and False for `64`.
  - `MacLocation.fetch` calls the runner with a command containing `brctl download` and the quoted path for a file named `it's [a] file.mp4` (Review Focus 5).
  - Review Focus 2: a `MacLocation` over a temp folder whose `shutil.copyfile` is patched to raise `OSError(errno.ENOTCONN)` raises `MacUnreachable` from `fetch` and from `publish`, and no final file exists afterwards.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_locations -v`. Expected: fail on import.
- [ ] **Step 3: Implement** `locations.py`, porting the disconnect handling and clock offset from `compactor_mac.py`.
- [ ] **Step 4: Run.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: local and Mac locations`.

---

### Task 6: Legacy state import

**Files:**
- Create: `media_organizer/legacy.py`, `tests/test_legacy.py`

**Interfaces:**
- Consumes: `db.upsert_candidate`, `db.set_status`, `Config`, `rules.kind_of`.
- Produces: `def import_legacy(con, cfg: Config, state_files: list[Path] | None = None) -> dict[str, int]` returning counts `{"imported": n, "skipped": m}`. Default files: `~/.local/share/compactor/state.json` and `state-mac.json`.

- Mapping: a flag path under `cfg.d_root` → `("d", relative path)`. A path containing `/MEGA/` → `("mega", text after the last "/MEGA/")`. Anything else is skipped. Only statuses `no-gain`, `low-quality`, `error` with old `settings == "r75s95"` are imported, and only when `cfg.min_gain_ratio == 0.75 and cfg.min_ssim == 0.95`. `no-gain` and `low-quality` become status `rejected` with that reason; `error` becomes `error`. `efficient` flags are not imported (HEVC is re-checked for bloat). Rows get `source="library"`, the location's library priority, `settings=cfg.settings`, and the size and mtime from the flag. Existing rows are left untouched. Running it twice imports nothing the second time.

- [ ] **Step 1: Write failing tests**: a state file with one flag of each status under a temp `d_root`, one under `/home/u/icloud-mac/MEGA/big/a.jpg`, one under `/somewhere/else/b.jpg` → `imported == 3` for the three importable ones (no-gain d, error d or as constructed, no-gain mega), the mega row has `relpath == "big/a.jpg"`, the `efficient` one and the unrelated one are skipped; second run returns `imported == 0`; a missing state file is not an error.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_legacy -v`. Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat: import app_compactor state`.

---

### Task 7: Scanners

**Files:**
- Create: `media_organizer/scanner.py`, `tests/test_scanner.py`

**Interfaces:**
- Consumes: `db.*`, `Location`, `rules.*`, `probe.probe_video`, `Config`.
- Produces:

```python
@dataclass
class ScanResult:
    found: int = 0; queued: int = 0; known: int = 0; adopted: int = 0; moved: int = 0; waiting: int = 0

PRIORITY = {"downloads": 0, "intake": 1, "mega": 2, "d": 3}

def scan_library(con, loc: Location, cfg: Config, *, sub: str = "", full: bool) -> ScanResult
def scan_downloads(con, loc: Location, locs: dict[str, Location], cfg: Config) -> ScanResult
def refresh_folders(con, loc: Location) -> None
```

- `scan_library`: walks `loc` under `sub` recursively. For each `rules.is_candidate` file older than `cfg.min_age_min` (by `loc.now()`) and non-empty: source is `"intake"` when the relpath is under the location's intake folder (`cfg.d_intake` for `"d"`, `cfg.mega_intake` for `"mega"`), else `"library"`; priority `1` for intake, else `PRIORITY[loc.name]`. Call `db.adopt_moved` first for paths with no row; otherwise `db.upsert_candidate`. For a newly queued video on a non-Mac location, probe it now and, when `rules.video_decision` says `efficient`, set status `skipped`, reason `efficient`. When `full` is True, afterwards call `db.prune_missing(location, under=sub, seen=…)`, delete `*.mo-part` files older than 24 hours, and call `refresh_folders`.
- `scan_downloads`: top level only (`recursive=False`). For each file where `rules.route_download` is not `None` and the age rule passes:
  - route `"d"` → copy straight to `locs["d"]` at `<cfg.d_intake>/<free_name>` with `publish(min_free=cfg.min_free_gb GiB)`, then delete the source; count `moved`. On `NoSpace` count `waiting` and leave the file.
  - route `"mega"` → `db.upsert_candidate(source="downloads", priority=0, dest_location="mega", dest_dir=cfg.mega_intake, kind=…)`; `.webp` files use `kind="image"` and are still queued (the worker moves them unconverted).
- `refresh_folders`: one row per first-level folder under the root (files directly in the root count under `"."`): total bytes, file count, and count of media files that are finished, where finished means `rules.is_compressed_name` or a row with status `done`, `skipped` or `rejected`. Stores with `db.replace_folders` and adds a `db.add_snapshot(location, total_bytes=sum, free_bytes=loc.free_bytes())`.

- [ ] **Step 1: Write failing tests** (temp folders, `LocalLocation`, files given old mtimes with `os.utime`):
  - Library scan of `a/x.mp4` (h264 via `make_video`), `a/y_compressed.mp4`, `a/z.txt`, `.hid/w.mp4`, and a fresh `a/new.mp4` (mtime now) → exactly one row, `x.mp4`, `queued`, source `library`, priority 3.
  - An HEVC low-bitrate file is recorded as `skipped` / `efficient` at scan time.
  - A file under `_TO_CONVERT` gets source `intake`, priority 1.
  - Second identical scan → `queued == 0`, `known == 1`.
  - Review Focus 4: set the row to `rejected`, move the file to `b/x.mp4` keeping mtime, run a full scan → still one row, relpath `b/x.mp4`, status `rejected`, `adopted == 1`.
  - Full scan removes a `done` row whose file is gone and deletes a 2-day-old `stale.mp4.mo-part`, but keeps a 1-hour-old one.
  - Downloads: `big.mp4` (sparse file of 400 MiB + 1 byte made with `truncate`), `small.mp4`, `pic.png`, `pic.webp`, `doc.pdf`, `dl.mp4.crdownload`, and `sub/inner.png` → `big.mp4` is now at `<d_root>/_TO_CONVERT/big.mp4` and gone from Downloads; rows exist for `small.mp4`, `pic.png`, `pic.webp` with `dest_location == "mega"`, `dest_dir == "TBC"`, priority 0; nothing else has a row; `sub/inner.png` untouched.
  - Downloads name collision: `_TO_CONVERT/big.mp4` already exists → the new one lands as `big (1).mp4`.
  - Review Focus 1: with `locs["d"].free_bytes` patched to 1 GiB, `big.mp4` stays in Downloads and `waiting == 1`.
  - `refresh_folders`: folder `A` with one `_compressed` file and one plain file → row `("A", total bytes, 2, 1)`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_scanner -v`. Expected: fail.
- [ ] **Step 3: Implement** `scanner.py`.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat: library scans and Downloads sweep`.

---

### Task 8: Worker

**Files:**
- Create: `media_organizer/worker.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `db.*`, `Location`, `MacUnreachable`, `NoSpace`, `convert.convert`, `rules.*`, `Config`.
- Produces:

```python
def process_item(con, item: sqlite3.Row, locs: dict[str, Location], cfg: Config, work: Path,
                 on_progress: Callable[[float], None] | None = None) -> str     # resulting status
def finalize_published(con, locs: dict[str, Location], cfg: Config) -> int       # originals deleted
```

- `process_item` for an item with no `dest_location` (library or intake):
  1. `loc.stat(relpath)`: missing → `db.delete_item`, return `"gone"`. Size or mtime differs from the row → update the row, set `queued`, return `"queued"`.
  2. Record `was_online_only = loc.is_online_only(relpath)`, then `loc.fetch` into `work`.
  3. `convert(...)`. Map: `efficient` → status `skipped`; `no-gain` / `low-quality` → `rejected`; `error` → `error` with `error=message`; each with `reason=outcome.status` and `finished_at`. For these, if `was_online_only`, call `loc.evict(relpath)`.
  4. `ok` → `final = rules.final_name(...)` in the same folder; `loc.publish(out, final, mtime=item mtime, min_free=cfg.min_free_gb GiB)`; set `published` with `final_relpath`, `final_size`, `not_before = loc.now() + (cfg.mac_delete_delay_s if loc.is_mac else 0)`. If the location is not a Mac, call `finalize_published` immediately.
  5. Intake post-step, location `"d"` only: when the item ends as `done`, `skipped` or `rejected` and its source is `intake`, move the resulting file (the final file for `done`, the original otherwise) to `<cfg.d_converted>/<free_name>` with `loc.move_within`, update the row's `relpath`, and `db.meta_set("sorter_dirty", "1")`.
- `process_item` for a Downloads item (`dest_location` set):
  1. Same existence check. If `locs[dest_location].reachable()` is False → set `queued`, raise `MacUnreachable`.
  2. Fetch, then convert unless the name ends in `.webp`. Payload is the converted file when `ok`, the fetched original when `efficient`, `no-gain`, `low-quality` or `.webp`. `error` → status `error`, source file left in place.
  3. Publish the payload to `<dest_dir>/<name>` on the destination (`final_name` for converted, `free_name` for originals), then delete the source from Downloads, `log_conversion` when converted (location = destination), and `db.delete_item`. Return `"done"`.
- Any `MacUnreachable` → row back to `queued` (attempts unchanged), re-raise. `NoSpace` → row back to `queued`, `reason="no-space"`, return `"queued"`. Any other exception → status `error` with the message. Files created in `work` for the item are always removed.
- `finalize_published` for each `db.due_published` row, mirroring `compactor.finalize`: final missing or size ≠ `final_size` → log LOST, row back to `queued`; original missing → `done`; original size or mtime changed → status `rejected`, reason `changed`, both files kept; otherwise delete the original, `log_conversion(bytes_before=size, bytes_after=final_size)`, status `done`, and `loc.evict(final_relpath)` when `was_online_only`. Rows on an unreachable Mac are skipped.

- [ ] **Step 1: Write failing tests** (temp `LocalLocation`s for `"d"`, `"win_dl"`, and a `LocalLocation`-backed fake for `"mega"` with `is_mac=True` and recordable `evict` / `is_online_only` / `reachable`):
  - Library h264 video → returns `"done"`; `x_compressed.mp4` exists, `x.mp4` is gone, one `conversions` row with `bytes_after < bytes_before`, mtime of the new file equals the original's.
  - Library file that yields `no-gain` (config `min_gain_ratio=0.01`) → `rejected`, original untouched, no `_compressed` file, `work` empty.
  - File deleted before processing → `"gone"` and no row.
  - File changed after scan (append bytes) → `"queued"` and the row has the new size.
  - Mac item: after `process_item` the status is `published` and the original still exists; `finalize_published` before `not_before` deletes nothing; after patching `now` past it, the original is gone and status is `done`.
  - Mac item that was online-only → `evict` called with the final relpath after finalize; a rejected online-only item → `evict` called with the original relpath.
  - Review Focus 3: with a `published` row, deleting the final file then `finalize_published` → original still there, row `queued`. Calling `finalize_published` twice on a healthy row deletes once and logs one conversion.
  - Original modified between publish and finalize → both files exist, status `rejected`, reason `changed`.
  - Review Focus 2: fake Mac location whose `publish` raises `MacUnreachable` → `process_item` raises it, row is `queued`, original intact, no `_compressed` file, `work` empty.
  - Review Focus 1: `publish` raising `NoSpace` → returns `"queued"`, reason `no-space`, original intact.
  - Intake: item `_TO_CONVERT/v.mp4` → after processing, `_CONVERTED/v_compressed.mp4` exists, `_TO_CONVERT` is empty, `meta sorter_dirty == "1"`, row relpath updated.
  - Downloads `pic.png` → `mega/TBC/pic_compressed.webp` exists, `win_dl/pic.png` gone, row deleted, conversion logged under `mega`.
  - Downloads `pic.webp` → `mega/TBC/pic.webp`, no conversion logged.
  - Downloads item with the Mac unreachable → raises `MacUnreachable`, source still in Downloads, row `queued`.
  - Downloads item whose conversion errors (text file named `bad.mp4`) → status `error`, file still in Downloads.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_worker -v`. Expected: fail.
- [ ] **Step 3: Implement** `worker.py`.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat: worker with safe publish and delete`.

---

### Task 9: Service loop

**Files:**
- Create: `media_organizer/service.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: everything above.
- Produces:

```python
@dataclass(frozen=True)
class ScanDef:
    name: str            # "d-intake" | "d-full" | "mega-intake" | "mega-full" | "win-downloads" | "mac-downloads"
    location: str
    kind: str            # "intake" | "full" | "downloads"

SCANS: tuple[ScanDef, ...]

def due_scans(con, cfg: Config, now: float) -> list[ScanDef]
def run_scan(con, scan: ScanDef, locs, cfg) -> bool            # True when it ran; records it with db.record_scan
def run_sorter(con, locs, cfg) -> bool
class Service:
    def __init__(self, cfg: Config, locs: dict[str, Location] | None = None)
    def tick(self) -> None        # one scheduler pass: due scans, finalize_published, sorter
    def work_once(self, kind: str) -> str | None    # claim and process one item of that kind; None when idle
    def run_forever(self) -> None
    current: dict                 # {"name", "size", "progress", "location"} of the running video, or {}
```

- `due_scans`: a scan is due when it has never run; or its last run was ok and older than `cfg.scan_interval_h` (`cfg.full_scan_interval_h` for `full`); or its last run failed and is older than `cfg.mac_retry_h`. `full` scans cover intake too, so when a `full` scan is due the same location's `intake` scan is not returned.
- `run_scan`: unreachable location → `record_scan(ok=False, message="unreachable")`, return False. `intake` → `scan_library(sub=<intake folder>, full=False)`; `full` → `scan_library(sub="", full=True)`; `downloads` → `scan_downloads`. After any scan of `"d"`, also `refresh_folders(locs["d"])`.
- `run_sorter`: when `meta sorter_dirty == "1"` and no item with source `intake` in location `"d"` is `queued`, `running` or `published`, run `[sys.executable, cfg.sorter, "--src", <d_root>/<d_converted>, "--dst", <d_root>]` with `cwd=cfg.sorter.parent`, then set `sorter_dirty` to `"0"`. A missing sorter script is logged and clears the flag.
- `work_once`: returns None when `meta paused == "1"`. Excludes Mac locations that are not reachable (reachability cached for 60 s). On `MacUnreachable` marks the Mac unreachable and returns `"queued"`.
- `run_forever`: takes an exclusive `flock` on `state_dir/lock` (exit with a message if held); `db.reset_running`; creates `work_dir` empty; starts 1 video thread and `cfg.image_workers` image threads, each with its own connection, looping `work_once` and sleeping 15 s when idle; main thread calls `tick()` every 30 s; logs to `cfg.log_path` with `logging.handlers.RotatingFileHandler` (5 MB × 5) and to stderr; on SIGTERM stops claiming, kills the running ffmpeg, re-queues the running items and exits 0. Writes `meta current` as JSON at most every 10 s for the `status` command, and `meta mac_reachable`.

- [ ] **Step 1: Write failing tests** (no threads; drive `tick` and `work_once` directly; fake Mac location):
  - Fresh database → `due_scans` names are `{"d-full", "mega-full", "win-downloads", "mac-downloads"}`.
  - After all ran ok at `t`, at `t + 5 h` nothing is due; at `t + 6.1 h` → `{"d-intake", "mega-intake", "win-downloads", "mac-downloads"}`; at `t + 169 h` the `full` scans are due and the `intake` ones are not.
  - A failed `mac-downloads` scan is not due after 59 minutes and is due after 61.
  - `run_scan` on an unreachable Mac records `ok == 0`, message `unreachable`.
  - End to end on temp folders: one h264 file in `d_root/A/`, one in `_TO_CONVERT/`, one PNG in Windows Downloads; `tick()` then `work_once("video")` and `work_once("image")` until both return None, then `tick()` → `A/x_compressed.mp4` exists; the intake result was handed to a stub sorter script (the stub writes its argv to a file; assert `--src` ends with `_CONVERTED`); the PNG is in `mega/TBC` as `.webp`; `sorter_dirty == "0"`.
  - Priority: with a library video and a Downloads video queued, the first `work_once("video")` processes the Downloads one.
  - `meta paused = "1"` → `work_once` returns None and nothing changes.
  - Mac unreachable: a `mega` item and a `d` item queued → `work_once` processes the `d` item and leaves the `mega` item `queued`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_service -v`. Expected: fail.
- [ ] **Step 3: Implement** `service.py`.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat: scheduler and worker threads`.

---

### Task 10: Stats

**Files:**
- Create: `media_organizer/stats.py`, `tests/test_stats.py`

**Interfaces:**
- Consumes: `db`, `Config`.
- Produces: `def summary(con, cfg: Config, now: float | None = None) -> dict` with exactly these keys:

```python
{
 "generated": float,
 "freed": {"total": int, "d": int, "mega": int},                       # bytes, from conversions
 "disk": {"d_free": int, "d_total": int, "mega_bytes": int, "mega_bytes_start": int},   # latest / first snapshots
 "first_pass": {"d": {"done": int, "total": int, "eta_days": float | None},
                "mega": {"done": int, "total": int, "eta_days": float | None}},
 "milestones": [{"label": str, "done": bool}],
 "daily": [{"date": "YYYY-MM-DD", "d": int, "mega": int}],              # bytes freed per local day, last 30 days, zero-filled
 "now": {"name": str, "size": int, "progress": float, "location": str} | None,
 "paused": bool,
 "queue": {"downloads": int, "intake": int, "mega": int, "library": int},
 "scans": [{"name": str, "finished": float | None, "ok": bool | None, "message": str}],   # one per ScanDef name
 "mac_reachable": bool,
 "folders": {"d": [...], "mega": [...]},     # top 15 by bytes: {"folder", "bytes", "files", "compressed_files"}
 "recent": [{"ts", "location", "relpath", "bytes_before", "bytes_after"}],   # last 50 conversions, newest first
 "problems": {"error": int, "no-gain": int, "low-quality": int, "changed": int,
              "items": [{"location", "relpath", "reason", "error"}]}         # error rows, max 100
}
```

- `first_pass[loc]`: `total` = all rows of that location with source `library` or `intake`; `done` = those with status `done`, `skipped`, `rejected` or `error`. `eta_days` = remaining queued bytes ÷ (source bytes of conversions in the last 3 days ÷ 3); `None` when either is 0.
- Milestones, in this order: `D: first pass 25%`, `50%`, `75%`, `D: first pass done`, `MEGA first pass 25%`, `50%`, `75%`, `MEGA first pass done`, `D: under 70% full`, `100 GB freed`, `250 GB freed`, `500 GB freed`. A percentage milestone with `total == 0` is not done.
- Queue buckets: `downloads` = queued rows with source `downloads`; `intake` = source `intake`; `mega` / `library` = source `library` in location `mega` / `d`.

- [ ] **Step 1: Write failing tests**: seeded database with 4 `d` rows (2 done, 1 queued, 1 error), conversions of 30 GB (`d`) and 5 GB (`mega`) on two different days, snapshots, folders → assert `freed`, `first_pass["d"] == {"done": 3, "total": 4, ...}`, milestone `D: first pass 50%` done and `75%` done and `done` not done, `daily` has 30 entries with the two days non-zero, `queue["library"] == 1`, `problems["error"] == 1`, `folders["d"]` sorted by bytes descending and capped at 15, `scans` has 6 entries with `finished is None` for never-run ones; an empty database returns the full key set with zeros and `now is None`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_stats -v`. Expected: fail.
- [ ] **Step 3: Implement** `stats.py`.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit** `feat: summary statistics`.

---

### Task 11: CLI

**Files:**
- Create: `media_organizer/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`, `Service`, `stats.summary`, `legacy.import_legacy`, `db`, `scanner`, `probe.check_tools`.
- Produces: `def main(argv: list[str] | None = None) -> int`. Global option `--config PATH`. Commands:
  - `run` — exits 1 listing missing tools from `check_tools()`; otherwise `Service(cfg).run_forever()`; also starts the web server (Task 12) unless `--no-web`.
  - `status [--errors] [--json]` — prints the layout from the spec's "`status` output (sketch)" using `stats.summary`; sizes in GB with one decimal; `--errors` lists `problems.items`; `--json` dumps the summary.
  - `scan --dry-run` — walks all reachable locations and prints one line per file that would be queued or moved (`WOULD QUEUE d A/x.mp4 812.4 MB` / `WOULD MOVE win_dl big.mp4 -> d/_TO_CONVERT`), plus a count per location. Uses an in-memory copy of the database so nothing is written anywhere. `scan` without `--dry-run` is rejected with a message to use the service.
  - `pause`, `resume` — set `meta paused`.
  - `retry-errors` — prints the count.
  - `import-legacy` — prints the counts.

- [ ] **Step 1: Write failing tests** (call `main([...])` with a temp config, capture stdout): `status` on an empty database exits 0 and prints `First pass` and `Queue`; `status --json` parses as JSON with key `freed`; `pause` then `status` prints `paused`; `resume` clears it; `retry-errors` prints `1` after seeding one error row; `scan --dry-run` on temp folders prints a `WOULD QUEUE` line and a `WOULD MOVE` line, and afterwards the big file is still in Downloads and the database has no rows; `scan` without the flag exits 2.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_cli -v`. Expected: fail.
- [ ] **Step 3: Implement** `cli.py`. For the dry run, `scan_downloads` and `scan_library` gain a keyword `dry_run: bool = False` that suppresses every filesystem write (moves, `.mo-part` cleanup); add tests for that flag in `tests/test_scanner.py`.
- [ ] **Step 4: Run** the whole suite: `python3 -m unittest discover -s tests -v`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: command line`.

---

### Task 12: Web dashboard

**Files:**
- Create: `media_organizer/web.py`, `media_organizer/dashboard.html`, `tests/test_web.py`
- Modify: `media_organizer/cli.py` (start the server in `run`)

**Interfaces:**
- Consumes: `stats.summary`, `Config`, `db.open_db`.
- Produces: `def make_server(cfg: Config, host: str = "0.0.0.0") -> ThreadingHTTPServer` (raises `ValueError` when `cfg.web_password` is empty) and `def serve_in_thread(cfg: Config) -> ThreadingHTTPServer | None` (logs and returns None on empty password).
- Routes: `GET /` → `dashboard.html`; `GET /api/summary` → `stats.summary` as JSON; anything else 404; other methods 405. Every route requires HTTP Basic auth, any user name, password compared with `hmac.compare_digest`; failure → 401 with `WWW-Authenticate: Basic realm="media-organizer"`. Each request opens its own database connection.
- `dashboard.html`: one self-contained file (inline CSS and JavaScript, no external requests). It fetches `/api/summary` on load and every 30 s and renders the sections of the spec's "Dashboard" sketch, top to bottom: header with Mac status and pause state; three tiles (storage freed, D: free of total, MEGA size now and at start); milestones as a checklist; two first-pass progress bars with counts and estimated days left; a line chart of cumulative GB freed over the last 30 days drawn as inline SVG (one line per library, legend, y-axis labels, a `<title>` tooltip per point); now-encoding line with progress bar and queue counts; two tables of biggest folders (folder, size, files, percent compressed); recent activity table (time, file, before → after, saved); problems with an expandable list. Layout works at phone width (single column under 700 px, tables scroll horizontally inside their own container). Light and dark themes through `prefers-color-scheme`. Sizes shown in GB or MB with one decimal. File and folder names are inserted with `textContent`, never `innerHTML`. The chart is hand-drawn SVG, which replaces the "small chart library" mentioned in the spec so the page has no dependency.

- [ ] **Step 1: Write failing tests** in `tests/test_web.py` (server on port 0 in a thread, `urllib.request`): no credentials → 401 with the `WWW-Authenticate` header; wrong password → 401; right password on `/` → 200, `text/html`, body contains `Media Organizer`; `/api/summary` → 200 and JSON with key `milestones`; `/nope` → 404; `POST /` → 405; `make_server` with an empty password raises `ValueError`; `dashboard.html` contains no `http://` or `https://` URL and no `innerHTML`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_web -v`. Expected: fail.
- [ ] **Step 3: Implement** `web.py` and `dashboard.html`; wire `serve_in_thread` into `cli run`.
- [ ] **Step 4: Run** the whole suite. Expected: all pass. Then start the server on a seeded temp database and load the page once in a headless check (`curl -u x:pw http://127.0.0.1:<port>/`) to confirm it serves.
- [ ] **Step 5: Commit** `feat: read-only web dashboard`.

---

### Task 13: Deployment files and README

**Files:**
- Create: `deploy/media-organizer.service`, `README.md`

**Content:**
- `media-organizer.service` (systemd user unit): `ExecStart=/usr/bin/python3 -m media_organizer.cli run`, `WorkingDirectory=` the repo path, `Environment=PYTHONUNBUFFERED=1`, `CPUQuota=960%`, `Nice=19`, `IOSchedulingClass=idle`, `Restart=on-failure`, `RestartSec=60`, `KillSignal=SIGTERM`, `TimeoutStopSec=60`, `WantedBy=default.target`.
- `README.md`, written for the owner, covering: what it does (one paragraph); install (copy `config.example.toml` to `~/.config/media-organizer/config.toml` and set `web_password`; `media-organizer import-legacy`; `media-organizer scan --dry-run`; `systemctl --user enable --now media-organizer`; `loginctl enable-linger`); making the dashboard reachable from other devices (WSL mirrored networking in `.wslconfig`, or a `netsh interface portproxy` rule plus a Windows firewall rule for the port, both as copy-paste PowerShell); keeping WSL running after boot (a Windows Task Scheduler entry running `wsl.exe -d <distro> --exec /bin/true` at logon); Mac prerequisites (Remote Login, full disk access for remote users, fixed IP 192.168.0.30, the `Host mac` ssh alias); daily operation (`status`, dashboard address `http://192.168.0.33:8002`, `pause` / `resume`, `retry-errors`, where the log is); the meaning of each status and reason; recovery (service stopped mid-encode, Mac away, D: full, database lost — the `_compressed` suffix prevents double compression and rejected files are simply tried once more); cutover from the old cron job (`crontab -e`, remove the `pipeline.py` line) and rollback (stop the service, restore the cron line).

- [ ] **Step 1:** Write both files.
- [ ] **Step 2:** Run `systemd-analyze verify --user deploy/media-organizer.service` (warnings about the unit not being installed are fine; syntax errors are not) and `python3 -m unittest discover -s tests -v`. Expected: all pass.
- [ ] **Step 3: Commit** `docs: service unit and README`.

---

### Task 14: Staged verification on real data (needs the owner)

Not for an implementer subagent. Run by the session lead with the owner's go-ahead at each step, following the spec's Verification section:

- [ ] `media-organizer scan --dry-run` against the real config; compare the `_TO_CONVERT` list with `python3 ../app_compactor/pipeline.py --dry-run`.
- [ ] With the Mac reachable: confirm `ssh mac 'stat -f %f <an online-only MEGA file>'` has bit `0x40000000` set and a downloaded one does not; confirm `brctl evict` on a file still uploading is refused. Adjust `MacLocation` if either differs.
- [ ] Real run on a copied folder of about 10 mixed files (config pointing at the copy).
- [ ] Real run on one person folder on D:, then one MEGA subfolder with online-only files (check `df` on the Mac before and after), then one file through each Downloads route. Play results on the iPhone and the Mac.
- [ ] Install the systemd unit; confirm CPU stays near 60% during an encode (`systemd-cgtop --user`), and that `systemctl --user restart media-organizer` mid-encode loses and duplicates nothing.
- [ ] Open the dashboard from the Mac, iPhone and iPad; compare totals with `media-organizer status` and `df -h /mnt/d`.
- [ ] Cutover: remove the cron line, enable the service, watch the first day.
