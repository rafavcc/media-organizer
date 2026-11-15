# Media organizer

A service that runs on the Windows 11 PC (inside Ubuntu in WSL2) and makes your photos and videos smaller. It has three rules:

1. **Compress in place.** Every video and image in the D: library (`D:\7_XYZ`) and in the iCloud `MEGA` folder on the Mac is converted next to the original: videos to H.265 `.mp4`, images to `.webp`. The new file is named `<name>_compressed.<ext>`. Resolution is never lowered.
2. **Downloads are swept.** Every 6 hours it takes the images and videos from the top level of the Windows Downloads folder and the Mac Downloads folder (not subfolders). A video bigger than 400 MB goes to `D:\7_XYZ\_TO_CONVERT`; every other video and all images go to `MEGA/TBC`, compressed on the way (`.webp` files are only moved there, never converted).
3. **Originals are deleted** once the new file is verified. There is no backup and no Recycle Bin. A file is only replaced when all of this is true:
   - the new file is at most 75% of the original size (otherwise nothing is gained and the original stays);
   - it has the same width and height, the same number of audio tracks and (video) the same length within 1 second; images must also look the same (similarity score at least 0.95);
   - the new file is in its final place with the right size, and the original has not been changed since the program read it.

The recipes (quality settings and checks) are the same as in the old `app_compactor` tool. Two web pages and a log show progress: see "Daily use".

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

   This changes nothing and writes nothing to the real database. It prints lines like `WOULD QUEUE d Some/Folder/clip.mp4 812.3 MB` and `WOULD MOVE win_dl big.mkv -> ...`, then one summary per place: `d: 1488 would queue, 0 would move`. The four places are `d` (D: library), `mega`, `win_dl` (Windows Downloads) and `mac_dl` (Mac Downloads). What to look for:
   - `d: 0 would queue` when you know D: is full of videos means D: is not mounted. Stop and fix that.
   - `SKIP mega (unreachable)` or `SKIP mac_dl (unreachable)` is what you see unless the Mac is already mounted: a dry run never mounts it. To preview MEGA, run `ssh mac true`, then mount it as the old README describes and run the dry run again:

     ```bash
     mkdir -p ~/.local/share/media-organizer/mac
     sshfs mac: ~/.local/share/media-organizer/mac -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3
     ```

     (This is the same mount the service makes; the dry run only recognises a Mac mounted at exactly that place. The old README mounts it elsewhere, which does not help here.) Unmount afterwards with `fusermount -u ~/.local/share/media-organizer/mac`. Do not start the service for this: the service deletes originals.
   - Names in `_compressed` and files in subfolders of Downloads must not appear.

4. **Stop the old cron job** (`pipeline.py`) so two programs never work on `_TO_CONVERT` at once:

   ```bash
   crontab -l > ~/crontab-before.txt   # save a copy first, for an exact rollback
   crontab -l          # shows your scheduled jobs; find the line containing pipeline.py
   crontab -e          # opens them in an editor; delete that line (or put # in front), save, exit
   crontab -l          # check the line is gone
   ```

   **Rollback:** stop the service (`systemctl --user disable --now media-organizer`), run `crontab -e` again and put the line back (your saved copy is `~/crontab-before.txt`). Keep the old scripts in place for a month as a fallback.

