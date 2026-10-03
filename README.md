# Leland Tracker

Leland Tracker is a small Discord bot that counts the configured user's messages
and measures observed time in configured voice channels. It stores statistics in
SQLite and is designed to run as a systemd service on the Raspberry Pi.
It requires Python 3.11 or newer; the pinned dependencies were checked on
Debian 13, aarch64, with Python 3.13.

The product scope and measurement definitions are in [DESIGN.md](DESIGN.md).
The implementation interfaces are in [IMPLEMENTATION.md](IMPLEMENTATION.md).
To propose a change, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Configure Discord

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On its **Bot** page, generate a token and put it in the private environment
   file as `DISCORD_TOKEN`.
3. Under **Installation**, enable **Guild Install** and choose the `bot` and
   `applications.commands` scopes. Use the install link to add the bot to the
   configured server.
4. Give it View Channel in the channels to collect from, plus Add Reactions
   wherever reaction mode should work, and Send Messages
   wherever it may reply to a direct mention or repost Evil Leland text. Embed
   Links is needed in a configured report channel. Reposting inside threads
   additionally needs Send Messages in Threads for those threads. Read Message
   History is needed only for `/leland tldr`, in the text channels it may
   summarize.

These portal steps follow [Discord's setup guide](https://docs.discord.com/developers/quick-start/getting-started).
The bot uses the `GUILDS`, `GUILD_MESSAGES`, `GUILD_VOICE_STATES`, and privileged
`GUILD_PRESENCES` and `MESSAGE_CONTENT` Gateway intents. Enable **Presence Intent**
and **Message Content Intent** on the app's **Bot** page in the Developer Portal.
The bot does not need server-members access. It reads message text to create
Evil Leland reposts and checks mention metadata for its fixed mention reply.
Message bodies are not saved to SQLite or logs.

Create a private `.env` file once from `.env.example` and set `DISCORD_TOKEN`, `GUILD_ID`,
`TARGET_USER_ID`, and `OWNER_USER_ID`. Set `ADMIN_USER_IDS` to a comma-separated
list of other trusted Discord user IDs, or leave it blank. Only the owner and
effective extra admins can pause, resume, delete data, or toggle Evil Leland and
reaction modes.
Discord server permissions do not grant tracker control. The target user ID
cannot be listed as an admin.
The configured owner can grant a server member access immediately with
`/leland admin add user:@member`, revoke any extra admin with
`/leland admin remove user_id:ID`, and see the owner and current admins, by display name, username, and ID, with
`/leland admin list`. These replies are private. Grants and revocations are
stored in SQLite and survive restarts. A revocation takes precedence over
`ADMIN_USER_IDS`; the configured owner cannot be revoked. Data deletion retains
admin settings, while restoring an older database backup restores the admin
settings captured in that backup. Keep `OWNER_USER_ID` in the private environment
file as the recovery owner.
The example collects in all text and voice channels the bot
can see. Set `TEXT_CHANNEL_IDS` and `VOICE_CHANNEL_IDS` to comma-separated
channel IDs to narrow collection. Stats and records omit per-channel totals and
names; `/leland where` follows the visibility rules described below.
`/leland company` charts observed time shared with human companions in those
voice channels. Each minute is divided evenly among everyone else present;
time alone is a separate slice. It starts collecting when this update runs,
so earlier companion time cannot be reconstructed.

Commands work anywhere in the configured server when `OUTPUT_CHANNEL_ID` is
blank. Set `PUBLIC_REPORT_CHANNEL_IDS` to comma-separated channel IDs to make
`/leland stats`, `records`, `where`, `company`, `leaderboard`, `trends`, `roast`, `tldr`, and `help` public only when called in those
channels; other channels get private replies. Set it to `*` for public reports
in every channel. The legacy `OUTPUT_CHANNEL_ID` setting restricts all commands to one
channel and makes general reports public there. `/leland online`, `/leland
about`, `/leland version`, and the admin controls are always private.
The report timezone defaults to `America/Costa_Rica`.
`TLDR_MODEL_URL` optionally points `/leland tldr` at a local llama-server
(for example `http://127.0.0.1:8089`); it must be `localhost` or a loopback
address so message text never leaves the machine. Leave it blank to disable TL;DR. See
[Local TL;DR model](#local-tldr-model-optional).

## Quick start (local)

Create the environment file once, then fill in the bot token and IDs. The guard
keeps this command from replacing an existing `.env`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e .
test -e .env || cp .env.example .env
```

Complete [Configure Discord](#configure-discord) above and edit `.env` with the
IDs and token.

The application reads process environment variables; it does not load `.env`
itself. For a local foreground run, load the file and keep runtime data in the
ignored `data/` directory:

```sh
set -a
. ./.env
set +a
mkdir -p data/backups
export DATABASE_PATH=data/tracker.sqlite3
export BACKUP_DIR=data/backups
.venv/bin/flock-cctv
```

The template uses service paths under `/var/lib/flock-cctv`; the overrides
above keep a foreground run's database and backups inside the ignored `data/`
directory. Stop the foreground process with Ctrl-C.

Never add the filled-in environment file to Git. `.gitignore` excludes it,
runtime data, SQLite files, backups, and Python build artifacts.

## Install the systemd service

For a first install, these commands create a dedicated unprivileged service
account and copy the project files into `/opt/flock-cctv`. Run them
from this checkout. Use the update section below after the first install.

```sh
sudo apt update
sudo apt install -y git rsync python3 python3-venv python3-pip sqlite3
sudo useradd --system --home-dir /var/lib/flock-cctv --shell /usr/sbin/nologin flock-cctv
sudo install -d -o root -g root -m 0755 /opt/flock-cctv
sudo rsync -a --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='data/' --exclude='*.sqlite*' ./ /opt/flock-cctv/
sudo chown -R root:root /opt/flock-cctv
sudo python3 -m venv /opt/flock-cctv/.venv
sudo /opt/flock-cctv/.venv/bin/python -m pip install -c /opt/flock-cctv/requirements.lock -e /opt/flock-cctv
sudo install -d -o flock-cctv -g flock-cctv -m 0750 /var/lib/flock-cctv
sudo install -d -o flock-cctv -g flock-cctv -m 0750 /var/lib/flock-cctv/backups
```

Create the service's private environment file from the template only if it does
not already exist, then edit it with the real token and IDs. The guard preserves
an existing configuration during repeat installs:

```sh
sudo test -e /etc/flock-cctv.env || sudo install -o root -g flock-cctv -m 0640 /opt/flock-cctv/.env.example /etc/flock-cctv.env
sudoedit /etc/flock-cctv.env
```

The template sets the database and managed backup directory under
`/var/lib/flock-cctv`. systemd creates that state directory with access for
the service account. Install the recovery helper outside the code tree, then
install and start the service:

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 /opt/flock-cctv/deploy/update.py /usr/local/libexec/flock-cctv-update.py
sudo install -o root -g root -m 0644 /opt/flock-cctv/deploy/flock-cctv.service /etc/systemd/system/flock-cctv.service
sudo systemctl daemon-reload
sudo systemctl enable --now flock-cctv.service
sudo systemctl status flock-cctv.service
```

The first startup registers guild-scoped slash commands. Check the service
logs, then run `/leland about` in the configured server:

```sh
sudo journalctl -u flock-cctv.service -n 100 --no-pager
sudo journalctl -u flock-cctv.service -f
```

The bot makes an outbound Discord Gateway connection. It does not require a
public web server, inbound port, or port forwarding.

The bot mirrors the target's current server avatar with inverted colours. It
checks when it connects and then every hour. It also refreshes the avatar if the
bot profile was changed manually; animated source avatars use their first frame.
This task runs independently of collection pause and data deletion. A small
marker file beside the database prevents repeating an unchanged profile edit.

## Local TL;DR model (optional)

`/leland tldr` sends up to 40 of Leland's latest message texts to a model
running on the same machine. Nothing leaves the Pi, and neither the texts nor
the summary are stored or logged.

The recommended model is **Qwen3-4B-Instruct-2507** at `Q4_K_M` quantization
(about 2.5 GB). On a Pi 5 it needs about 3.4 GB of RAM and takes roughly
45–60 seconds for a typical week and up to about two and a half minutes when
every message is long. The bot caps the prompt at 40 messages and 4,000
characters. Smaller models (Llama 3.2 3B, Qwen2.5 1.5B/3B) are faster but
produce flatter summaries. Use the Pi 5's active cooler: a TL;DR is a short
burst of full CPU load, and long back-to-back runs can reach the 80 °C soft
throttling limit.

Build llama.cpp's `llama-server` and install it under `/opt/llama.cpp`:

```sh
sudo apt install -y build-essential cmake git
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF
cmake --build ~/llama.cpp/build --config Release -j3 --target llama-server
sudo install -D -m 0755 ~/llama.cpp/build/bin/llama-server /opt/llama.cpp/bin/llama-server
```

The static build produces one self-contained binary. The build takes roughly
15–20 minutes on a Pi 5.

Download the model and install it:

```sh
curl -L -o model.gguf https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
sudo install -d -m 0700 /var/lib/private
sudo install -d -m 0755 /var/lib/private/flock-cctv-llm
sudo install -m 0644 ./model.gguf /var/lib/private/flock-cctv-llm/model.gguf
```

With `DynamicUser=yes`, systemd keeps the state directory at
`/var/lib/private/flock-cctv-llm` and exposes it to the service as
`/var/lib/flock-cctv-llm`, so the unit reads
`/var/lib/flock-cctv-llm/model.gguf`. Install and start the unit, then
enable TL;DR in the bot's environment:

```sh
sudo install -o root -g root -m 0644 deploy/flock-cctv-llm.service /etc/systemd/system/flock-cctv-llm.service
sudo systemctl daemon-reload
sudo systemctl enable --now flock-cctv-llm.service
curl -s http://127.0.0.1:8089/health
sudoedit /etc/flock-cctv.env   # set TLDR_MODEL_URL=http://127.0.0.1:8089
sudo systemctl restart flock-cctv.service
```

The server listens only on `127.0.0.1`, runs at lower CPU priority, and is
capped at 4.5 GB of memory. It loads the model into RAM rather than memory-mapping it and
disables thinking mode and the extra RAM prompt cache.

## Tests

Run the standard-library test suite with:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## Back up and restore

The bot uses SQLite's backup API for managed daily backups and retains the
seven most recent daily copies. These backups are stored in the configured
`BACKUP_DIR`. Make sure the Pi has enough free space and that its clock stays
synchronized. Backups run at startup and every 24 hours thereafter.

To make an additional consistent copy while the service is running, use
SQLite's backup API:

```sh
sudo -u flock-cctv python3 - <<'PY'
import sqlite3

source = sqlite3.connect("/var/lib/flock-cctv/tracker.sqlite3")
destination = sqlite3.connect("/var/lib/flock-cctv/backups/flock-cctv-manual.sqlite3")
source.backup(destination)
destination.close()
source.close()
PY
sudo sqlite3 -readonly /var/lib/flock-cctv/backups/flock-cctv-manual.sqlite3 'PRAGMA integrity_check;'
```

Before restoring, check the chosen backup and proceed only if SQLite reports
`ok`:

```sh
sudo sqlite3 -readonly /var/lib/flock-cctv/backups/flock-cctv-manual.sqlite3 'PRAGMA integrity_check;'
```

Then stop the bot, preserve the current database, replace it with the checked
backup, and verify the restored database:

```sh
sudo systemctl stop flock-cctv.service
sudo -u flock-cctv python3 - <<'PY'
import sqlite3

source = sqlite3.connect("/var/lib/flock-cctv/tracker.sqlite3")
destination = sqlite3.connect("/var/lib/flock-cctv/backups/flock-cctv-before-restore.sqlite3")
source.backup(destination)
destination.close()
source.close()
PY
sudo rm -f /var/lib/flock-cctv/tracker.sqlite3-wal /var/lib/flock-cctv/tracker.sqlite3-shm
sudo install -o flock-cctv -g flock-cctv -m 0640 /var/lib/flock-cctv/backups/flock-cctv-manual.sqlite3 /var/lib/flock-cctv/tracker.sqlite3
sudo sqlite3 -readonly /var/lib/flock-cctv/tracker.sqlite3 'PRAGMA integrity_check;'
```

Start the service only if the restored database check also reports `ok`:

```sh
sudo systemctl start flock-cctv.service
sudo systemctl status flock-cctv.service
```

The two named copies above remain until overwritten or deleted. `/leland delete-data`
removes them, the daily backups, and temporary backup files along with the tracked
statistics. Keep these exact filenames in `BACKUP_DIR` so they remain covered by
deletion. Copies placed elsewhere are operator-managed and need separate deletion.

## Update and rollback

Before an update, note the current revision with `git rev-parse HEAD` and make a
consistent database backup using the SQLite backup snippet above. Then stop the
service before replacing installed files. From the new checkout, copy the
project and install its locked dependencies:

```sh
sudo systemctl stop flock-cctv.service
sudo rsync -a --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='data/' --exclude='*.sqlite*' ./ /opt/flock-cctv/
sudo chown -R root:root /opt/flock-cctv
sudo rm -f /opt/flock-cctv/.deployed-revision
sudo rm -f /opt/flock-cctv/src/flock_cctv/_build.py
sudo /opt/flock-cctv/.venv/bin/python -m pip install -c /opt/flock-cctv/requirements.lock -e /opt/flock-cctv
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 /opt/flock-cctv/deploy/update.py /usr/local/libexec/flock-cctv-update.py
sudo install -o root -g root -m 0644 /opt/flock-cctv/deploy/flock-cctv.service /etc/systemd/system/flock-cctv.service
sudo systemctl daemon-reload
sudo systemctl start flock-cctv.service
sudo systemctl status flock-cctv.service
sudo journalctl -u flock-cctv.service -n 100 --no-pager
```

The commands above refresh the bot unit and recovery helper. To roll back, stop
the service, restore the previous project revision and its matching locked
dependencies, then start it again. If the update changed the database schema,
restore the database backup made before that update as well; older code may not
open a newer schema. Restoring that backup discards activity recorded after it
was taken. Keep code, dependency lock, and database from compatible revisions
together.

## Automatically update from the default branch

The Pi polls this repository's remote default branch (`main` or `master`) every
15 minutes and ten minutes after boot. This runs entirely on the Pi, so GitHub
Actions remains test-only. A push is deployed on the next successful poll. The
Pi needs outbound access to GitHub (SSH) and PyPI.

### Deploy key

The repository is private, so the updater reads it with a dedicated read-only
deploy key. Create the key on the Pi, pin GitHub's SSH host keys, then add the
public key to the repository (**Settings → Deploy keys**, read-only, or with
`gh repo deploy-key add`):

```sh
sudo install -d -o root -g root -m 0700 /etc/flock-cctv-deploy
sudo ssh-keygen -t ed25519 -N '' -C flock-cctv-pi-deploy -f /etc/flock-cctv-deploy/id_ed25519
ssh-keyscan -t ed25519 github.com | sudo tee /etc/flock-cctv-deploy/known_hosts
ssh-keygen -lf /etc/flock-cctv-deploy/known_hosts
sudo cat /etc/flock-cctv-deploy/id_ed25519.pub
```

The fingerprint must be `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`,
GitHub's published ed25519 host key; delete the file and stop if it differs.
The updater unit sets `GIT_SSH_COMMAND` to use only this key and these host
keys. The key never leaves the Pi and grants read access to this one repository.

### Install the timer

First complete the manual update above from a checkout containing the updater.
It installs the stable recovery helper and the bot unit, which starts
`python -m flock_cctv` so a prepared virtual environment can be moved into
`/opt/flock-cctv`. Then install and enable the timer:

```sh
sudo install -o root -g root -m 0644 deploy/flock-cctv-update.service /etc/systemd/system/flock-cctv-update.service
sudo install -o root -g root -m 0644 deploy/flock-cctv-update.timer /etc/systemd/system/flock-cctv-update.timer
sudo install -o root -g root -m 0644 deploy/flock-cctv-update.path /etc/systemd/system/flock-cctv-update.path
sudo systemctl daemon-reload
sudo systemctl enable --now flock-cctv-update.timer flock-cctv-update.path
sudo systemctl start flock-cctv-update.service
sudo systemctl list-timers flock-cctv-update.timer
sudo journalctl -u flock-cctv-update.service -n 100 --no-pager
```

The path unit lets `/leland update` start a check without waiting for the
timer. The bot only writes `update-requested.json` in its state directory;
systemd sees it and starts the same updater service, which deletes the file when
it starts. A request made during a run starts one more run after it. The bot
gets no extra permissions. If you already run the timer, first reinstall the
updater (the `install ... deploy/update.py` step above): an older copy never
deletes the request, so systemd would keep restarting it. Then install the
`flock-cctv-update.path` line and run
`sudo systemctl enable --now flock-cctv-update.path`.

### How an update runs

The updater checks the remote default branch's commit ID. For a new commit, root
fetches it into a temporary directory; only Git runs as root at this stage. The
service account (`flock-cctv`, set with `--stage-user`) then creates a new
virtual environment, installs pinned dependencies, and runs the full test suite,
so repository code never runs as root. The staged tree is then made root-owned,
with setuid/setgid and group/world write bits removed. Only after those steps
pass does it stop the bot, save a consistent SQLite backup in `BACKUP_DIR`,
replace the installed code, and start the new bot.

The new version must stay running and write a fresh `bot-ready.json` marker
(beside the database) when it connects to Discord within three minutes, then
stay up without restarting for 15 more seconds. Otherwise the updater restores
the previous code at `/opt/flock-cctv.previous` and the pre-update database
backup, unless tracking data was deleted during the startup check. In that case
it leaves the deletion in place and reports the rollback problem. A failed
candidate may lose activity observed during its brief startup check when the
pre-update database is restored.

A root-owned pending record beside the installed tree lets the bot's startup
helper recover an interrupted swap before the bot starts, including after a
power loss. A bad network connection is retried on the next poll. A commit that
fails to install, test, or start is retried after six hours or when a newer
commit arrives, so a broken commit does not restart the bot every 15 minutes.
Allow space for two code trees, two virtual environments, and a database backup.
The deployed commit is recorded in `/opt/flock-cctv/.deployed-revision`.

### Failure alerts

After every run the updater writes `update-status.json` beside the database: the
result, deployed commit, consecutive failures, and a short error. It holds no
statistics or credentials. `/leland about` shows it, and the bot sends the
configured owner one DM when a run of failures starts (not on every failed
poll). If the owner's DMs are closed, only `/leland about` and the journal
show it.

### Roll back to an older commit

To deploy a specific commit through the same staged, tested, health-checked path
and stop polling there:

```sh
sudo systemctl stop flock-cctv-update.timer
sudo GIT_SSH_COMMAND="ssh -i /etc/flock-cctv-deploy/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/flock-cctv-deploy/known_hosts" \
  /usr/local/libexec/flock-cctv-update.py --repository git@github.com:jose9706/FlockCCTV.git --revision <full-commit-id>
sudo systemctl start flock-cctv-update.timer
```

The hold survives restarts and `/leland about` shows it. Resume normal
updates with the same command using `--release` instead of `--revision ...`.
If the older commit cannot open a newer database schema, its startup check fails
and the updater rolls back to the current code.

### Checking what is running

The release number lives in `src/flock_cctv/__init__.py` (`__version__`,
semantic versioning) and is bumped by hand in the pull request that changes
behavior. The updater also stamps the deployed commit into the installed package,
so the running version is `release (short commit)`, for example `0.2.0 (1a2b3c4)`:

- `/leland version` replies privately with it; `/leland about` also shows it on its first line.
- The service journal logs `Starting flock-cctv <version>` at every start.
- `/opt/flock-cctv/.venv/bin/python -m flock_cctv --version` prints it
  without needing the bot's environment file.

A manual install has no updater stamp (the manual steps delete any old one), so
it reports `revision unknown` unless run from a git checkout; the updater
replaces it with a stamped copy on its next run because the manual steps remove
`.deployed-revision`.

The timer runs its own copy of the updater in `/usr/local/libexec`, which
auto-updates never replace. Until you reinstall it with the `install ...
deploy/update.py` step above, auto-deploys report `revision unknown`;
`/opt/flock-cctv/.deployed-revision` still holds the true commit.

The backup is `flock-cctv-before-update.sqlite3` in `BACKUP_DIR`; it is
replaced at the next update and `/leland delete-data` removes it. Temporary
SQLite backups from an interrupted update are also removed by that command.
The bot token and database stay outside the installed code tree. If you use
nondefault `DATABASE_PATH` or `BACKUP_DIR`, add matching `--database` and
`--backups` arguments to both the updater service's `ExecStart` and the bot
service's `ExecStartPre`.

To disable polling, run `sudo systemctl disable --now flock-cctv-update.timer flock-cctv-update.path`.
Changing systemd units, the recovery helper, or `/etc/flock-cctv.env` still
requires a manual installation step. Review changes to the default branch before
merging: merged code installs automatically and runs as the service account,
though never as root.

## Troubleshooting

- **Service exits on startup:** check `systemctl status` and the journal above.
  Common causes are a missing or invalid token/ID, invalid timezone, missing
  Python dependencies, or state-directory permissions.
- **Slash commands are missing:** check the startup log and configured guild ID;
  confirm the app was installed with `bot` and `applications.commands` scopes.
  Presence and Message Content intents must be enabled in the portal.
- **Counts or voice time stop changing:** check `/leland about`, confirm
  collection is not paused, and check the channel allowlists and View Channel
  access. Voice time is observed connection time, and the server AFK channel is
  excluded.
- **Database identity or timezone error:** use the original `GUILD_ID`,
  `TARGET_USER_ID`, and `TIMEZONE` for that database. Changing timezone requires
  a fresh database; it does not regroup existing statistics.
- **Database instance lock:** run only one bot process against a database. Stop
  a duplicate foreground process or service; do not delete the `.lock` file to
  bypass the lock.
- **Evil Leland does not repost:** confirm the mode is on, collection is
  unpaused, the message is in a collected text channel, and the bot can send
  messages there. It reposts text only; text is still reposted when the source
  message also has attachments. Mentions and embeds are suppressed.
- **The bot does not answer a direct mention:** replies work independently of
  collection pause and channel allowlists, but the bot must receive the server
  message and have permission to send in that channel.
- **Avatar does not refresh:** check that the target is still in the configured
  server and inspect the service journal for avatar update errors.
- **TL;DR says the kitchen is closed:** the local model did not answer. Check
  `systemctl status flock-cctv-llm.service`, its journal, and
  `curl -s http://127.0.0.1:8089/health`; confirm `TLDR_MODEL_URL` matches the
  server's host and port. The first request after a start can be slow while the
  model loads.

## Commands

- `/leland stats` reports message totals, observed voice time, active days,
  voice visits, and missing coverage for today, this week, this month, or all
  time. The default period is this week.
- `/leland records` shows the busiest message day, the longest fully observed
  voice visit, how long a visit in progress has been observed so far, and the
  top voice companion since companion tracking began (same split-time measure
  and channel visibility rules as `/leland company`).
- `/leland where` shows the last observed voice channel and time, or that Leland
  is currently in voice. It is public in configured report channels and private
  elsewhere. A public reply names the voice channel only if the `@everyone` role
  can view it;
  a private reply names it only if the requester can view it.
- `/leland company` attaches a pie chart of observed voice time with each person
  or alone for today, this week, this month, or all time. The default is this
  week. Its image legend and text list show names, percentages, and durations.
  Names are looked up from Discord when absent from the bot's cache; if Discord
  cannot provide a name, the report shows the user ID. Names are not stored.
  The chart includes only source voice channels visible to the requester
  for private replies or to `@everyone` for public replies. Time lost during
  outages is never estimated. Member IDs and daily attributed totals are stored
  in SQLite and managed backups; `/leland delete-data` erases them.
- `/leland leaderboard` ranks the top 10 people by the whole observed voice
  time they spent in a tracked channel with Leland. Unlike `/leland company`,
  time is not split: an hour in a call with three people counts as an hour for
  each of them. It uses the same channel visibility rules and name lookup as
  `/leland company`. Time alone is shown but not ranked, and anyone past the
  top 10 is counted on one line. Periods are today, this week, this month, or
  all time; the default is all time. Company time recorded before whole shared
  time was tracked counts as its split share, since the group size at the time
  was not stored.
- `/leland trends period: kind:` attaches a chart of how activity changes. The
  default period is the last 7 days (today and the six days before it); pick
  `This week` to start on Monday instead. Kinds:
  - `daily` (default): messages and observed voice time per day (grouped by
    week or month for long periods), busiest day, active-day streaks, and ghost
    days. A ghost day is a finished day the tracker watched in full with no
    messages or voice; days it was disconnected or paused are never ghost days.
  - `compare`: this period so far against the previous day, week, 7 days, or month up
    to the same point, from retained detail. It declines for all time, when
    tracking began during the previous period, or when that period is older
    than `RETENTION_DAYS`, and notes when either window was not fully watched.
  - `hours`: messages and observed voice time by local hour, with peak hours
    and the 00:00–05:00 share.
  - `weekdays`: average messages and voice time per weekday, over days with
    activity or fully watched by the tracker; unwatched quiet days are left out.
  - `company`: stacked bars of companion time per day, week, or month with the
    top companion for recent buckets, under the same channel visibility rules
    and name lookup as `/leland company`.
  - `bursts`: runs of messages sent within two minutes of each other, with the
    biggest burst, the average size, and the share in bursts of five or more.
  `hours`, `bursts`, and `compare` need message send times or voice sessions,
  which exist only within `RETENTION_DAYS`; replies say when a period reaches
  past that. Other kinds use daily totals, which match `/leland stats` and do
  not filter by channel. Unobserved time is never filled in.
- `/leland online` checks Leland's current Discord status. Online, Away, and Do
  Not Disturb count as online. Offline may also mean Invisible. The reply is
  private and no presence history is stored.
- `/leland roast` produces a short joke from a real statistic and uses a
  shared cooldown.
- `/leland tldr` posts an AI-cooked, playful summary of Leland's latest messages
  (this week by default, up to 40 messages). It fetches their text from Discord
  on demand, only from channels and public threads visible to the requester for
  private replies or to `@everyone` for public replies (never private threads),
  and only messages the tracker
  counted in the period. It runs on the local model set by `TLDR_MODEL_URL`,
  stores nothing, is off while collection is paused, handles one request at a
  time, and has a shared one-minute cooldown.
- `/leland help` explains the measurements and available commands.
- `/leland about` privately shows the bot version, connection health, collector
  state, last checkpoint, recorded coverage gaps, and the last automatic update
  result. All commands live under `/leland`; the old `/tracker` group was merged
  into it in 0.5.0 and disappears from Discord when the bot next syncs commands
  at startup.
- `/leland version` privately shows the release number and deployed commit.
- `/leland update` lets the configured owner and extra admins make the Pi check
  GitHub for a new commit now instead of waiting for the 15-minute poll. It
  needs the update path unit from "Install the timer" below; without it the
  request is picked up at the next scheduled poll.
- `/leland pause`, `/leland resume`, and `/leland delete-data` control
  collection. Only the configured owner and extra admins can use these controls.
  Data deletion requires an ephemeral confirmation and checks access again.
- `/leland evil-mode mode:on/off` lets those admins enable or disable Evil Leland
  mode. When on, the bot posts upside-down Unicode versions of Leland's new text
  messages in the same channel. It does not repost attachment files, though text
  accompanying an attachment is still reposted. Empty text is skipped. Mentions
  and embeds are suppressed, and message bodies are not stored. Pausing collection
  stops reposts; deleting data turns this mode off. The toggle persists across
  restarts.
- `/leland reaction-mode mode:on/off` lets those admins enable or disable
  occasional reactions to Leland's newly counted ordinary messages. When on,
  the bot picks a new interval of 15–25 messages, then reacts with either 😂 or
  👸 and picks another interval. The setting and current interval survive a
  restart. Pausing collection stops the counter; deleting data turns it off.
  The bot needs Add Reactions permission in tracked text channels. Reactions
  may be missed if Discord rejects one or the bot stops between counting and
  reacting.
- `/leland admin add user:@member`, `/leland admin remove user_id:ID`, and
  `/leland admin list` let only the configured owner manage tracker admins.
  Removal also accepts a user mention and works after someone leaves the server.
  The list and the add/remove confirmations show each person's display name and
  username next to their ID, or "Unknown user" when Discord cannot resolve the
  account.

When someone directly mentions the bot in the configured server, it responds
with “I am evil Leland, more gay than the original”. This reply works whether
Evil Leland mode is on or off, even while collection is paused or the channel is
outside the text allowlist, and never mentions anyone.

Voice time measures observed connection time, not speaking. Visits already in
progress when observation begins are marked incomplete and cannot set the
longest-visit record. A brief tracker reconnect or restart (up to two
minutes) while Leland stays in the same channel does not split his visit; the
unobserved interval is excluded from its length. See [DESIGN.md](DESIGN.md) for coverage and recovery rules.
