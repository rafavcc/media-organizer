# Media organizer

A service that runs on the Windows 11 PC (inside Ubuntu in WSL2) and makes your photos and videos smaller. It has three rules:

1. **Compress in place.** Every video and image in the D: library (`D:\7_XYZ`) and in the iCloud `MEGA` folder on the Mac is converted next to the original: videos to H.265 `.mp4`, images to `.webp`. The new file is named `<name>_compressed.<ext>`. Resolution is never lowered.
2. **Downloads are swept.** Every 6 hours it takes the images and videos from the Windows Downloads folder and the Mac Downloads folder **and all their subfolders** (folders whose name starts with `.` are skipped, and links to other folders are never followed). A video bigger than 400 MB goes to `D:\7_XYZ\_TO_CONVERT`; every other video and all images go to `MEGA/TBC`, compressed on the way (`.webp` files are only moved there, never converted). Files land in the destination under their own name, without the subfolder; two files with the same name get `(1)`, `(2)`, ... (or a different `_compressed` name). The same sweep **deletes every `.nfo` file** it finds in Downloads and **deletes empty folders** there (details below).
3. **Originals are deleted** once the new file is verified. There is no backup and no Recycle Bin. A file is only replaced when all of this is true:
   - the new file is at most 75% of the original size (otherwise nothing is gained and the original stays);
   - it has the same width and height, the same number of audio tracks and (video) the same length within 1 second; images must also look the same (similarity score at least 0.95);
   - the new file is in its final place with the right size, and the original has not been changed since the program read it.

A converted file carries the time of its conversion as its modification date; an original that is only moved (a `.webp`, or a video or image that was not converted) keeps its own date.

The recipes (quality settings and checks) are the same as in the old `app_compactor` tool. One dashboard page and a log show progress: see "Daily use".

## Contents