5. **Staged trials on copies (do not skip).** The program deletes originals, and it has not yet been run on real data (see "Status of this release"). Run it first against throw-away copies, with its own settings file and its own state folder, so the real database stays untouched. Create `~/media-organizer-trial.toml`:

   ```toml
   d_root = "/mnt/d/_trial"
   win_downloads = "/mnt/c/Users/rafavcc/trial-downloads"
   mac_mega = "Library/Mobile Documents/com~apple~CloudDocs/MEGA/MEGA_TRIAL"
   mac_downloads = "Downloads/trial"
   mega_intake = "TBC"
   state_dir = "~/.local/share/media-organizer-trial"
   web_port = 8766
   web_password = "trial"
   ```

   Paths of the Windows side (`d_root`, `win_downloads`, `state_dir`) are full Linux paths; `mac_mega` and `mac_downloads` are relative to the Mac's home folder; `mega_intake` is a folder inside `mac_mega`. Run a trial in the foreground:

   ```bash
   python3 -m media_organizer.cli --config ~/media-organizer-trial.toml run --no-web
   ```

   Press Ctrl+C to stop it: the current item is put back in the queue (or finished) and nothing is lost. In another terminal, `python3 -m media_organizer.cli --config ~/media-organizer-trial.toml status` shows progress. Files must be at least 10 minutes old to be picked up. Afterwards check: `status` shows no unexpected errors; each converted file plays (on the iPhone and the Mac); and an original is gone only where its `_compressed` file exists. Do the four trials in this order, changing the trial config each time:

   1. **A copied folder** of about 10 mixed files (videos and images) at `/mnt/d/_trial/` (`d_root` as above). Copy them with Windows Explorer or `cp -r`.
   2. **One person's folder on D:**: set `d_root = "/mnt/d/7_XYZ/<that folder>"` (the real files, so first make sure you have a backup of it).
   3. **One MEGA subfolder with online-only files:** set `mac_mega` to that subfolder (for example `.../MEGA/MEGA_TRIAL`). Check the Mac's free disk space before and after (it should not grow by more than a few files).
   4. **One file through each Downloads route:** put into the trial `win_downloads` (and the Mac's `Downloads/trial`) a video over 400 MB, a small video and an image, and check that the big video lands in `_trial/_TO_CONVERT` and the others in the trial MEGA `TBC` folder.

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
| `media-organizer status` | Summary: first-pass progress, space freed, D: free space, queue, what is encoding now, last scan of each place, problem counts |
| `media-organizer status --errors` | Lists the files in error, with the error text |
| `media-organizer status --json` | The same data as JSON |
| `media-organizer pause` | Stop starting new items; an encode already running finishes. The service keeps running |
| `media-organizer resume` | Carry on |
| `media-organizer retry-errors` | Put every file in `error` back in the queue |
| `media-organizer scan --dry-run` | Preview, as in the first run |

Sample `status`:

```
First pass   D: library   412 / 1,496 videos   freed 231.4 GB   D: free 257 GB
             MEGA          3,120 / 9,870 files  freed  18.2 GB
Queue        2 Downloads · 0 inbox · 6,750 MEGA · 1,084 library
Now          encoding "clip_1080p.mp4" (2.1 GB)  37%
Last scans   D: intake 12:00 ok · Win Downloads 12:00 ok · Mac Downloads 12:00 unreachable, retry 13:00
Problems     3 errors · 222 no-gain · 7 low-quality      (media-organizer status --errors)
```

Other things you can do:

- **Dashboard:** `http://localhost:8765` on the PC, `http://192.168.0.33:8765` from other devices. Read-only: space freed, D: free space, milestones, progress, a chart, the biggest folders, recent conversions and problems. It still works when the Mac is off (MEGA figures are from the last scan). Pause, resume and retry are only available on the command line.
- **Log:** `~/.local/share/media-organizer/organizer.log` (rotated, 5 files of 5 MB). Follow it with `tail -f ~/.local/share/media-organizer/organizer.log`, or `journalctl --user -u media-organizer -f`.
- **Service:** `systemctl --user stop|start|restart media-organizer`. Stop waits up to 60 seconds for the running encode to be put back cleanly.
- **When things are scanned:** the intake folders (`D:\_TO_CONVERT`, `MEGA/TBC`) and both Downloads folders every 6 hours; both whole libraries once a week; Mac scans are retried every hour while the Mac is away.
- Files modified in the last 10 minutes, empty files, files starting with `.` and `.part`, `.crdownload`, `.download` files are ignored until they settle.

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
3. General: choose **Run whether user is logged on or not** (so no console window appears) and tick **Hidden**. Trigger: **At log on**. Action: **Start a program**, program `wsl.exe`, arguments `-d Ubuntu --exec /bin/sleep infinity` (use your distro name).
4. Conditions: untick "Start the task only if the computer is on AC power" if this is a laptop. Settings: untick "Stop the task if it runs longer than".

Together with `loginctl enable-linger $USER` (from first-run step 6), the service then starts with WSL and survives closing every terminal. To test, run `wsl --shutdown`, wait a few seconds, start the task from Task Scheduler (right-click, Run) and check `systemctl --user status media-organizer` in a new Ubuntu window.

## What the statuses mean

Each file the program knows has a status, and often a reason. Statuses appear in the database; the dashboard and `status` show problems (`error`, `no-gain`, `low-quality`, `changed`). Files that finish fine and whose result was delivered from Downloads are forgotten afterwards.

| Status | Reason | Meaning | What happens next |
|---|---|---|---|
| `queued` | | Waiting its turn (Downloads first, then the inbox folders, then MEGA, then the D: library; largest first within each) | Processed in time |
| `queued` | `no-space` | Not enough free space on the destination drive to write the result and keep 2 GB spare | Retried later automatically; see "D: nearly full" |
| `running` | | Being converted right now | If the service stops, it goes back to `queued` |
| `published` | | New file written and in place; waiting to delete the original (on the Mac it waits about a minute so iCloud can catch up) | Original deleted when its checks pass |
| `done` | | Converted, original deleted | Never touched again |
| `done` | `post-step-failed` | Converted, but moving it to `_CONVERTED` or running the sorter failed | Retried automatically; look in the log |
| `skipped` | `efficient` | A video already in AV1, or in H.265 at a bitrate that is already low | Left alone |
| `rejected` | `no-gain` | The result was bigger than 75% of the original | Original kept; not retried unless the file or the settings change |
| `rejected` | `low-quality` | An image's similarity score was below 0.95 | Original kept; same |
| `rejected` | `changed` | The original changed while it was being processed | The file is queued again if a later scan sees it changed; the new file is kept |
| `error` | | Something failed (the message is in `status --errors`) | Fix the cause, then `media-organizer retry-errors` |

## When something goes wrong

- **The service stopped, or WSL or the PC restarted mid-encode.** Nothing is lost. At the next start, files marked running go back to the queue, the temporary work folder is emptied, and the encode starts again from the beginning. A half-written `.mo-part` file left in a library is deleted by the next weekly whole-library scan once it is old. Originals are only deleted after the checks, so an interrupted file is simply tried again. If a leftover `*_compressed` file and its original both exist, the next pass handles them; do not delete either by hand while the service is running.
- **The Mac is away (asleep, off, or off the network).** Nothing is flagged or deleted. Files that need the Mac stay `queued`, and the Mac scans are retried every hour. Work on D: continues meanwhile. When the Mac is back, it carries on by itself. If it never recovers, check `ssh mac true` first, then `mount | grep media-organizer`; a stale mount is cleaned up by the service on its next attempt.
- **D: is nearly full.** The program will not write a result if less than the result's size plus `min_free_gb` (2 GB) would be free on the destination; the file waits as `queued` / `no-space`. Each converted file frees its original's space once the original is deleted. If D: is completely full, free a few GB by hand (for example delete files you do not need) and the queue resumes.
- **The database was lost or deleted** (`~/.local/share/media-organizer/organizer.db`). Restart the service (`systemctl --user restart media-organizer`); it creates a new empty database and starts over by scanning. Optionally run `media-organizer import-legacy` first to re-import the old tool's rejections. Files already ending in `_compressed` are never processed again, so nothing is compressed twice. Files that were rejected before (the original is still there) are simply tried once more and rejected again; this wastes time but is harmless. Only a conversion interrupted at exactly the wrong moment could leave one extra copy, named like `a_jpg_compressed.webp` next to `a_compressed.webp`; delete the extra by hand.
- **An original came back after it was deleted (iCloud).** The old tool's README describes iCloud restoring files that were created and deleted within seconds. This program writes the new file with a temporary name inside the folder, renames it, and waits before deleting an original on the Mac; but the real iCloud behaviour has not been checked against this program (see "Status of this release"). If you see a file and its `_compressed` copy both back, tell the developer, and do not delete the `_compressed` one until you have checked it plays.
- **Port already in use.** The log says "dashboard disabled: cannot bind port"; the rest of the service still works. Change `web_port` in the settings file and restart.
- **"already running (lock held)".** A second copy was started. Use `systemctl --user status media-organizer` to see the first one.
- **"missing tools: ..."** Install them with `sudo apt install` (see "Before you start").

## What it will never do

- It never goes into subfolders of Downloads: only files directly in the folder.
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

Restart the service after a change: `systemctl --user restart media-organizer`. An unknown key makes the program refuse to start, with the key's name in the message. If you change the quality settings (`crf`, `min_gain_ratio`, `min_ssim`, `hevc_bloat_bpp`), previously rejected files are looked at again, and `import-legacy` skips the old verdicts.

## Status of this release

The automated tests only use temporary folders and tiny generated files. The program has **not yet been run on your real data**. That is why first-run step 5 stages the first runs on copies, in this order:

1. a copied folder of about 10 mixed files;
2. one person's folder on D:;
3. one MEGA subfolder with online-only files (watch the Mac's free disk space before and after);
4. one file through each Downloads route (small video, big video, image; Windows and Mac).

Play the results on the iPhone and the Mac. The real service is enabled only after all four look right. Until then, treat the real libraries as untested, and keep the old scripts for a month.
