# chat-backup

Keeps a local, browsable copy of every chat in your ChatGPT account, including
chats you later delete. It runs 24/7 in Docker on a machine at home, checks
ChatGPT every 2 to 15 minutes (more often while you are using it), writes each chat as plain files, mirrors the
archive to Google Drive, and sends a push notification to your phone when it
needs you.

What you get, for every chat:

```
data/archive/
  INDEX.md                                   every chat: date, title (link), project, status
  index.csv                                  the same, for spreadsheets
  2026-09/2026-09-13_planning-a-trip_a1b2c3d4/
    transcript.md                            readable transcript, edit branches included
    conversation.json                        the raw data, exactly as ChatGPT returned it
    files/                                   uploaded files and generated images
    canvas/                                  Canvas documents and code
    DELETED.txt                              appears when ChatGPT no longer has the chat
```

Nothing is ever removed from the archive. A chat you delete in ChatGPT stays
here, marked as deleted.

## Read this first

- This uses ChatGPT's own internal web API, the same one the website uses. It
  is not an official API, OpenAI's terms frown on automated access, and it can
  change without notice. The service only reads; it never posts or deletes.
- The browser has to run on your **home internet**. ChatGPT's Cloudflare
  protection challenges datacenter and VPN addresses even when you are logged
  in, so a cloud VM does not work for this.
- Every few weeks the saved login expires. You get a phone alert, run a
  one-minute script on your laptop, copy one file, done. That is the whole
  maintenance burden.

## What you need

- A machine at home that stays on: any x86 mini PC, old laptop or desktop with
  4 GB RAM or more, running Linux with Docker Engine and the Compose plugin.
- Your laptop, with Python 3.10 or newer, for the login step.
- The free ntfy app on your phone (alerts) and a Google account (mirror).

## Setup

Do the steps in order. Each ends with a check; do not move on until it passes.

### 1. Laptop: save a login

```bash
pip install playwright==1.62.0
playwright install chromium
python tools/export_session.py
```

A browser window opens. Log in to ChatGPT as usual, then press Enter in the
terminal.

Check: the script prints `OK: session cookie found` and a file
`storage_state.json` exists. Close the browser window; do **not** log out in
it (that would cancel the session you just saved).

No screen, for example when you are connected over SSH from a phone? Run
`tools/remote-login/run.sh` on the backup machine instead (after step 2's
`docker compose build`). It prints an address and password: open the address in
your phone's browser over Tailscale, log in to ChatGPT there, and the login is
saved into `data/` automatically.

### 2. Backup machine: install and configure

```bash
git clone <this repository> && cd lil-tykes/chat-backup
cp .env.example .env
nano .env            # set TZ and LOCALE to what your laptop uses; leave NTFY_TOPIC empty for now
mkdir -p data
```

Copy `storage_state.json` from the laptop into `data/`, for example with
`scp storage_state.json you@backup-box:lil-tykes/chat-backup/data/`. Then build:

```bash
docker compose build
```

Check: the build finishes without errors. The service runs as user id 1000
inside the container (`user:` in docker-compose.yml), so `data` must belong to
user 1000: check with `id -u`, and change `user:` if yours differs.

### 3. First run

```bash
docker compose run --rm chatbackup --once
```

This checks once, archives up to 8 chats and exits.

Check the output for `seeding cookies from storage_state.json`, then
`token captured` (or `logged in`), then `archived: <title>` lines. Afterwards
`data/archive/INDEX.md` lists chats and a chat folder contains
`transcript.md`, `conversation.json` and, if the chat had any, `files/`.

If it prints "The ChatGPT login has expired or was never imported", the saved
login did not work: repeat step 1.

### 4. Start the service

```bash
docker compose up -d
docker compose logs -f        # Ctrl-C stops the log view, not the service
```

Check: the log shows `archived:` lines, and `full check started` after half an hour
(the first pass through a big account takes a while: a few chats per check, so
several days per 3,000 chats, to stay well under ChatGPT's rate limit, which your own
browser shares; `DOWNLOAD_DAYS` keeps it short, and chats you are actively using are saved first). After a couple of minutes `docker compose ps` shows the container as
`healthy`, and `data/.heartbeat` is refreshed every 30 seconds.

### 5. Phone alerts

