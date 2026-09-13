# chat-backup

Keeps a local, browsable copy of every chat in your ChatGPT account, including
chats you later delete. It runs 24/7 in Docker on a machine at home, checks
ChatGPT about once a minute, writes each chat as plain files, mirrors the
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

### 2. Backup machine: install and configure

```bash
git clone <this repository> && cd lil-tykes/chat-backup
cp .env.example .env
nano .env            # set TZ and LOCALE to what your laptop uses; leave NTFY_TOPIC empty for now
mkdir -p data && sudo chown 1000:1000 data
```

Copy `storage_state.json` from the laptop into `data/`, for example with
`scp storage_state.json you@backup-box:lil-tykes/chat-backup/data/`. Then build:

```bash
docker compose build
```

Check: the build finishes without errors. The service runs as user id 1000
inside the container, which is why `data` must be owned by 1000.

### 3. First run

```bash
docker compose run --rm chatbackup --once
```

This checks once, archives up to 20 chats and exits.

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

Check: the log shows `full check started` and a stream of `archived:` lines
(the first full pass through a big account takes a while: about 20 chats per
minute). After a couple of minutes `docker compose ps` shows the container as
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

Edit `host/chat-backup.service`: set `User=` to your Linux user and
`APP_DIR=` to the full path of this folder. Then:

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

- Start a throwaway chat in ChatGPT. Within about two minutes the log shows
  `new:` and `archived:` for it and its folder appears.
- Edit one of your messages in it. The transcript gains an "Alternate branches"
  section.
- Delete the chat. After the next full check (up to 30 minutes) the folder gets
  a `DELETED.txt` and the transcript header says so. Nothing is removed.
- Stop the service for 25 minutes (`docker compose stop`) and start it again.
  Your phone gets "poller down", then "poller ok".
- The next morning after 9:00 a low-priority daily summary arrives. That
  summary is the "everything is fine" signal: if it stops coming, look.

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
| `disk` | under 2 GB free on the archive disk | Make room. |
| `poller down` | no heartbeat for 10 minutes (sent by the host script) | `docker compose ps`, `docker compose logs --tail 100`. |
| `mirror down` | rclone has not succeeded for 2 hours | `journalctl -u chat-backup --since -3h`. Often an expired Google token: repeat step 6.3. |
| daily summary | everything is running | Nothing. Its absence is the alarm. |

Every alert is sent once when the problem starts and once when it is over.

### Things to know

- Run `docker compose stop` before `docker compose run --rm chatbackup --once`.
  Two browsers cannot share one profile.
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
- Every minute it asks ChatGPT for the most recently updated chats and the
  Projects sidebar. Anything new or changed is fetched and written: raw JSON
  first, then files, Canvas documents and the transcript. A chat whose answer
  is still being written is fetched again two minutes later.
- Every 30 minutes it lists everything (active, archived, and every Project,
  because the main list leaves Project chats out). A chat missing from all
  lists is fetched one last time if ChatGPT still allows it, then marked
  deleted.
- A watchdog thread writes `data/.heartbeat` only while the main loop makes
  progress and exits the process if it stalls for 15 minutes; Docker restarts
  it. The browser is also restarted every 6 hours because Chromium leaks
  memory.
- SQLite (`data/chatbackup.sqlite3`) remembers what has been fetched. A
  snapshot of it is mirrored with the archive; the live file is not (it would
  be inconsistent mid-write).

## Limits

- A chat created and deleted within one minute can be missed.
- Voice-mode audio is not saved. Files over 50 MB are skipped.
- Canvas documents are rebuilt by replaying ChatGPT's edits; when an edit
  cannot be replayed, a `.replay-warning.txt` sits next to the file and the raw
  edits stay in `conversation.json`.
- If the backup machine loses power, nothing can alert you. The missing daily
  summary is the signal.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use hand-written conversation JSON in `tests/fixtures/` and never
touch the network or a browser.
