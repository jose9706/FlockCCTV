# Flock CCTV

Flock CCTV (Flock for short) is a small Discord bot that counts the messages of
an admin-managed list of people and measures their observed time in configured
voice channels. It stores statistics in SQLite and is designed to run as a
systemd service on the Raspberry Pi. It requires Python 3.11 or newer; the
pinned dependencies were checked on Debian 13, aarch64, with Python 3.13.
This is release 1.0.1. It replaces the single-person Leland Tracker; existing
Leland Tracker data moves over once with the import in
[Migrating from Leland Tracker](#migrating-from-leland-tracker).

The product scope and measurement definitions are in [DESIGN.md](DESIGN.md).
The implementation interfaces are in [IMPLEMENTATION.md](IMPLEMENTATION.md).
To propose a change, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Who is tracked

Nobody is tracked by default. The owner and tracker admins choose who is
tracked in Discord with `/flock track add user:@member` and stop with
`/flock track remove user_id:ID`; `/flock track list` shows everyone. Counting
starts at the moment someone is added and stops the moment they are removed.
Removing someone keeps their recorded history, and adding them again resumes
into it. The time in between is shown as missing coverage, never as quiet time.
Bots cannot be tracked.

Tell people before you track them. The bot records message counts and voice
connection time for each tracked person, and any server member can see those
reports. A person can ask an admin to stop tracking them or to erase their data
with `/flock delete-data user:@member` (or `user_id:` after they leave).

## Configure Discord

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On its **Bot** page, generate a token and put it in the private environment
   file as `DISCORD_TOKEN`.
3. Under **Installation**, enable **Guild Install** and choose the `bot` and
   `applications.commands` scopes. Use the install link to add the bot to the
   configured server.
4. Give it View Channel in the channels to collect from. Embed Links is needed
   in a configured report channel. For the optional Leland-only features (see
   [Leland-only legacy features](#leland-only-legacy-features)), add Add
   Reactions wherever reaction mode should work, and Send Messages wherever the
   bot may reply to a direct mention or repost Evil Leland text. Reposting inside
   threads additionally needs Send Messages in Threads for those threads. The
   bot does not need Administrator.

These portal steps follow [Discord's setup guide](https://docs.discord.com/developers/quick-start/getting-started).
The bot uses the `GUILDS`, `GUILD_MESSAGES`, `GUILD_VOICE_STATES`, and privileged
`GUILD_PRESENCES` Gateway intents. Enable **Presence Intent** on the app's **Bot**
page in the Developer Portal. With `LELAND_USER_ID` set it also requests the
privileged `MESSAGE_CONTENT` intent, so enable **Message Content Intent** too in
that case; it reads message text only to create Evil Leland reposts. Without
`LELAND_USER_ID` the bot never requests message text. The fixed mention reply
uses mention metadata. The bot does not need server-members access. Message
bodies are not saved to SQLite or logs.

Create a private `.env` file once from `.env.example` and set `DISCORD_TOKEN`,
`GUILD_ID`, and `OWNER_USER_ID`. Set `ADMIN_USER_IDS` to a comma-separated list
of other trusted Discord user IDs, or leave it blank. Only the owner and
effective extra admins can pause, resume, delete data, manage the tracked list,
or toggle Leland's modes. Discord server permissions do not grant tracker
control. Admins may themselves be tracked.
The configured owner can grant a server member access immediately with
`/flock admin add user:@member`, revoke any extra admin with
`/flock admin remove user_id:ID`, and see the owner and current admins, by
display name, username, and ID, with `/flock admin list`. These replies are
private. Grants and revocations are stored in SQLite and survive restarts. A
revocation takes precedence over `ADMIN_USER_IDS`; the configured owner cannot
be revoked. Data deletion retains admin settings and the tracked list, while
restoring an older database backup restores the admin settings and tracked list
captured in that backup. Keep `OWNER_USER_ID` in the private environment file as
the recovery owner.

`LELAND_USER_ID` is optional. Set it to Leland's Discord user ID to keep the
features that only ever applied to him; leave it blank otherwise. Leland cannot
be the owner or an admin, and he must also be added with `/flock track add` for
his messages to count. A leftover `TARGET_USER_ID` from Leland Tracker is
ignored.

The example collects in all text and voice channels the bot
can see. Set `TEXT_CHANNEL_IDS` and `VOICE_CHANNEL_IDS` to comma-separated
channel IDs to narrow collection. Stats and records omit per-channel totals and
names; `/flock where` follows the visibility rules described below.
`/flock company` charts observed time a tracked person shared with human
companions in those voice channels. Each minute is divided evenly among everyone
else present, tracked or not; time alone is a separate slice. A person's company
is recorded only while they are tracked, so earlier companion time cannot be
reconstructed.

Commands work anywhere in the configured server when `OUTPUT_CHANNEL_ID` is
blank. Set `PUBLIC_REPORT_CHANNEL_IDS` to comma-separated channel IDs to make
`/flock stats`, `records`, `where`, `company`, `leaderboard`, `trends`, `roast`,
`top`, and `help` public only when called in those
channels; other channels get private replies. Set it to `*` for public reports
in every channel. The legacy `OUTPUT_CHANNEL_ID` setting restricts all commands to one
channel and makes general reports public there. `/flock online`, `/flock about`,
`/flock version`, `/flock track` and `/flock admin` commands, and the admin
controls are always private.
The report timezone defaults to `America/Costa_Rica`.

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
directory. Stop the foreground process with Ctrl-C. Once it is running, add
people with `/flock track add`.

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
the service account. Install the recovery helper outside the code tree and the
service unit:

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 /opt/flock-cctv/deploy/update.py /usr/local/libexec/flock-cctv-update.py
sudo install -o root -g root -m 0644 /opt/flock-cctv/deploy/flock-cctv.service /etc/systemd/system/flock-cctv.service
sudo systemctl daemon-reload
```

Replacing a running Leland Tracker? Stop here and do
[Migrating from Leland Tracker](#migrating-from-leland-tracker) before the first
start: the import refuses to write into a database that already exists. For a
fresh install, start the service:

```sh
sudo systemctl enable --now flock-cctv.service
sudo systemctl status flock-cctv.service
```

The first startup registers guild-scoped slash commands. Check the service
logs, then run `/flock about` in the configured server and add the first people
with `/flock track add`:

```sh
sudo journalctl -u flock-cctv.service -n 100 --no-pager
sudo journalctl -u flock-cctv.service -f
```

The bot makes an outbound Discord Gateway connection. It does not require a
public web server, inbound port, or port forwarding.

## Leland-only legacy features

When `LELAND_USER_ID` is set, these features that came from Leland Tracker work
for that one user and for nobody else. Without it they are off, the two toggle
commands reply that Leland mode isn't configured, and `/flock help` and
`/flock about` leave them out.

- Evil mode (`/flock evil-mode`) reposts upside-down versions of his new text
  messages.
- Reaction mode (`/flock reaction-mode`) occasionally reacts to his newly counted
  ordinary messages.
- The bot mirrors his current server avatar with inverted colours. It checks when
  it connects and then every hour. It also refreshes the avatar if the bot
  profile was changed manually; animated source avatars use their first frame.
  This task runs independently of collection pause and data deletion. A small
  marker file beside the database prevents repeating an unchanged profile edit.
- A direct mention of the bot gets the fixed reply described under
  [Commands](#commands).

Both modes act only on messages that are counted, so Leland must be tracked, and
they stop while collection is paused. Deleting everyone's data, or deleting
Leland's data, turns both modes off.

## Migrating from Leland Tracker

This is a one-time move on the Pi from the old single-person bot to Flock. The
import copies the old database into a new Flock database; the old database file
is never modified, so the old install stays available as a rollback until you
delete it. The paths and unit names below are the Leland Tracker defaults
(`leland-tracker`, `/var/lib/leland-tracker`, `/etc/leland-tracker.env`); adjust
them if your install differs. Only a schema version 9 Leland Tracker database
can be imported. If you reuse the old Discord application and token, the old
`/leland` commands disappear from the server the first time Flock starts.

1. Install Flock as described in [Install the systemd service](#install-the-systemd-service)
   up to and including `daemon-reload`, but do not start it. Fill in
   `/etc/flock-cctv.env`: copy `DISCORD_TOKEN`, `GUILD_ID`, `OWNER_USER_ID`,
   `ADMIN_USER_IDS`, the channel settings, and `TIMEZONE` from the old file, and
   set `LELAND_USER_ID` to the old `TARGET_USER_ID`. `TIMEZONE` and `GUILD_ID`
   must match the old database. Environment admins live in the file, not the
   database, so copy `ADMIN_USER_IDS` too. Do not add `TARGET_USER_ID`.

2. Stop the old bot and everything that could restart it or run it again. Two
   bots must not share one token, and the old updater must not redeploy the old
   code:

   ```sh
   sudo systemctl disable --now leland-tracker-update.timer leland-tracker-update.path
   sudo systemctl stop leland-tracker-update.service
   sudo systemctl disable --now leland-tracker.service leland-tracker-llm.service
   systemctl list-units 'leland-tracker*'
   ```

3. Snapshot the old database with SQLite's backup API as the old service
   account, copy it into Flock's state directory, and check it. The snapshot is
   a second copy of Leland's data, so delete both copies when the migration is
   done. Keep it outside `BACKUP_DIR`, which `/flock delete-data` manages.

   ```sh
   sudo -u leland-tracker python3 - <<'PY'
   import sqlite3

   source = sqlite3.connect("/var/lib/leland-tracker/tracker.sqlite3")
   destination = sqlite3.connect("/var/lib/leland-tracker/leland-snapshot.sqlite3")
   source.backup(destination)
   destination.close()
   source.close()
   PY
   sudo install -o flock-cctv -g flock-cctv -m 0640 /var/lib/leland-tracker/leland-snapshot.sqlite3 /var/lib/flock-cctv/leland-snapshot.sqlite3
   sudo rm /var/lib/leland-tracker/leland-snapshot.sqlite3
   sudo sqlite3 -readonly /var/lib/flock-cctv/leland-snapshot.sqlite3 'PRAGMA integrity_check; PRAGMA user_version;'
   ```

   The first line printed must be `ok` and the second `9`.

4. Run the import as the service account with a dry run first. It validates the
   snapshot and prints row counts without writing anything:

   ```sh
   sudo -u flock-cctv sh -c 'set -a; . /etc/flock-cctv.env; set +a; exec /opt/flock-cctv/.venv/bin/python -m flock_cctv.legacy_import --source /var/lib/flock-cctv/leland-snapshot.sqlite3 --dry-run'
   ```

   If the counts look right, run the same command without `--dry-run`. The tool
   takes `--source` (required), `--database`, `--backups`, `--guild-id`,
   `--leland-user-id`, `--timezone`, and `--dry-run`. Apart from `--source`, each
   option falls back to the `DATABASE_PATH`, `BACKUP_DIR`, `GUILD_ID`,
   `LELAND_USER_ID`, and `TIMEZONE` environment variables, which is what the
   command above uses. To pass them explicitly (the IDs here are made up):

   ```sh
   sudo -u flock-cctv /opt/flock-cctv/.venv/bin/python -m flock_cctv.legacy_import \
     --source /var/lib/flock-cctv/leland-snapshot.sqlite3 \
     --database /var/lib/flock-cctv/tracker.sqlite3 --backups /var/lib/flock-cctv/backups \
     --guild-id 111111111111111111 --leland-user-id 222222222222222222 \
     --timezone America/Costa_Rica
   ```

   It exits 0 and prints only counts: never IDs or message data. It exits
   non-zero with a message, and removes a database it had just created, when the
   source is not schema version 9, when its guild, tracked user, or timezone
   differ from the arguments, when the destination already holds a Flock
   database (it never merges), or when a copy or row-count check fails. A failed
   import can be rerun after fixing the cause.

5. Start Flock and check it:

   ```sh
   sudo systemctl enable --now flock-cctv.service
   sudo journalctl -u flock-cctv.service -n 100 --no-pager
   ```

   In Discord, `/flock about` should show one tracked person, and
   `/flock stats user:@Leland period:all` should show his old totals. The first
   `/flock` sync replaces the server's command list, so `/leland` is gone and
   `/flock` takes its place.

6. Set up automatic updates for the new repository with the steps in
   [Automatically update from the default branch](#automatically-update-from-the-default-branch).
   Leland Tracker's deploy key cannot be reused: GitHub deploy keys belong to one
   repository, so FlockCCTV needs its own new read-only key.

7. When Flock has run correctly for a while, delete the snapshot
   (`sudo rm /var/lib/flock-cctv/leland-snapshot.sqlite3`) and remove the old
   service files, `/etc/leland-tracker.env`, `/opt/leland-tracker`, the old
   deploy key, the old backups, and `/var/lib/leland-tracker`. Those copies
   hold Leland's data and the old bot token, and Flock's deletion commands do
   not manage them.

What the import copies, all for Leland and keeping record IDs:

- Leland becomes a tracked person, active since the old database's tracking
  start, with an open tracking interval from that moment.
- His whole history: message metadata and daily totals, voice visits and
  segments, voice company (current roster and daily rows, including the whole
  shared time), last voice observation, and records.
- Collector state: coverage intervals and gaps, the pause state and who paused,
  checkpoints, the pruning boundary, and the retention setting.
- Owner-issued admin grants and revocations, and the evil-mode and reaction-mode
  settings, including the reaction countdown.

Anything the old bot had open when it stopped is closed by Flock's normal
startup recovery at its last checkpoint and marked incomplete; the summary
reports how many open voice segments that affects.

## Tests

Run the standard-library test suite with:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## Back up and restore

The bot uses SQLite's backup API for managed daily backups and retains the
seven most recent daily copies. These backups are stored in the configured
`BACKUP_DIR` as `flock-cctv-YYYY-MM-DD.sqlite3`. Make sure the Pi has enough
free space and that its clock stays synchronized. Backups run at startup and
every 24 hours thereafter.

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

The two named copies above remain until overwritten or deleted. `/flock delete-data`
removes them, the daily backups, and temporary backup files along with the
tracked statistics, whether it erases everyone's data or one person's, because
every backup contains that person's data. Keep these exact filenames in
`BACKUP_DIR` so they remain covered by deletion. Deletion also removes abandoned
updater restore snapshots beside the database: `<database-name>.update-restore`
and `.<database-name>.update-restore-*.tmp`. Copies placed elsewhere are
operator-managed and need separate deletion. Restoring a backup also restores
the tracked list and each person's history as they were when it was taken.

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

The repository (`git@github.com:jose9706/FlockCCTV.git`) is private, so the
updater reads it with a dedicated read-only deploy key. Create the key on the Pi,
pin GitHub's SSH host keys, then add the public key to the repository
(**Settings → Deploy keys**, read-only, or with `gh repo deploy-key add`). GitHub
deploy keys belong to a single repository, so a key created for another
repository, including Leland Tracker's, cannot be reused here:

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

The path unit lets `/flock update` start a check without waiting for the
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

Rollback writes a unique temporary snapshot through its newly created file
descriptor before replacing the database. A failed copy keeps the live
database and its SQLite sidecars intact. Abandoned restore snapshots are
covered by data deletion as described above.

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
statistics or credentials. `/flock about` shows it, and the bot sends the
configured owner one DM when a run of failures starts (not on every failed
poll). If the owner's DMs are closed, only `/flock about` and the journal
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

The hold survives restarts and `/flock about` shows it. Resume normal
updates with the same command using `--release` instead of `--revision ...`.
If the older commit cannot open a newer database schema, its startup check fails
and the updater rolls back to the current code.

### Checking what is running

The release number lives in `src/flock_cctv/__init__.py` (`__version__`,
semantic versioning) and is bumped by hand in the pull request that changes
behavior. The updater also stamps the deployed commit into the installed package,
so the running version is `release (short commit)`, for example `1.0.1 (1a2b3c4)`:

- `/flock version` replies privately with it; `/flock about` also shows it on its second line.
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
replaced at the next update and `/flock delete-data` removes it. Temporary
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
  Python dependencies, a `LELAND_USER_ID` that is also the owner or listed in
  `ADMIN_USER_IDS`, or state-directory permissions.
- **Slash commands are missing:** check the startup log and configured guild ID;
  confirm the app was installed with `bot` and `applications.commands` scopes.
  Presence and Message Content intents must be enabled in the portal.
- **Someone's counts or voice time are not recorded:** run `/flock track list`.
  Only people on the tracked list are counted, and only from the moment they
  were added. Bots cannot be tracked. A report about someone who was never added
  says they aren't tracked.
- **Counts or voice time stop changing for everyone:** check `/flock about`,
  confirm collection is not paused, and check the channel allowlists and View
  Channel access. Voice time is observed connection time, and the server AFK
  channel is excluded.
- **Database identity or timezone error:** use the original `GUILD_ID` and
  `TIMEZONE` for that database. Changing timezone requires a fresh database; it
  does not regroup existing statistics.
- **Database instance lock:** run only one bot process against a database. Stop
  a duplicate foreground process or service; do not delete the `.lock` file to
  bypass the lock.
- **The import was refused:** read the message it printed. The usual causes are
  a source that is not a schema version 9 Leland Tracker database, a
  `--guild-id`, `--leland-user-id`, or `--timezone` that differs from the old
  database, or a destination that already holds a Flock database. The import
  never merges into an existing database.
- **`/flock evil-mode` says Leland mode isn't configured:** set `LELAND_USER_ID`
  in `/etc/flock-cctv.env` and restart the service.
- **Evil Leland does not repost:** confirm `LELAND_USER_ID` is set, Leland is
  tracked, the mode is on, collection is unpaused, the message is in a collected
  text channel, and the bot can send messages there. It reposts text only; text
  is still reposted when the source message also has attachments. Mentions and
  embeds are suppressed.
- **The bot does not answer a direct mention:** the reply exists only when
  `LELAND_USER_ID` is set. It works independently of collection pause and
  channel allowlists, but the bot must receive the server message and have
  permission to send in that channel.
- **Avatar does not refresh:** avatar mirroring needs `LELAND_USER_ID`. Check
  that he is still in the configured server and inspect the service journal for
  avatar update errors.

## Commands

All commands live under `/flock`. These reports take an optional `user` option
("Whose activity to show"); omit it to see your own: `stats`, `records`,
`where`, `company`, `leaderboard`, `trends`, `online`, and `roast`. A bot gets a
private "Bots aren't tracked." reply, and someone who was never tracked gets a
private reply saying so and pointing to `/flock track add`. For someone who was
tracked and is no longer, the report works from their kept history and adds a
"no longer tracked" line (`online` needs a currently tracked person). Titles and
text use the person's display name, never a mention. Every period choice is
today, this week, this month, or all time unless stated otherwise.

- `/flock stats period: user:` reports message totals, observed voice time,
  active days, voice visits, and missing coverage for the period. The default
  period is this week. Each person has their own tracking start and gaps:
  missing coverage includes the time they were not on the tracked list.
- `/flock records user:` shows the busiest message day, the longest fully observed
  voice visit, how long a visit in progress has been observed so far, and the
  top voice companion since companion tracking began (same split-time measure
  and channel visibility rules as `/flock company`).
- `/flock where user:` shows the last observed voice channel and time, or that the
  person is currently in voice. It is public in configured report channels and
  private elsewhere. A public reply names the voice channel only if the
  `@everyone` role can view it;
  a private reply names it only if the requester can view it.
- `/flock company period: count: user:` attaches a pie chart of the person's observed
  voice time with each companion or alone. The default is this week. Companions
  are all humans in the same voice channel, tracked or not. Its image legend and
  text list show names, percentages, and durations. `count` sets how time shared
  with several people is counted: `split` (default) splits each shared minute
  evenly so the slices add up to the observed time; `full` credits each person
  with the whole minute, as `/flock leaderboard` does, so slices overlap,
  percentages are of observed time, and the chart shows relative shares. With
  `full`, company time recorded before whole shared time was tracked counts as
  its split share.
  Names are looked up from Discord when absent from the bot's cache; if Discord
  cannot provide a name, the report shows the user ID. Names are not stored.
  The chart includes only source voice channels visible to the requester
  for private replies or to `@everyone` for public replies. Time lost during
  outages is never estimated. Companion member IDs and daily attributed totals
  are stored in SQLite and managed backups; deletion erases the subject's rows.
- `/flock leaderboard period: user:` ranks the top 10 people by the whole observed
  voice time they spent in a tracked channel with the person. Unlike
  `/flock company`, time is not split: an hour in a call with three people counts
  as an hour for each of them. It uses the same channel visibility rules and name
  lookup as `/flock company`. Time alone is shown but not ranked, and anyone past
  the top 10 is counted on one line. The default period is all time. Company
  time recorded before whole shared time was tracked counts as its split share,
  since the group size at the time was not stored.
- `/flock trends period: kind: count: user:` attaches a chart of how activity changes. The
  default period is the last 7 days (today and the six days before it); pick
  `This week` to start on Monday instead. Kinds:
  - `daily` (default): messages and observed voice time per day (grouped by
    week or month for long periods), busiest day, active-day streaks, and ghost
    days. A ghost day is a finished day the tracker watched in full, while the
    person was tracked, with no messages or voice; days it was disconnected or
    paused, or the person was not tracked, are never ghost days, even when the
    interruption lasts less than a second.
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
    and name lookup as `/flock company`. `count` works as in `/flock company`;
    with `full`, stacked bars can add up to more than the observed time. Other
    kinds ignore `count`.
  - `bursts`: runs of messages sent within two minutes of each other, with the
    biggest burst, the average size, and the share in bursts of five or more.
  `hours`, `bursts`, and `compare` need message send times or voice sessions,
  which exist only within `RETENTION_DAYS`; replies say when a period reaches
  past that. Other kinds use daily totals, which match `/flock stats` and do
  not filter by channel. Unobserved time is never filled in.
- `/flock online user:` checks a currently tracked person's Discord status.
  Online, Away, and Do Not Disturb count as online. Offline may also mean
  Invisible. The reply is private and no presence history is stored.
- `/flock roast period: user:` produces a short joke from a real statistic about
  the person (this week by default) and uses a shared cooldown.
- `/flock top period: metric:` ranks the tracked people, top 10, by `Messages`
  (the default), `Voice time`, or `Active days` for the period (this week by
  default). People with nothing for that metric are left out, a former person
  appears only if they have activity in the period and is marked as no longer
  tracked, and the rest are counted on one line. It follows the general report
  visibility rules and names no channels.
- `/flock help` explains the measurements and available commands.
- `/flock about` privately shows the bot version, connection health, collector
  state, number of tracked people, last checkpoint, recorded coverage gaps, and
  the last automatic update result. With `LELAND_USER_ID` set it also shows the
  evil and reaction mode state. Coverage gaps here are the collector's global
  outages; admins can see each one with `/flock debug uptime`.
- `/flock version` privately shows the release number and deployed commit.
- `/flock update` lets the configured owner and extra admins make the Pi check
  GitHub for a new commit now instead of waiting for the 15-minute poll. It
  needs the update path unit from "Install the timer" above; without it the
  request is picked up at the next scheduled poll.
- `/flock debug` commands help the configured owner and extra admins check on
  the bot. Every reply is private.
  - `/flock debug health` shows how long the bot process and collection have
    been running, how old the last checkpoint is, the current error, how many
    problems were logged in the last day and week, database size and free disk
    space, the newest daily backup, retention, the outage alert setting, and the
    last automatic update result. It flags a stale checkpoint, under 10% free
    disk, or no backup in two days.
  - `/flock debug uptime period:` (last 7 days by default) shows the share of
    time the bot was watching, every outage with its start, length and cause
    ("Discord connection lost" or "bot stopped or restarted"), time paused, and
    a stacked chart of watching, outage, and paused time per day.
  - `/flock debug errors` lists the newest warnings and errors the bot logged.
    Only the bot's own log line and the error type are kept (never message text
    or error details), for the retention period and at most 500 entries; the
    journal still has full tracebacks.
  - `/flock debug person user:` shows one person's tracking history: when they
    were tracked and by whom, how much of that time the bot was watching,
    missing coverage split into outages, pauses, and time off the tracked list,
    the outages that hit them, counts of stored records, what retention pruned,
    and whether they are in voice now. It never names voice channels.
  - `/flock debug alerts minutes:` shows or sets how long an outage must last
    before the owner gets a DM about it once the bot is back. The default is 15
    minutes and 0 turns alerts off. Outages that ended before a change are not
    announced.
- `/flock pause`, `/flock resume`, and `/flock delete-data` control
  collection. Only the configured owner and extra admins can use these controls.
  Pausing and resuming apply to everyone.
- `/flock delete-data user:` (or `user_id:` with an ID or mention, which also
  works after someone leaves the server) requires an ephemeral confirmation and
  checks access again. Cancel works until deletion starts; once it is processing,
  cancellation reports that it cannot stop the operation. Without either option
  it erases everyone's statistics and all managed local backups, then pauses
  collection; the tracked list is kept, so everyone who was tracked starts a
  fresh history when
  collection resumes. With one it erases only that person's statistics and
  managed backups and removes them from the tracked list, while collection for
  everyone else continues. Time the erased person shared with others stays in
  those people's company reports, so their totals still add up, but it is
  credited to "Deleted person" instead of their ID and is never ranked on a
  leaderboard or named as a top companion. If the person is still in a call
  with someone tracked, that call keeps counting them as company, as it does
  for any human present. The confirmation says which of the two it will do.
- `/flock track add user:@member` starts tracking a server member, and
  `/flock track remove user_id:ID` stops tracking someone by ID or mention,
  including after they leave the server; their history is kept. Only the
  configured owner and extra admins can use them, and the replies are private.
  `/flock track list` privately shows who is tracked (with the date they were
  added) and, separately, former people whose history is still kept (with the date
  they stopped). Anyone in the server can use `list`.
- `/flock admin add user:@member`, `/flock admin remove user_id:ID`, and
  `/flock admin list` let only the configured owner manage tracker admins.
  Removal also accepts a user mention and works after someone leaves the server.
  Adding rejects bots, the owner, and the configured Leland user. The list and
  the add/remove confirmations show each person's display name and
  username next to their ID, or "Unknown user" when Discord cannot resolve the
  account.
- `/flock evil-mode mode:on/off` lets admins enable or disable Evil Leland mode.
  When on, the bot posts upside-down Unicode versions of Leland's new text
  messages in the same channel. It does not repost attachment files, though text
  accompanying an attachment is still reposted. Empty text is skipped. Mentions
  and embeds are suppressed, and message bodies are not stored. Pausing collection
  stops reposts; deleting data turns this mode off. The toggle persists across
  restarts. Nobody else's messages are reposted.
- `/flock reaction-mode mode:on/off` lets admins enable or disable occasional
  reactions to Leland's newly counted ordinary messages. When on,
  the bot picks a new interval of 15–25 messages, then reacts with either 😂 or
  👸 and picks another interval. The setting and current interval survive a
  restart. Pausing collection stops the counter; deleting data turns it off.
  The bot needs Add Reactions permission in tracked text channels. Reactions
  may be missed if Discord rejects one or the bot stops between counting and
  reacting.

Both mode commands reply privately that Leland mode isn't configured when
`LELAND_USER_ID` is unset.

When `LELAND_USER_ID` is set and someone directly mentions the bot in the
configured server, it responds with “I am evil Leland, more gay than the
original”. This reply works whether Evil Leland mode is on or off, even while
collection is paused or the channel is outside the text allowlist, and never
mentions anyone.

Voice time measures observed connection time, not speaking. Visits already in
progress when observation begins, including when someone is added to the tracked
list while in voice, are marked incomplete and cannot set the longest-visit
record. A brief tracker reconnect or restart (up to two
minutes) while a person stays in the same channel does not split their visit; the
unobserved interval is excluded from its length. See [DESIGN.md](DESIGN.md) for coverage and recovery rules.