1. Install the ntfy app (Android, iPhone) and open it.
2. Make up a long random topic name: `openssl rand -hex 16`. The topic name is
   the only thing protecting your alerts, so treat it like a password.
3. Subscribe to that topic in the app.
4. Test from the backup machine: `curl -d "hello" https://ntfy.sh/<topic>` should
   arrive on the phone.
5. Put the topic in `.env` (`NTFY_TOPIC=...`) and run `docker compose up -d`
   again so the service reads it.

Check: a "chat-backup started" notification arrives.

Optional: set `NOTIFY_NEW_CHATS=true` in `.env` and run `docker compose up -d` to
also get an alert for every new chat; tapping it opens the chat. Only chats
created in the last two hours alert, so older chats found later stay quiet.

### 6. Google Drive mirror

The archive is only on one disk until this step is done.

1. In the Google Cloud console, create a project, enable the **Google Drive
   API**, open **OAuth consent screen**, choose External, fill in the name and
   your email, and **publish** it (an app left in "testing" mode gets logged
   out every week). Then create credentials: **OAuth client ID**, type
   **Desktop app**. Note the client id and secret. This is needed because
   rclone's shared credential is being switched off in 2026.
2. On the backup machine install rclone (`curl https://rclone.org/install.sh | sudo bash`)
   and run `rclone config`: new remote, name `gdrive`, type `drive`, paste the
   client id and secret, scope `drive.file`, no advanced config, and answer
   **n** to "Use web browser to automatically authenticate".
3. It prints a command to run on the laptop: `rclone authorize "drive" ...`.
   Run it there (install rclone on the laptop first), log in to Google, and
   paste the token block it prints back into the backup machine's prompt.
4. Create the folder: `rclone mkdir gdrive:chatgpt-backup`.

Check: `rclone lsd gdrive:` lists the folder.

Storage note: a new Google account gets 5 GB of free space, or 15 GB after
verifying a phone number, shared with Gmail and Photos. A text-heavy archive is
small; generated images add up.

### 7. Mirror and health check on a timer

Edit `host/chat-backup.service`: set `User=` to your Linux user, `APP_DIR=` to
the full path of this folder, and the home directory in `PATH=`. That last line
matters because systemd does not search `~/.local/bin`, so if you installed
rclone there (the `install.sh` above puts it in `/usr/bin`, but a manual install
often does not) the mirror fails with "rclone: not found". If your rclone is
system-wide, you can delete the `PATH=` line instead. Then:

```bash
sudo cp host/chat-backup.service host/chat-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chat-backup.timer
sudo systemctl start chat-backup.service      # run it once by hand
```

Check: `journalctl -u chat-backup` shows rclone transferring files,
`rclone ls gdrive:chatgpt-backup | head` shows them, `host-state/mirror-ok`
exists, and `systemctl list-timers chat-backup.timer` shows the next run.

### 8. Try the whole thing

- Start a throwaway chat in ChatGPT. Within about 15 minutes the log shows
  `new:` and `archived:` for it and its folder appears.
- Edit one of your messages in it. The transcript gains an "Alternate branches"
  section.
- Delete the chat. After the next full check (up to a day) the folder gets
  a `DELETED.txt` and the transcript header says so. Nothing is removed.
- Stop the service for 25 minutes (`docker compose stop`) and start it again.
  Your phone gets "poller down", then "poller ok".
- The next morning after 9:00 a low-priority daily summary arrives. That
  summary is the "everything is fine" signal: if it stops coming, look.

### 9. Outside watcher and crash alerts

Every alert so far comes from the backup machine itself, so a machine that
crashes, loses power or loses its internet connection cannot tell you. This step
adds an outside service that notices when the machine goes quiet, and a phone
note after every restart saying whether the machine shut down cleanly.

1. Sign up at [healthchecks.io](https://healthchecks.io) (the free plan monitors
   20 checks). Under **Integrations**, add **ntfy** with your `NTFY_TOPIC` so its
   alerts reach the same phone app (email is on by default).
2. Add a check named `machine`: period **5 minutes**, grace time **10 minutes**.
3. Add a check named `chat-backup`: period **15 minutes**, grace time **45 minutes**.
4. Put each check's ping URL in `.env` as `HC_MACHINE_URL=` and
   `HC_CHAT_BACKUP_URL=`. The URLs work like passwords.