- [Before you start](#before-you-start)
- [First run, step by step](#first-run-step-by-step)
- [Daily use](#daily-use)
- [Opening the dashboard from other devices](#opening-the-dashboard-from-other-devices)
- [Keeping it running](#keeping-it-running)
- [What the statuses mean](#what-the-statuses-mean)
- [When something goes wrong](#when-something-goes-wrong)
- [What it will never do](#what-it-will-never-do)
- [Settings](#settings)
- [Status of this release](#status-of-this-release)

## Before you start

Everything below is typed in the Ubuntu (WSL) terminal unless it says PowerShell.

- **systemd must be on in WSL.** Check that `/etc/wsl.conf` contains:

  ```
  [boot]
  systemd=true
  ```

  It already does on this PC. If you ever change it, run `wsl --shutdown` in PowerShell.
- **Tools** (the program checks them at start and lists what is missing):
  `sudo apt install webp ffmpeg libimage-exiftool-perl imagemagick sshfs` (ffmpeg must include `libx265`; Ubuntu's does).
- **The Mac** (needed for MEGA and the Mac Downloads): 
  - On the Mac: System Settings, General, Sharing: turn on **Remote Login**, and in its info button turn on **Allow full disk access for remote users**. Give the Mac a fixed IP in your router (192.168.0.30).
  - In WSL: `sudo apt install sshfs`, then create an ssh key if you have none (`ssh-keygen -t ed25519`, empty passphrase), add this to `~/.ssh/config` (`Host mac`, `HostName 192.168.0.30`, `User` your Mac user name, `IdentityFile ~/.ssh/id_ed25519`, `ServerAliveInterval 15`) and run `ssh-copy-id mac`.
  - Check with `ssh mac true`: it must return without asking for a password.
  - More detail: section 9 ("Mac version") of the old tool's README, `../app_compactor/README.md`.
  - The service mounts the Mac itself at `~/.local/share/media-organizer/mac`; you only mount it by hand to preview MEGA (see first-run step 3). The Mac needs `brctl`, which is part of macOS.
- **WSL clock.** If WSL's clock is wrong (for example 3 hours ahead), see "WSL clock 3 hours ahead" at the end of the same old README. (Mac waiting times are measured against a time read from the Mac, so this matters less than it did, but a wrong clock still confuses logs and file dates.)
- **D: must be mounted in WSL.** Check: `ls /mnt/d/7_XYZ` should list your folders. An unmounted D: looks like an empty library and shows up as "0 files" in a scan.

Everything is run from the repository folder. To avoid typing the long form, add this alias once:

```bash
cd /mnt/c/Users/rafavcc/iCloudDrive/Programming/Projects/media_organizer
echo "alias media-organizer='python3 -m media_organizer.cli'" >> ~/.bashrc
source ~/.bashrc
```

Wherever this README says `media-organizer ...`, run it **from the repository folder** (the alias needs it). There is nothing to `pip install`.

## First run, step by step

The first full pass over the roughly 860 GB in D: takes **days to weeks**. That is normal. The PC stays usable: the service is capped at 60% of the CPU and runs at the lowest priority.

1. **Create the settings file and set the password.**

   ```bash
   mkdir -p ~/.config/media-organizer
   cp deploy/config.example.toml ~/.config/media-organizer/config.toml
   nano ~/.config/media-organizer/config.toml
   ```

   Change the line `web_password = "change-me"` to a password of your own. The dashboard refuses to start with an empty password. Every other line is commented out and shows its default; paths only need changing if yours differ. See "Settings".

2. **Import what the old tool already rejected**, so the roughly 230 MEGA files it found not worth compressing are not tried again:

   ```bash
   media-organizer import-legacy
   ```

   Expected output: `imported 230, skipped 0` (the numbers will differ). It is safe to run if the old state files do not exist (`imported 0, skipped 0`).

3. **Preview what it would do.** First make sure D: is mounted: `ls /mnt/d/7_XYZ`. Then:

   ```bash
   media-organizer scan --dry-run
   ```

   This changes nothing and writes nothing to the real database. It prints lines like `WOULD QUEUE d Some/Folder/clip.mp4 812.3 MB` and `WOULD MOVE win_dl big.mkv -> ...`, `WOULD DELETE win_dl Some/Folder/movie.nfo` (a `.nfo` file that would be deleted) and `WOULD REMOVE FOLDER win_dl Old/Empty` (a folder that is empty right now and would be removed), then one summary per place: `d: 1488 would queue, 0 would move` (the Downloads places add `, 0 .nfo, 0 folders`). The four places are `d` (D: library), `mega`, `win_dl` (Windows Downloads) and `mac_dl` (Mac Downloads). What to look for:
   - `d: 0 would queue` when you know D: is full of videos means D: is not mounted. Stop and fix that.
   - `SKIP mega (unreachable)` or `SKIP mac_dl (unreachable)` is what you see unless the Mac is already mounted: a dry run never mounts it. To preview MEGA, run `ssh mac true`, then mount it as the old README describes and run the dry run again:

     ```bash
     mkdir -p ~/.local/share/media-organizer/mac
     sshfs mac: ~/.local/share/media-organizer/mac -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3
     ```

     (This is the same mount the service makes; the dry run only recognises a Mac mounted at exactly that place. The old README mounts it elsewhere, which does not help here.) Unmount afterwards with `fusermount -u ~/.local/share/media-organizer/mac`. Do not start the service for this: the service deletes originals.
   - Names in `_compressed`, files in dot-folders (`.something`) and `.nfo` files that are less than 10 minutes old must not appear. Read the `WOULD DELETE` and `WOULD REMOVE FOLDER` lines carefully: the real service deletes exactly those.

4. **Stop the old cron job** (`pipeline.py`) so two programs never work on `_TO_CONVERT` at once:

   ```bash
   crontab -l > ~/crontab-before.txt   # save a copy first, for an exact rollback
   crontab -l          # shows your scheduled jobs; find the line containing pipeline.py
   crontab -e          # opens them in an editor; delete that line (or put # in front), save, exit
   crontab -l          # check the line is gone
   ```

   **Rollback:** stop the service (`systemctl --user disable --now media-organizer`), run `crontab -e` again and put the line back (your saved copy is `~/crontab-before.txt`). Keep the old scripts in place for a month as a fallback.

5. **Staged trials on copies (do not skip).** The program deletes originals, and it has not yet been run on real data (see "Status of this release"). Run it first against throw-away copies, with a settings file and a state folder of its own per trial, so the real database stays untouched. Each trial has its **own complete settings file** with every path key written out, so no real folder is ever inherited from an earlier trial. Create the four files below (`~/media-organizer-trial1.toml` to `~/media-organizer-trial4.toml`). Paths of the Windows side (`d_root`, `win_downloads`, `state_dir`) are full Linux paths; `mac_mega` and `mac_downloads` are relative to the Mac's home folder; `mega_intake` is a folder inside `mac_mega`. Create the trial folders first (`mkdir -p /mnt/d/_trial /mnt/c/Users/rafavcc/trial-downloads`; the Mac ones on the Mac).

   **Trial 1: a copied folder** of about 10 mixed files (videos and images), copied with Windows Explorer or `cp -r` into `/mnt/d/_trial/`. `~/media-organizer-trial1.toml`:

   ```toml
   d_root = "/mnt/d/_trial"
   win_downloads = "/mnt/c/Users/rafavcc/trial-downloads"
   state_dir = "~/.local/share/media-organizer-trial1"
   mac_mega = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/MEGA_TRIAL"
   mac_downloads = "Downloads/trial"
   mega_intake = "TBC"
   web_port = 8766
   web_password = "trial"
   ```

   Must stay untouched: everything in `D:\7_XYZ` outside `_trial`, the real Downloads folders, the real `MEGA` folder and the real service database.

   **Trial 2: one person's folder on D:** (the real files, so first make sure you have a backup of that folder). `~/media-organizer-trial2.toml`:

   ```toml
   d_root = "/mnt/d/7_XYZ/<that folder>"
   win_downloads = "/mnt/c/Users/rafavcc/trial-downloads"
   state_dir = "~/.local/share/media-organizer-trial2"
   mac_mega = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/MEGA_TRIAL"
   mac_downloads = "Downloads/trial"
   mega_intake = "TBC"
   web_port = 8766
   web_password = "trial"
   ```

   Must stay untouched: every other folder in `D:\7_XYZ`, the real Downloads folders and the real `MEGA` folder. This is the only trial whose `d_root` is a real folder.

   **Trial 3: a MEGA subfolder with online-only files.** On the Mac create the folder `MEGA/MEGA_TRIAL` (inside `Library/Mobile Documents/com~apple~CloudDocs/`) and put a few files in it that are **online-only** (copy a few photos and videos in, wait until they have synced, then right-click each and choose "Remove Download"). Note the Mac's free disk space before the trial and again after it (System Settings, General, Storage); it must not grow by more than a few files. `~/media-organizer-trial3.toml`:

   ```toml
   d_root = "/mnt/d/_trial"
   win_downloads = "/mnt/c/Users/rafavcc/trial-downloads"
   state_dir = "~/.local/share/media-organizer-trial3"
   mac_mega = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/MEGA_TRIAL"
   mac_downloads = "Downloads/trial"
   mega_intake = "TBC"
   web_port = 8766
   web_password = "trial"
   ```

   Must stay untouched: the rest of `MEGA` (everything outside `MEGA_TRIAL`), the Mac's real `Downloads` and everything on D: except `_trial`.

   **Trial 4: one file through each Downloads route.** Put into `trial-downloads` (and the Mac's `Downloads/trial`) a video over 400 MB, a small video and an image, some of them inside subfolders; the big video must land in `_trial/_TO_CONVERT` and the others in the trial MEGA `TBC` folder. Also add a `.nfo` file, an empty subfolder and a `.pdf` inside another subfolder, **at least 10 minutes before you start `run`** (they are only touched once they are 10 minutes old, and the next sweep is 6 hours later; alternatively add `min_age_min = 0` to that trial's settings file, which makes every file and folder eligible at once). After the first sweep the `.nfo` and the empty folder must be gone and the `.pdf` and its folder must still be there. `~/media-organizer-trial4.toml`:

   ```toml
   d_root = "/mnt/d/_trial"
   win_downloads = "/mnt/c/Users/rafavcc/trial-downloads"
   state_dir = "~/.local/share/media-organizer-trial4"
   mac_mega = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/MEGA_TRIAL"
   mac_downloads = "Downloads/trial"
   mega_intake = "TBC"
   web_port = 8766
   web_password = "trial"
   ```

   Must stay untouched: the real `Downloads` folders (Windows and Mac), the real `MEGA` folder and every folder of `D:\7_XYZ` except `_trial`.

   Run a trial in the foreground (use that trial's file):

   ```bash
   python3 -m media_organizer.cli --config ~/media-organizer-trial1.toml run --no-web
   ```

   Press Ctrl+C to stop it: the current item is put back in the queue (or finished) and nothing is lost. In another terminal, `python3 -m media_organizer.cli --config ~/media-organizer-trial1.toml status` shows progress. Files must be at least 10 minutes old to be picked up. Afterwards check: `status` shows no unexpected errors; each converted file plays (on the iPhone and the Mac); and an original is gone only where its `_compressed` file exists. Do the four trials in this order.

   Enable the real service (next step) **only after all four trials look right.** Do not run a trial and the real service on the same folders at the same time.

6. **Install and start the service.**

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/media-organizer.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now media-organizer
   loginctl enable-linger $USER
   systemctl --user status media-organizer
   ```

   `loginctl enable-linger` lets the service keep running with no terminal open. The last command should show `active (running)`. If you do not want to start it yet, use `systemctl --user enable media-organizer` without `--now`. (Copy the unit, do not link it: files on `/mnt/c` look executable and world-writable to systemd, which complains.) If you move the repository, edit `WorkingDirectory=` in the unit.

7. **Open the dashboard** on the PC at `http://localhost:8765` (user name: anything; password: the one from step 1). Use the `192.168.0.33` address only from other devices, after "Opening the dashboard from other devices". You should see a page asking for the password. A blank page or a timeout means the service is not running: check `systemctl --user status media-organizer`. After a minute `media-organizer status` should show scans completing.

## Daily use

All from the repository folder:

| Command | What it does |
|---|---|
| `media-organizer status` | Summary: first-pass progress, space freed, D: free space (live), queue (including items waiting for space and files still to be released from the Mac disk), what is encoding now, last scan of each place, problem counts |
| `media-organizer status --errors` | Lists the files in error, with the error text |
| `media-organizer status --json` | The same data as JSON |
| `media-organizer pause` | Stop starting new items; an encode already running finishes. The service keeps running |
| `media-organizer resume` | Carry on |
| `media-organizer retry-errors` | Put every file in `error` back in the queue |
| `media-organizer scan --dry-run` | Preview, as in the first run |

Sample `status`:

```
First pass   D: library   412 / 1,496 items   freed 231.4 GB   D: free 257 GB
             MEGA          3,120 / 9,870 files  freed  18.2 GB
Queue        2 Downloads · 0 inbox · 6,750 MEGA · 1,084 library · 3 waiting for space · 5 to release on the Mac
Now          encoding "clip_1080p.mp4" (2.1 GB)  37%
Last scans   D: intake 12:00 ok · D: full 03:10 ok · MEGA intake 12:00 ok · MEGA full 03:40 ok · Win Downloads 12:00 ok · Mac Downloads 12:00 unreachable, retry 13:00
Problems     3 errors · 222 no-gain · 7 low-quality · 1 changed · 2 multi-frame · 1 stuck (original not deleted)      (media-organizer status --errors)
```

Other things you can do:

- **Dashboard:** `http://localhost:8765` on the PC, `http://192.168.0.33:8765` from other devices. Read-only: space freed, D: free space, milestones, progress, charts (space freed over time, conversions per day, the biggest folders on D: and in MEGA), recent conversions and problems. The charts use Chart.js, which the program serves itself (behind the same password), so the page needs no internet connection; if the chart library cannot load, the charts show "Charts unavailable" and the rest of the page works. It still works when the Mac is off (MEGA figures are from the last scan). Pause, resume and retry are only available on the command line.
- **Log:** `~/.local/share/media-organizer/organizer.log` (rotated, 5 files of 5 MB). Follow it with `tail -f ~/.local/share/media-organizer/organizer.log`, or `journalctl --user -u media-organizer -f`.
- **Service:** `systemctl --user stop|start|restart media-organizer`. Stop waits up to 60 seconds for the running encode to be put back cleanly.
- **When things are scanned:** the intake folders (`D:\_TO_CONVERT`, `MEGA/TBC`) and both Downloads folders (with all their subfolders) every 6 hours; both whole libraries once a week; Mac scans are retried every hour while the Mac is away.
- Files modified in the last 10 minutes, empty files, files starting with `.` and `.part`, `.crdownload`, `.download` files are ignored until they settle.
- **Downloads cleanup.** Besides moving media, each Downloads sweep (Windows and Mac) deletes things it considers clutter. **This is permanent: no Recycle Bin.**
  - Every file ending in `.nfo` (any capitals), in any subfolder, once it is at least 10 minutes old.
  - Empty folders, deepest first. A folder counts as empty when it holds nothing, or only `.DS_Store`, `Thumbs.db` or `desktop.ini` (these three are deleted together with the folder). It is removed only when the folder itself was last changed at least 10 minutes ago. The Downloads folder itself, folders starting with `.` and anything inside them are never touched. A folder that contains anything else (for example a `.pdf` or `.zip`) is kept. A folder that becomes empty because its last file was just moved or deleted is removed by a later sweep, not the same one.
  - Each deleted `.nfo` and removed folder is written to the log (`deleted ...`, `removed folder ...`). The database keeps the counts of the last sweep in the scan message, for example `found 12, queued 3, moved 1, nfo 2, folders 1` (counts that are 0 are left out); see it with `media-organizer status --json`.

Files in `D:\7_XYZ\_TO_CONVERT` are, after conversion, moved to `_CONVERTED` and the sorter (`fileSortingv6.py`) is run on them, as the old `pipeline.py` did. Files delivered to `MEGA/TBC` stay there for you to sort by hand.

## Opening the dashboard from other devices

The dashboard listens on port 8765 inside WSL. By default only the PC itself can reach it. First confirm the PC's home-network address: run `ipconfig` in PowerShell and look at the IPv4 address (this README assumes `192.168.0.33`). Then pick **one** of these:

**(a) Mirrored networking (simplest).** In Windows, create or edit `%UserProfile%\.wslconfig` (that is `C:\Users\rafavcc\.wslconfig`) and put:

```
[wsl2]
networkingMode=mirrored
```

Then in PowerShell run `wsl --shutdown` and open Ubuntu again. WSL now shares the PC's address. This path is **untested** here: in mirrored mode inbound connections can also be blocked by the Hyper-V firewall, so the Windows Defender rule alone may not be enough. If another device cannot connect, use option (b) instead.

**(b) Port forwarding.** In PowerShell **as administrator**:

```powershell
$wsl = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8765 connectaddress=$wsl connectport=8765
New-NetFirewallRule -DisplayName "Media organizer dashboard" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private -RemoteAddress LocalSubnet
```

WSL's own address changes after a restart, so the rule must be redone then: run `netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8765` and the first two lines again. Check it with `netsh interface portproxy show all`. (Do not use this together with mirrored networking.)

Test on the PC first with `http://localhost:8765`, then from the Mac or iPhone with `http://192.168.0.33:8765`.

**Security:** the dashboard is protected only by the password, sent unencrypted (plain HTTP), and it shows file and folder names. It is for the home network only. Never forward the port on the router and do not reuse an important password.

## Keeping it running

WSL shuts down by itself a little after the last terminal closes, and then the service stops. To keep WSL alive from the moment you log in to Windows, add a Task Scheduler entry:

1. In PowerShell run `wsl -l -v` and note your distribution name (for example `Ubuntu`).
2. Open Task Scheduler, **Create Task**. Name: `Keep WSL alive`.
3. General: choose **Run only when user is logged on** and tick **Hidden** (or start the task with its window minimised). This is the first choice. The other option, **Run whether user is logged on or not**, may stop `wsl.exe` from starting at all in a non-interactive session; try it only if you want no window, and test it as described below. Trigger: **At log on**. Action: **Start a program**, program `wsl.exe`, arguments `-d Ubuntu --exec /bin/sleep infinity` (use your distro name).
4. Conditions: untick "Start the task only if the computer is on AC power" if this is a laptop. Settings: untick "Stop the task if it runs longer than".

Together with `loginctl enable-linger $USER` (from first-run step 6), the service then starts with WSL and survives closing every terminal. To test, run `wsl --shutdown`, wait a few seconds, start the task from Task Scheduler (right-click, Run) and check `systemctl --user status media-organizer` in a new Ubuntu window.

## What the statuses mean

Each file the program knows has a status, and often a reason. Statuses appear in the database; the dashboard and `status` show problems (`error`, `no-gain`, `low-quality`, `changed`, `multi-frame`, `stuck`). Files that finish fine and whose result was delivered from Downloads are forgotten afterwards.

| Status | Reason | Meaning | What happens next |
|---|---|---|---|
| `queued` | | Waiting its turn (Downloads first, then the inbox folders, then MEGA, then the D: library; largest first within each) | Processed in time |
| `queued` | `no-space` | Not enough free space: the destination needs 75% of the original plus `min_free_gb`, and the work folder 2 times the original plus `min_free_gb` (checked before anything is copied; also set when writing the result finds the drive full) | Left alone for one hour, then tried again; other files go first meanwhile. `status` counts these as `waiting for space`; see "D: nearly full" |
| `queued` | `name-taken` | Another file took the planned name at the last moment | Planned again at once with a fresh name |
| `running` | | Being converted right now | If the service stops, it goes back to `queued` |
| `published` | | New file written and in place; waiting to delete the original (on the Mac it waits about a minute so iCloud can catch up) | Original deleted when its checks pass |
| `done` | | Converted, original deleted | Never touched again |
| `done` | `post-step-failed` | Converted, but moving it to `_CONVERTED` or running the sorter failed | Retried automatically; look in the log |
| `skipped` | `efficient` | A video already in AV1, or in H.265 at a bitrate that is already low | Left alone |
| `rejected` | `no-gain` | The result was bigger than 75% of the original | Original kept; not retried unless the file or the settings change |
| `rejected` | `low-quality` | An image's similarity score was below 0.95 | Original kept; same |
| `rejected` | `multi-frame` | An image with several pages or frames (multi-page TIFF, multi-image HEIC, animated PNG); only one frame would survive | Original kept; not retried unless the file changes |
| `rejected` | `changed` | The original changed while it was being processed | The file is queued again if a later scan sees it changed; the new file is kept |
| `error` | | Something failed (the message is in `status --errors`). This includes `encoder stalled: no progress for 30 min` (see `encode_stall_min`) and a conversion tool that ran past its time limit | Fix the cause, then `media-organizer retry-errors` |
| `error` | `could not delete original: ...` | The new file is in place but the original could not be deleted five times (for example a read-only file); the new file is kept | Fix the cause, then `media-organizer retry-errors` finishes it. While it was trying, `status` shows it as `stuck` after an hour |

## When something goes wrong

- **The service stopped, or WSL or the PC restarted mid-encode.** Nothing is lost. At the next start, files marked running go back to the queue, the temporary work folder is emptied, and the encode starts again from the beginning. A half-written `.mo-part` file left in a library is deleted by the next weekly whole-library scan once it is old. Originals are only deleted after the checks, so an interrupted file is simply tried again. If a leftover `*_compressed` file and its original both exist, the next pass handles them; do not delete either by hand while the service is running.
- **The Mac is away (asleep, off, or off the network).** Nothing is flagged or deleted. Files that need the Mac stay `queued`, and the Mac scans are retried every hour. Work on D: continues meanwhile. When the Mac is back, it carries on by itself. If it never recovers, check `ssh mac true` first, then `mount | grep media-organizer`; a stale mount is cleaned up by the service on its next attempt.
- **D: (or the Mac) is nearly full.** Before copying a file the program checks that the destination has 75% of the original's size plus `min_free_gb` (2 GB) free, and that the work folder has twice the original's size plus 2 GB. If not, the file waits as `queued` / `no-space` for one hour and other files go first; `status` shows how many are `waiting for space`. Each converted file frees its original's space once the original is deleted. If the drive is completely full, free a few GB by hand (for example delete files you do not need); the waiting files are tried again within the hour. The work folder is `work` inside the state folder; it lives on the Linux disk.
- **The database was lost or deleted** (`~/.local/share/media-organizer/organizer.db`). Restart the service (`systemctl --user restart media-organizer`); it creates a new empty database and starts over by scanning. Optionally run `media-organizer import-legacy` first to re-import the old tool's rejections. Files already ending in `_compressed` are never processed again, so nothing is compressed twice. Files that were rejected before (the original is still there) are simply tried once more and rejected again; this wastes time but is harmless. Only a conversion interrupted at exactly the wrong moment could leave one extra copy, named like `a_jpg_compressed.webp` next to `a_compressed.webp`; delete the extra by hand.
- **An original came back after it was deleted (iCloud).** The old tool's README describes iCloud restoring files that were created and deleted within seconds. This program writes the new file with a temporary name inside the folder, renames it, and waits before deleting an original on the Mac; but the real iCloud behaviour has not been checked against this program (see "Status of this release"). If you see a file and its `_compressed` copy both back, tell the developer, and do not delete the `_compressed` one until you have checked it plays.
- **Port already in use.** The log says "dashboard disabled: cannot bind port"; the rest of the service still works. Change `web_port` in the settings file and restart.
- **"already running (lock held)".** A second copy was started. Use `systemctl --user status media-organizer` to see the first one.
- **"missing tools: ..."** Install them with `sudo apt install` (see "Before you start").

## What it will never do

- It never enters a folder whose name starts with `.` (in Downloads or in the libraries), nor a macOS package or library folder (a folder whose name ends in `.app`, `.bundle`, `.framework`, `.plugin`, `.kext`, `.pkg`, `.mpkg`, `.photoslibrary`, `.fcpbundle`, `.imovielibrary`, `.musiclibrary`, `.tvlibrary`, `.xcarchive`, `.lrlibrary`, `.lrdata` or `.dSYM`, any capitals); these are never searched, moved from or removed. A folder it cannot read (permissions) is skipped with a line in the log, and neither it nor its parents are ever removed. It cannot recognise an extracted Windows program or game folder: its images and videos **will** be swept and its `.nfo` files deleted, so do not extract software inside Downloads. It also never follows a symbolic link or junction to another folder or file.
- In Downloads the only things it deletes are: `.nfo` files, empty folders (with their `.DS_Store`, `Thumbs.db` or `desktop.ini`) and the originals it has moved or converted after checking them. It never deletes the Downloads folder itself, never deletes a folder that holds anything else, and removes folders only with a plain "remove empty folder" (never recursively). Any other file type in Downloads (`.pdf`, `.zip`, ...) is left alone.
- It never touches a file whose name ends in `_compressed` (any capitals).
- It never deletes an original that changed since it was read, or one whose replacement is missing or the wrong size.
- It never lowers resolution, and never uses lossless WebP.
- Inside the libraries it only adds `<name>_compressed.<ext>` files (first as a `.mo-part` file, renamed when complete) and then removes the original. The only other places it writes are the delivery and routing folders (`_TO_CONVERT`, `_CONVERTED` with the sorter's results, `MEGA/TBC`).
- It never changes anything from the dashboard: the dashboard only shows information.
- It never moves files between the D: library and MEGA; the 400 MB rule applies to Downloads only.

## Settings

All settings are in `~/.config/media-organizer/config.toml`; `deploy/config.example.toml` lists every key with its default. The ones you may want to change:

| Key | Default | Meaning |
|---|---|---|
| `web_password` | (none, required) | Dashboard password |
| `web_port` | 8765 | Dashboard port |
| `size_threshold_mb` | 400 | Downloads video size above which it goes to `_TO_CONVERT` |
| `scan_interval_h` | 6 | Hours between scans of intake and Downloads |
| `full_scan_interval_h` | 168 | Hours between whole-library scans |
| `image_workers` | 4 | Images converted in parallel (videos always one at a time) |
| `min_free_gb` | 2 | Free space to keep on the destination drive |
| `mac_host` | `mac` | The ssh alias of the Mac |
| `encode_stall_min` | 30 | Minutes without any encoding progress after which a video encode is considered stuck |
| `min_age_min` | 10 | Minutes a file or folder must be unchanged before it is touched (applies to library and intake files, Downloads media, `.nfo` files and empty folders) |

**Encoder watchdog.** While a video is being encoded, the program watches ffmpeg's progress. If it does not move for `encode_stall_min` minutes (counted from the start of the encode), ffmpeg is stopped, the file is marked `error` ("encoder stalled") with the original untouched, and the next file goes on. Image conversions, ffprobe and the other conversion tools also have time limits (2 minutes for probing, 30 minutes for the rest); a tool that runs past its limit is stopped and the file is marked `error` naming the tool. Use `media-organizer retry-errors` once the cause is fixed. For very slow machines or very long videos, raise `encode_stall_min`.

**Downloads paths are checked at start.** `win_downloads`, `d_root` and `state_dir` must be absolute paths (`~` is allowed; links are followed before the checks). The program refuses to start when `win_downloads` is `/`, a drive (`/mnt/c`), `/mnt/c/Users`, a user's home folder, or overlaps `d_root` or `state_dir`; or when `mac_downloads` is empty, `.`, `Desktop`, `Documents`, `Library` or inside `Library`, or overlaps `mac_mega`. The message names the key.

Restart the service after a change: `systemctl --user restart media-organizer`. An unknown key makes the program refuse to start, with the key's name in the message. If you change the quality settings (`crf`, `min_gain_ratio`, `min_ssim`, `hevc_bloat_bpp`), previously rejected files are looked at again, and `import-legacy` skips the old verdicts.

## Status of this release

The automated tests only use temporary folders and tiny generated files. The program has **not yet been run on your real data**. That is why first-run step 5 stages the first runs on copies, in this order:

1. a copied folder of about 10 mixed files;
2. one person's folder on D:;
3. one MEGA subfolder with online-only files (watch the Mac's free disk space before and after);
4. one file through each Downloads route (small video, big video, image; Windows and Mac).

Play the results on the iPhone and the Mac. The real service is enabled only after all four look right. Until then, treat the real libraries as untested, and keep the old scripts for a month.

**Assumptions the trials must verify** (none of these could be checked without the real machines):

- **CPU cap.** Run `systemctl --user show media-organizer -p CPUQuotaPerSecUSec` and watch Task Manager during an encode: the service should stay near 60% of the CPU. A user unit in WSL may not be able to apply the cap.
- **Online-only detection.** On the Mac, `/usr/bin/stat -f %f <an online-only file>` must have bit `0x40000000` set (`stat -f %f` prints decimal; check with a known online-only file and a downloaded one). Use `/usr/bin/stat` because another `stat` earlier in the path would behave differently.
- **`brctl download` and `brctl evict` while a file is still uploading.** An evict of a file that iCloud has not finished uploading may be refused; the program retries the evict every tick for 7 days (`status` shows `to release on the Mac`) but the real behaviour is unknown.
- **The Mac sleeping in the middle of a copy.** What the sshfs mount returns then (an error that looks like a broken disk, or a hang) is unknown; the program turns such errors into "Mac away" only when a quick `ssh true` fails too.
- **The Mac's free disk stays flat during trial 3.** Converted online-only files must be released again; if the free space shrinks, look at the `to release on the Mac` count.
