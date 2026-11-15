# Media Organizer: design

## Context

Two media libraries hold a lot of uncompressed files, and D: is 98% full (26 GB free of 895 GB).

- **Big library:** `/mnt/d/7_XYZ/` on the gamer PC. 1,482 MP4 files make up 863 GB; 14 other videos (WMV, MKV, AVI) add 5 GB.
- **Small library:** `~/Library/Mobile Documents/com~apple~CloudDocs/MEGA` on the Mac (192.168.0.30), mostly online-only in iCloud. Reached from the gamer PC over SSH because iCloud for Windows is unreliable.

Today three scripts cover part of this: `compactor.py` (images to WebP, MP4/MOV to HEVC), `compactor_mac.py` (same, for the Mac's iCloud over sshfs, run by hand) and `pipeline.py` (WSL cron every 6 h: compress `_TO_CONVERT`, move to `_CONVERTED`, sort with `fileSortingv6.py`). Nothing compresses the already-sorted library, nothing runs MEGA on a schedule, and nothing watches Downloads.

The goal is one service on the gamer PC that frees storage by compressing everything in both libraries once, keeps doing it for new files, and sweeps both Downloads folders into the libraries.

## Goals

1. **First pass:** compress every eligible file in both libraries, in place.
2. **Ongoing:** every 6 hours, compress anything new in the intake folders (`D:\7_XYZ\_TO_CONVERT` and `MEGA/TBC`). Once a week, re-check both whole libraries by name and size to catch files saved straight into a library folder.
3. **Downloads:** every 6 hours, take every image and video from the Windows and Mac Downloads folders, compress it, and move it to the right library.
4. **Visibility:** a web dashboard, a log and a `status` command.

## Non-goals

- Moving files between the two libraries. The 400 MB rule applies to Downloads only.
- Changing how sorting by name works. `fileSortingv6.py` is called as it is; the 18 unmatched files in `_CONVERTED` stay there.
- Keeping originals. They are deleted as soon as the checks pass (decided).
- Lowering resolution. A 4K file stays 4K (decided).
- GPU encoding. It is faster but makes larger files, which works against "highly compressed".
- Phone notifications, anything running permanently on the Mac.
- Controlling the service from the dashboard. It only shows information; pausing and retrying stay on the command line.

## Anti-goals

- Losing a file: an original must never be deleted unless its replacement exists, is complete and passed the checks.
- Filling the Mac's 256 GB disk by downloading MEGA.
- Starving the server apps on the gamer PC.
- Compressing the same file twice.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| A | Extend the existing scripts with the missing jobs | Least new code; state stays in JSON files spread over several scripts; weak reporting |
| **B** | **Rewrite as one always-on service with a database and a work queue** | **Chosen by the user.** One place for state, proper status reporting, clean priorities and resume. Costs more code and more testing before it may touch 863 GB |
| C | Tdarr/Unmanic plus the current scripts | Dashboard for free, but no WebP, no iCloud handling, no Downloads routing; two systems |

To limit the risk of B, the conversion recipes and safety checks are ported from `compactor.py` and `compactor_mac.py` unchanged (same ffmpeg/cwebp arguments, same verification), not re-invented. Only the orchestration around them is new.

## Decisions made by the user

| Topic | Decision |
|---|---|
| Video output | HEVC (H.265) in `.mp4`, high compression |
| Originals | Deleted immediately after checks pass |
| Already-HEVC videos | Skipped, except "bloated" ones, which are re-encoded |
| Resolution | Kept |
| CPU | Runs at all times, capped at about 60% |
| Downloads rule | Every image and video is taken; the user moves exceptions out by hand |
| Downloads routing | Video over 400 MB to `/mnt/d/7_XYZ/_TO_CONVERT`; video up to 400 MB and all images to `MEGA/TBC` |
| Mac unreachable | Retry the Mac work every hour until it succeeds |
| Reporting | Web dashboard, log file and a status command |
| Dashboard access | Home network only, behind one password set in the settings file |
| Ongoing scope | Intake folders and Downloads every 6 hours; whole libraries once a week |
| Downloads folders | Windows (192.168.0.33): `C:\Users\rafavcc\Downloads`. Mac (192.168.0.30): `/Users/rafavcc/Downloads` |

## Calls made by me (correct any of these)

- **Language and storage:** Python 3 standard library plus SQLite. The heavy work is done by ffmpeg, x265 and cwebp, which are compiled programs, so the language that starts them has no effect on speed. Python lets the proven recipes and checks be ported line for line, and it includes the database and the web server the dashboard needs. A compiled language such as Go would add a build step and a full re-write of those checks for no gain here.
- **Location:** new project folder `Projects/media_organizer/` beside `app_compactor` and `fileSorting`, with its own git repository. The old scripts stay untouched until cutover.
- **State:** one SQLite database at `~/.local/share/media-organizer/organizer.db` on the Linux disk, outside both libraries. The existing `state.json` and `state-mac.json` are imported once, so the 230 MEGA files already rejected are not retried.
- **`_compressed` suffix:** kept. It is a second guard if the database is ever lost, and existing files already carry it.
- **More formats:** MKV, AVI, WMV, WEBM and M4V are also converted to `.mp4`.
- **"Bloated HEVC" rule:** an HEVC file is re-encoded when its video bitrate is above 0.07 bits per pixel per frame (roughly 2 to 3 times what CRF 30 produces). The threshold lives in the settings file and is tuned in the test phase on real samples. The existing 75% rule still applies, so a re-encode that saves less than 25% is discarded.
- **400 MB is measured on the file as downloaded**, before compression.
- **Order of work:** Downloads first, then `_TO_CONVERT`, then MEGA, then the library backlog. Within the D: backlog, largest files first, because D: has only 26 GB free.
- **`iCloudDrive\Downloads`** on Windows is not swept.
- **Files delivered to `MEGA/TBC`** stay there after compression; categorizing them is done by hand.
- **Dashboard technology:** one page served by the service itself, plain HTML and JavaScript with one small chart library, no separate build. It reads from the database, so it still shows MEGA figures from the last scan when the Mac is away.
- **Partial downloads** (`.part`, `.crdownload`, `.download`, modified in the last 10 minutes) are ignored.
- **Scheduler:** a systemd user service in WSL replaces the cron line. `CPUQuota=960%` (60% of 16 threads) and `Nice=19` give the CPU cap.

## Design

### Shape

One long-running program, `media-organizer`, with these parts:

```
     scanners (intake + Downloads every 6 h, whole libraries weekly;
               Mac ones retry hourly when the Mac is away)
   D: library   _TO_CONVERT   Win Downloads   Mac Downloads   MEGA (+TBC)
        \            |              |               |           /
         +-----------+------ SQLite: files + jobs --+----------+
                                  |
                       worker (1 video at a time,
                        several images in parallel)
                                  |
              encode locally -> verify -> publish -> delete original
                                  |
                 post-steps: move to _CONVERTED + run sorter,
                 deliver to MEGA/TBC, release file from Mac disk
```

- **Scanners** list files (names, sizes, dates only), decide eligibility and add jobs. They never download or modify anything.
- **Worker** takes the highest-priority job, copies the source to a local work folder on the Linux disk, encodes, verifies, publishes the result next to the original as `<name>_compressed.<ext>` (written as a `.part` file and renamed), then deletes the original.
- **Database** holds one row per known file (path, size, date, codec, status, reason) and one row per job (type, state, attempts, bytes before and after, error). Every step is recorded before it is taken, so a crash or reboot resumes cleanly and leftover `.part` files are cleaned up at start.
- **CLI:** `media-organizer run` (the service), `status`, `scan --dry-run`, `pause`, `resume`, `retry-errors`.

### Per-location behaviour

| Location | Access | Behaviour |
|---|---|---|
| `/mnt/d/7_XYZ/**` | local | Compress in place. Original deleted in the same step. Folders starting with `.` skipped. |
| `/mnt/d/7_XYZ/_TO_CONVERT` | local | Compress, move to `_CONVERTED`, run `fileSortingv6.py --src _CONVERTED --dst /mnt/d/7_XYZ` (current behaviour of `pipeline.py`). |
| Windows Downloads | local | Route by type and size. Big videos are moved to `_TO_CONVERT`. Small videos and images are compressed, then written to `MEGA/TBC`; the Downloads copy is deleted only after that write is confirmed. If the Mac is away, they wait. |
| Mac `~/Downloads` | SSH | Same routing. Files are pulled to the gamer PC, processed, delivered, then removed from the Mac. |
| Mac `MEGA/**` | sshfs + SSH | Per file: ask the Mac to download it (`brctl download`), copy once to the gamer PC, compress, write back, delete the original, then release the new file from the Mac's disk (`brctl evict`) if the original was online-only. One file at a time, so the Mac never holds more than a few files. |

Mac reachability is checked before each Mac job. If the Mac drops off mid-run, the job is put back untouched and nothing is deleted, as `compactor_mac.py` does today.

### Rules carried over unchanged from the current compactor

- Images: JPG 85, PNG/TIFF/BMP/HEIC 90, GIF animated lossy 75; result must be at most 75% of the original, same dimensions or frame count, SSIM at least 0.95.
- Videos: `libx265`, preset `fast`, CRF 30, 10-bit, constant frame rate, `hvc1` tag, audio copied when MP4 allows it, else AAC 192k; result must be at most 75% of the original, same resolution, same audio track count, duration within 1 second.
- Files that fail a check are recorded with the reason and left alone; they are retried only if the file or the settings change.
- Original dates are kept on converted files.

### Settings

One file, `~/.config/media-organizer/config.toml`: paths, the 400 MB threshold, scan interval (6 h), full-library sweep interval (7 days), Mac retry interval (1 h), dashboard port and password, Mac host, image worker count, the bloated-HEVC threshold, encoder settings.

## Delivery, by phase

| Phase | What happens |
|---|---|
| **Plan** | This design, then a step-by-step implementation plan. |
| **Code** | New `media_organizer` package: `db`, `probe`, `encode` (ported recipes), `verify`, `locations` (local, Mac), `scanners`, `worker`, `router` (Downloads), `web` (dashboard), `cli`. Test-first. |
| **Build** | No compile step. A `pyproject.toml` gives the `media-organizer` command; a startup check confirms ffmpeg with libx265, cwebp, gif2webp, webpmux, ImageMagick, sshfs. |
| **Test** | See Verification. |
| **Release** | Install the systemd user service with the CPU cap; import old state; remove the cron line for `pipeline.py`; keep the old scripts in place for one month as a fallback. Confirm WSL stays running without an open terminal. |
| **Operate** | The dashboard, `media-organizer status`, log at `~/.local/share/media-organizer/organizer.log` (rotated), `pause`/`resume`, settings file. A short README covers these and how to recover from the usual failures. |

### `status` output (sketch)

```
First pass   D: library   412 / 1,496 videos   freed 231.4 GB   D: free 257 GB
             MEGA          3,120 / 9,870 files  freed  18.2 GB
Queue        2 Downloads · 0 inbox · 6,750 MEGA · 1,084 library
Now          encoding "…_1080p.mp4" (2.1 GB)  37%
Last scans   D: 12:00 ok · Win Downloads 12:00 ok · Mac 12:00 unreachable, retry 13:00
Problems     3 errors · 222 no-gain · 7 low-quality      (media-organizer status --errors)
```

### Dashboard

Opened in a browser from any device at home (`http://192.168.0.33:<port>`), after entering the password. Read-only.

```
MEDIA ORGANIZER                                   Mac: away, retry 13:00
┌ Storage freed ─────────┐ ┌ D: free ──────────┐ ┌ iCloud MEGA ─────────┐
│ 249.6 GB               │ │ 257 GB of 895     │ │ 61.3 GB (was 79.5)   │
└────────────────────────┘ └───────────────────┘ └──────────────────────┘
Milestones
 ✔ Service installed          ✔ D: first pass 25%      ○ D: first pass 50%
 ○ D: first pass done (est. 9 days)   ○ MEGA first pass done   ○ D: under 70% full
First pass   D: ███████░░░░░░░░░░░░░ 412 / 1,496      MEGA ██████░░░░ 3,120 / 9,870
Storage freed over time            [line chart, GB per day, both libraries]
Now encoding  "…_1080p.mp4"  2.1 GB  37%      Queue: 2 Downloads · 0 inbox · 6,750 MEGA · 1,084 library
Biggest folders   D:\7_XYZ                    MEGA
                  Name A      41.2 GB         big        22.0 GB
                  Name B      38.7 GB         TBC         9.4 GB
                  …top 15, with size before / after and % compressed
Recent activity   time · file · before → after · saved          (last 50)
Problems          3 errors · 222 no-gain · 7 low-quality        (expandable list)
```

Data shown:
- **Milestones:** first-pass progress at 25/50/75/100% for each library, estimated finish date from the recent encoding rate, D: dropping below chosen fullness levels, total GB freed at round numbers.
- **Storage:** freed in total and per library, free space on D:, MEGA size now against its size at the start, a daily freed-space chart.
- **Biggest folders:** top folders in both libraries by size, with how much of each is already compressed. This also shows where the remaining savings are.
- **Work:** current file and progress, queue by source, last and next scan of each location, Mac reachable or not.
- **History and problems:** recent conversions with before and after sizes; files that errored or were rejected, with the reason.

## Technical details I settle while coding

These do not change what you get, so they need no decision from you. They are listed so nothing is forgotten.

- The exact database tables and how the code is split into files.
- How the service tells that a MEGA file is online-only, so it knows whether to release it from the Mac's disk afterwards. Also confirming that the Mac refuses to release a file that has not finished uploading. Both have to be checked on the Mac itself, which was unreachable during planning.
- How the encoder's thread count is set so the 60% CPU cap is used well.
- Making WSL start with Windows and stay running with no terminal open.
- Making the dashboard reachable from other devices, since WSL sits behind Windows' network by default.

## Verification

1. **Unit tests** (pytest, temporary folders, tiny clips and images generated with ffmpeg): eligibility rules, routing by type and 400 MB, the bloated-HEVC rule, name collisions, each verification failure leaving the original untouched, crash between publish and delete, resume after restart, Mac going away mid-job (simulated).
2. **Parity test:** the new encoder produces the same ffmpeg and cwebp commands as `compactor.py` for the same inputs.
3. **Dry run on the real libraries:** `media-organizer scan --dry-run` lists what would be done and changes nothing. Compare its list for `_TO_CONVERT` and MEGA against the old scripts' dry runs.
4. **Staged real runs:** a copied folder of about 10 mixed files, then one person's folder on D:, then one MEGA subfolder with online-only files (check Mac free disk before and after), then one file through each Downloads route. Play the results on the iPhone and the Mac.
5. **Service check:** under the systemd unit, confirm CPU use stays near 60% during an encode and that the service survives a WSL restart mid-encode with no lost or duplicated file.
6. **Dashboard check:** open it from the Mac, iPhone and iPad; its totals must match `media-organizer status` and `df` on D:; it must still load with the Mac switched off.
7. **Cutover:** enable the full first pass only after steps 1 to 5 pass; watch `status` for the first day.