5. Edit `User=` and `APP_DIR=` in `host/chat-backup-heartbeat.service` and
   `host/chat-backup-boot-report.service` as in step 7. The boot report reads the
   system journal, so that user must be in the `adm` or `systemd-journal` group
   (`id` lists your groups). Then:

```bash
sudo cp host/chat-backup-heartbeat.service host/chat-backup-heartbeat.timer \
        host/chat-backup-boot-report.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chat-backup-heartbeat.timer
sudo systemctl enable chat-backup-boot-report.service
```

Check: both checks on healthchecks.io turn green within 15 minutes, and
`sudo systemctl start chat-backup-boot-report.service` sends a phone note about
the last restart.

Two settings let the machine recover without you:

- **After a power cut:** in the BIOS (F2 at power-on), set what happens when power
  returns (on Dell: Power Management, AC Recovery) to **Power On**. Otherwise the
  machine stays off until someone presses the button.
- **After a freeze:** Intel machines have a hardware watchdog that restarts the
  machine when the system stops responding. Ubuntu leaves its driver off. To use it:

```bash
sudo modprobe iTCO_wdt && ls /dev/watchdog0     # must list /dev/watchdog0; stop here if it does not
echo iTCO_wdt | sudo tee /etc/modules-load.d/iTCO_wdt.conf
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=1min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf
sudo systemctl daemon-reexec
```

Check: `systemctl show -p RuntimeWatchdogUSec` prints `RuntimeWatchdogUSec=1min`.

## Everyday use

The archive is a folder. Open `INDEX.md` in any Markdown viewer (Obsidian, VS
Code, the Google Drive preview) and click a title. Search it with your
editor's search or `grep -ri "phrase" data/archive`.

### Alerts and what to do

| Alert | Meaning | What to do |
|---|---|---|
| `login` | the saved ChatGPT session expired | Step 1 on the laptop, copy the file into `data/`. The service notices within 10 minutes; `docker compose restart` makes it immediate. |
| `challenge` | Cloudflare keeps challenging the browser | Usually clears by itself. If it lasts hours, make sure the machine is not on a VPN and has your normal home address. |
| `api_errors` | several checks in a row failed | `docker compose logs --tail 100`. If ChatGPT changed its internals, the code needs updating. |
| `rate_limited` | ChatGPT has refused requests for over 30 minutes | Usually nothing: the service backs off by itself (quick checks after 15, 30, then every 60 minutes) and returns to normal when ChatGPT answers. If it lasts for hours, give it a rest (see Things to know). |
| `chat-backup resting` / `resumed` | a planned break started, or ChatGPT answered again after it | Nothing. |
| `disk` | under 2 GB free on the archive disk | Make room. |
| `poller down` | no heartbeat for 10 minutes (sent by the host script) | `docker compose ps`, `docker compose logs --tail 100`. |
| `chatgpt down` | the service runs, but no ChatGPT check has succeeded for two hours (sent by the host script) | `docker compose logs --tail 100`: look for `browser start failed`, `login` or `rate limited`. |
| `mirror down` | rclone has not succeeded for 2 hours | `journalctl -u chat-backup --since -3h`. Often an expired Google token: repeat step 6.3. |
| daily summary | everything is running | Nothing. Its absence is the alarm. |
| `New ChatGPT chat` | a chat was just created (only with `NOTIFY_NEW_CHATS=true`) | Nothing; tap it to open the chat. |
| healthchecks.io: `machine` down | the machine has not checked in for 15 minutes: off, crashed or offline (step 9) | Go and look at it. A crash note follows once it is back. |
| healthchecks.io: `chat-backup` down | the host health check has not reported "all fine" for an hour (step 9) | Read the other alerts. With none, check `systemctl list-timers chat-backup.timer`. |
| `<machine> crashed or lost power` | sent at boot: the previous run ended without a shutdown (step 9) | Check that chat-backup recovered: `docker compose ps`, `docker compose logs --tail 50`. |
| `<machine> restarted` | sent at boot after a clean restart (step 9) | Nothing. |

Every alert is sent once when the problem starts and once when it is over.

### Things to know

- Run `docker compose stop` before `docker compose run --rm chatbackup --once`.
  Two browsers cannot share one profile.
- `DOWNLOAD_DAYS` in `.env` limits downloads to chats created or used in the last
  that many days, which keeps the first run small and far from ChatGPT's rate limit.
  Older chats still appear in `INDEX.md` by title and are downloaded as soon as you
  use them again. `0` downloads your whole history.
- To give ChatGPT's rate limit a break, write a Unix time into `data/rest-until`,
  for example 3 hours from now: `echo $(( $(date +%s) + 10800 )) > data/rest-until`.
  Until then the service sends ChatGPT nothing (its heartbeat keeps going and the host
  script knows about the rest, so there is no "poller down" or "chatgpt down" alert),
  then starts again with quick checks only.
- To watch closely for a while, write a Unix time into `data/fast-until`, for example an
  hour from now: `echo $(( $(date +%s) + 3600 )) > data/fast-until`. Until then quick checks
  run about every minute (from the next check on), then go back to the normal pace by
  themselves. A refused check still backs off. Keep it short: every-minute checks around
  the clock are what used up the rate limit before.
- `QUIET_HOURS` in `.env` (for example `1-8`: 1:00 until 8:00, in your `TZ`) is a rest
  every night: no requests to ChatGPT, and no phone notes about it. Chats you use during
  those hours are saved when they end.
- ChatGPT's rate limit is per account, so it is shared with your own browser: if the
  backup uses it up, ChatGPT shows *you* "Too many requests". That is why the service
  checks slowly while you are not using ChatGPT, and backs off hard when refused.
- Set `LOG_LEVEL=DEBUG` in `.env` to see the first 300 characters of every
  ChatGPT response. That is the first thing to do when ChatGPT changes
  something and the log shows `KeyError` or `bad_body`.
- All times in the archive use the `TZ` from `.env`. Do not change it later
  unless you are happy with mixed timestamps.
- `data/` holds your live session cookies. It is excluded from git; keep it
  that way.
- The archive is never pruned. `history/` inside a chat folder only appears if
  a later copy of the chat had fewer messages than an earlier one, which
  should not happen; it exists so nothing can be lost silently.

## How it works

- A real, visible Chromium (on a virtual display inside the container) stays
  logged in with a persistent profile. Headless browsers get challenged; this
  one looks like a person's browser because it is one.
- It asks ChatGPT for the most recently updated chats and the Projects
  sidebar: every 2 minutes while you are using ChatGPT (a check found a new or
  changed chat in the last 20 minutes), every 15 minutes otherwise, and not at
  all during `QUIET_HOURS`. Anything new or changed is fetched and written: raw
  JSON first, then files, Canvas documents and the transcript. A chat whose
  answer is still being written is fetched again two minutes later.
- Once a day, at a moment you are not using ChatGPT, it lists everything
  (active, archived, and every Project, because the main list leaves Project
  chats out). A chat missing from all lists is fetched one last time if ChatGPT
  still allows it, then marked deleted. A refused full check waits 3 hours.
- A watchdog thread writes `data/.heartbeat` only while the main loop makes
  progress and exits the process if it stalls for 15 minutes; Docker restarts
  it. The browser is also restarted every 6 hours because Chromium leaks
  memory.
- Every chat list ChatGPT answers also writes `data/.last-success`. The host
  script alerts when that is over two hours old, which catches a service that
  runs but cannot do its job, such as a browser that will not start.
- SQLite (`data/chatbackup.sqlite3`) remembers what has been fetched. A
  snapshot of it is mirrored with the archive; the live file is not (it would
  be inconsistent mid-write).

## Limits

- A chat created and deleted between two checks (up to about 20 minutes apart, or
  during `QUIET_HOURS`) can be missed. While ChatGPT is
  rate limiting, a chat deleted before a download succeeds may keep only its title.
- Voice-mode audio is not saved. Files over 50 MB are skipped.
- A chat's first 4 files download together with it. Any more, and any a restart cut off,
  follow a few per minute, and the transcript links them as they arrive.
- Canvas documents are rebuilt by replaying ChatGPT's edits; when an edit
  cannot be replayed, a `.replay-warning.txt` sits next to the file and the raw
  edits stay in `conversation.json`.
- Without step 9, a machine that crashes or loses power cannot alert you; the
  missing daily summary is then the only signal.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use hand-written conversation JSON in `tests/fixtures/` and never
touch the network or a browser.
