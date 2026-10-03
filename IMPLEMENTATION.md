# Implementation contracts

This document describes the current module interfaces. Python 3.11+;
discord.py; standard-library SQLite through serialized worker-thread operations.
All timestamps in the storage/service API are UTC Unix seconds (`float`).
Discord IDs are `int` in Python and TEXT in SQLite. Commands operate only in the
configured guild. With an output channel configured, enforce that channel; without
one, accept commands anywhere in the guild. `OUTPUT_CHANNEL_ID` restricts all
commands and makes general reports public in that channel, even if
`PUBLIC_REPORT_CHANNEL_IDS` is blank. Otherwise, that setting selects public
general-report channels; other channels receive ephemeral replies.
Tests use fake events and a temporary database.

## Configuration

Frozen `Config` dataclass in `config.py`:
`token: str` (excluded from repr), `guild_id: int`, `owner_user_id: int`,
`output_channel_id: int | None`, `text_channel_ids: frozenset[int] | None`,
`voice_channel_ids: frozenset[int] | None`, `timezone: str`, `database_path: Path`,
`backup_dir: Path`, `admin_user_ids: frozenset[int] = frozenset()`,
`public_report_channel_ids: frozenset[int] | None = frozenset()`,
`checkpoint_seconds: int = 60`, `retention_days: int = 90`,
`leland_user_id: int | None = None`. Construct it with keywords.
`Config.from_env()` validates required values, explicit nonempty channel allowlists,
IDs, timezone, distinct safe backup/database locations, and positive numeric settings.
There is no configured target: the tracked people live in SQLite. The database is
tied to its original guild and timezone. Use the same ID and timezone when
starting an existing database.
Environment: DISCORD_TOKEN, GUILD_ID, OWNER_USER_ID,
ADMIN_USER_IDS (optional comma-separated extra admin IDs),
LELAND_USER_ID (optional; enables the Leland-only legacy features),
OUTPUT_CHANNEL_ID,
PUBLIC_REPORT_CHANNEL_IDS (blank private, `*` public everywhere, or CSV channel IDs),
TEXT_CHANNEL_IDS and VOICE_CHANNEL_IDS (default `*`, represented as None, for all
bot-visible guild channels), OUTPUT_CHANNEL_ID is optional,
TIMEZONE (America/Costa_Rica),
DATABASE_PATH (data/tracker.sqlite3), BACKUP_DIR (data/backups),
CHECKPOINT_SECONDS (60), RETENTION_DAYS (90).
`OWNER_USER_ID` is the immutable recovery owner. SQLite admin decisions override
the optional environment admin list without changing the frozen config.
`LELAND_USER_ID`, when set, must be a positive ID that is neither
`OWNER_USER_ID` nor in `ADMIN_USER_IDS` (`ValueError("LELAND_USER_ID cannot be a
tracker admin")`). A leftover `TARGET_USER_ID` is ignored.

## Storage (storage.py and stats.py)

`Store(path: Path, backup_dir: Path, timezone: str)` exposes async methods and
`SCHEMA_VERSION = 1`; `StoreError` is its error type. All per-person methods take
`user_id: int` first. State is either global (collector health and settings) or
per person (everything else); see the data model in [DESIGN.md](DESIGN.md).
A new database is created at version 1 directly, and `initialize` refuses a
database with a newer schema.

Lifecycle and global state:

- `initialize(now: float, guild_id: int)` creates the schema, validates database
  identity (`StoreError("database belongs to a different guild")`,
  `StoreError("database timezone differs from configured timezone")`), closes
  **every** stale open voice segment at its own checkpoint as incomplete, clears
  all current company rosters, marks coverage lost since the earliest recovery
  point as a `process_restart` gap, and reapplies the visit bridge rule per person.
- `close()` releases resources.
- `state() -> dict`: `paused: bool`, `paused_by: str | None`,
  `tracking_since: float` (the database clock), `last_checkpoint: float | None`,
  `evil_mode: bool`, `reaction_mode: bool`.
- `set_paused(paused: bool, actor_id: int, now: float)` persists state and, when
  pausing, closes every open segment and coverage. Authorization is checked by
  commands.
- `set_evil_mode(enabled: bool)` persists the Leland-only message-repost setting.
- `set_reaction_mode(enabled: bool)` persists the Leland-only reaction setting and
  starts a fresh random 15–25 message interval when enabled.
- `admin_override(user_id: int) -> bool | None` reads a persisted decision or
  falls back to the environment list when absent. `admin_overrides() -> dict[int, bool]`
  supports the private list command. `set_admin_override(user_id: int, enabled: bool)`
  persists an owner-issued grant or revocation.
- `connect(now: float)` opens coverage unless paused; idempotent.
- `disconnect(now: float)` closes every open segment at its own last checkpoint as
  incomplete, closes connected coverage conservatively, and marks unavailable; the
  gap starts at the earliest of the coverage and segment checkpoints.
- `checkpoint(now: float)` advances every open segment and the coverage checkpoint
  to one common time, `max(now, coverage checkpoint, every open segment checkpoint)`.
- `coverage_gap_seconds(now: float) -> float` is the collector's global outage
  total since the database clock began, shown by `/flock about`.
- `maintenance(now: float, retention_days: int)` rolls up/prunes details for all
  people without changing totals, uses SQLite backup API, retains seven managed
  daily backups.

The tracked list:

- `track_user(user_id: int, actor_id: int, now: float) -> bool` returns False when
  the person is already tracked. It creates the row (first tracked time = now) or
  reactivates it (keeping the first tracked time), and opens a tracking interval
  that never overlaps an earlier one. It works while paused.
- `untrack_user(user_id: int, actor_id: int, now: float) -> bool` returns False
  when the person is not tracked. An open segment closes at `now` through the
  normal close path as incomplete, the person's current roster is removed, the
  tracking interval closes, and the row becomes inactive. History is kept.
- `tracked_users() -> list[dict]` returns every row, active or not, ordered by ID:
  `user_id: int`, `active: bool`, `tracking_since: float`, `added_by: int | None`
  (`None` for an import), `updated_at: float`.
- `active_user_ids() -> frozenset[int]`.

Collection. Each method does nothing, or returns False, unless the person is
**active**, and also rejects events before `max(database clock, resume boundary,
start of the person's open tracking interval)`, while paused or disconnected,
and pruned messages:

- `add_message(user_id: int, message_id: int, channel_id: int, created_at: float) -> bool`
  inserts deduplicated metadata and increments the person's aggregates
  transactionally.
- `add_message_with_reaction(user_id, message_id, channel_id, created_at, *, ordinary: bool) -> tuple[bool, bool]`
  also advances the single global saved reaction interval in the same
  transaction, returning whether insertion succeeded and a reaction is due. Only
  the Leland user is routed here.
- `voice_transition(user_id: int, channel_id: int | None, now: float, complete_start: bool = True, companions: set[int] | frozenset[int] = frozenset())`
  opens/closes/splits that person's one active visit; moves between tracked channels
  retain the visit ID; `complete_start=False` means original join unknown.
  `companions` is the set of other present human member IDs. Same channel is a
  no-op. Afterwards every other open segment is credited through the same moment.
- `companion_transition(channel_id: int, member_id: int, joined: bool, now: float)`
  runs in one transaction. It first credits every open segment through `now`
  with the rosters that applied until then, then, for every person other than
  `member_id` whose open segment and current roster are in `channel_id`, adds or
  removes `member_id` in their roster. Rosters that already match are skipped
  and the member's own segment is never touched.
- Whenever one person's event advances the shared coverage checkpoint, every
  other open segment is credited to the same time first (as `checkpoint` does),
  so coverage never runs ahead of an open segment; `untrack_user` does the same
  after closing the person's segment.

Per-person reports (`include_live` semantics unchanged: a person's live time is
their open segment while connected and not paused). A person's start is
`max(database clock, their first tracked time)`; their watched time is the global
coverage intervals intersected with their tracking intervals; their gap seconds in
a window are the global coverage gaps inside their tracking intervals plus every
moment after their start that falls outside all their tracking intervals.

- `stats(user_id: int, period: str, now: float, *, include_live: bool = True) -> dict`:
  `messages: int`, `voice_seconds: float`, `voice_visits: int`, `active_days: int`,
  `tracking_since: float` (the person's start), `gap_seconds: float`,
  `paused: bool`, `tracked: bool` (currently active), `known: bool` (has a row).
  Periods: today/week/month/all. Clip live duration to now only while connected;
  split at local day boundaries. Reports pass include_live=False while runtime
  reconciliation is incomplete.
- `ranking(period: str, now: float, *, include_live: bool = True) -> list[dict]`
  returns one row per person who is active or has any activity in the period:
  `user_id`, `messages`, `voice_seconds`, `voice_visits`, `active_days`, `tracked`.
  It uses `stats` aggregation (daily totals plus each live segment, live days
  counted as active) with period bounds from the database clock, ordered by user
  ID; the command sorts.
- `company_daily(user_id: int, period: str, now: float, *, include_live: bool = True) -> list[dict]`
  returns the person's company rows by `day`, `channel_id`, and `member_id`, with
  live time split by local day; `company_totals(user_id, period, now, *, include_live=True)`
  sums them into `seconds` (split evenly among peers present) and `full_seconds`
  (the whole shared time per peer) by channel and member ID (`0` means alone).
  Daily totals survive detail retention; deletion removes the subject's rows.
- `daily_trend(user_id: int, period: str, now: float, *, include_live: bool = True) -> list[dict]`
  returns one row per local day in the period from the person's start (`day`,
  `messages`, `voice_seconds`, `voice_visits`, `watched`), zero-filled, with live
  voice split by day under the same rule as `stats`. `watched` is true only for a
  finished day fully inside coverage and the person's tracking intervals.
- `period_comparison(user_id: int, period: str, now: float, *, include_live: bool = True) -> dict`
  returns `current` and `previous` window totals (`start`, `end`, `messages`,
  `voice_seconds`, `voice_visits` with a complete start, `watched_seconds`) from
  retained detail, using `stats.previous_period_bounds` (same local day offset
  and clock time). `previous` is `None` with `reason` `all`, `untracked` (the
  window starts before the person's start), or `pruned` when no fair comparison
  exists.
- `message_times(user_id: int, period: str, now: float) -> dict` returns retained
  message send times (`times`, oldest first), `since` (where retained detail starts
  within the period), and `period_start`.
- `voice_hours(user_id: int, period: str, now: float, *, include_live: bool = True) -> dict`
  returns observed voice seconds by local hour (`hours`) from retained segments,
  clipped to the same `since`, and `period_start`.
- `records(user_id: int, now: float, *, include_live: bool = True) -> dict`:
  `busiest_day: str | None`, `busiest_day_messages: int`,
  `longest_visit_seconds: float`, `longest_visit_at: float | None`,
  `current_visit_seconds: float | None` (observed length so far of a live visit),
  and `current_visit_complete_start: bool`. Only fully observed closed visits win
  records. When reconciliation finds the person in the same channel within
  `VISIT_BRIDGE_SECONDS` (120) of a visit cut short by a disconnect or restart
  gap, the visit continues; the gap stays uncounted. The previous visit must be
  that person's, so an untrack and re-track never bridges. Startup reapplies this
  rule to retained visits and only raises the record.
- `last_voice(user_id: int, now: float, *, include_live: bool = True) -> dict | None`
  returns the latest channel/time and whether the person is currently observed
  there. The latest observation survives detail retention and is cleared by
  deletion.

Deletion:

- `delete_data(actor_id: int, now: float)` is global. It removes managed backups
  first, then everyone's statistics, coverage, and tracking intervals, resets the
  database clock, turns evil and reaction modes off, leaves collection paused by
  the invoking admin, and clears open state; compaction is best effort. The
  tracked list survives as an operational setting: active people restart with a
  first tracked time of `now` and a fresh open interval, inactive people's rows
  are dropped. Admin decisions remain in SQLite.
- `delete_user_data(user_id: int, actor_id: int, now: float, *, reset_legacy_modes: bool = False) -> bool`
  removes managed backups first (they contain this person's data), then in one
  transaction every per-person row for `user_id`: messages, daily totals, visits
  and segments, current and daily company where `user_id` is the subject, last
  voice, records, tracking intervals, and the tracked row. Daily company rows in
  which the person is someone else's companion are re-keyed to
  `DELETED_COMPANION_ID` (`"-2"`), summing on conflict, so other people keep
  their shared time without the erased ID; live rosters are left alone. A person
  who appears only as a companion still counts as existing. It does not pause
  collection. `reset_legacy_modes=True` also turns evil and reaction modes off and
  clears the countdown. It returns False when nothing existed for the person.

Storage owns daily aggregates/retention; stats.py may contain pure date helpers.
Keep deletion/backup operations serialized. Never prune before preserving records
and totals. Replay deduplication applies within detailed retention; reject messages
older than the retention cutoff once their IDs have been pruned.

## Service (collectors.py)

`Tracker(config: Config, store: Store)` exposes `connected: bool`, `guild_is_available: bool`,
`collection_ready: bool` (false until storage reconciliation succeeds),
`last_error: str | None`, `tracked_ids: frozenset[int]` (the cached active tracked
people, refreshed from `Store.active_user_ids()` whenever guild collection starts
and after every track, untrack, or deletion), and async methods:
`gateway_ready()`, `ready(snapshot: VoiceSnapshot, now: float | None = None)`,
`guild_available(snapshot, now: float | None = None)`, `guild_unavailable(now: float | None = None)`,
`disconnected(now: float | None = None)`, `message(message) -> bool` (Discord-like object),
`message_with_reaction(message, *, ordinary: bool) -> tuple[bool, bool]`,
`voice(member, before, after)`, `checkpoint()`,
`pause(actor_id: int)`, `resume(actor_id: int, snapshot: VoiceSnapshot)`,
`delete_data(actor_id: int)`, `track_user(user_id: int, actor_id: int, snapshot) -> bool`,
`untrack_user(user_id: int, actor_id: int) -> bool`,
`delete_user_data(user_id: int, actor_id: int) -> bool`, `shutdown()`.
`report_error(operation, exc)` and `report_recovered(operation)` let the adapter
publish and clear its own failures.

`VoiceSnapshot` is either a mapping or a zero-argument function returning one; a
function is called only once the tracker holds its lock. A *voice snapshot*
is `Mapping[int, int]`: member ID to the eligible channel ID of
every non-bot member currently in an eligible voice channel (allowlist applied,
AFK excluded). The adapter builds it; the tracker derives each tracked person's
companions as the other members the snapshot places in the same channel. Starting
guild collection refreshes `tracked_ids`, retries `Store.disconnect`, calls
`Store.connect`, then starts an incomplete-start visit for each tracked person in
the snapshot. `track_user` starts one for a newly added person the same way when
collection is live and not paused.

`message` and `message_with_reaction` accept a message only when its author is in
`tracked_ids`, in addition to the guild, channel allowlist, bot, pause, and
observation-boundary checks, and forward the author ID as `user_id`. `message_with_reaction`
is used only for `Config.leland_user_id`; the reaction countdown is global.
`voice` ignores bots entirely and returns when the eligible previous and current
channels are equal. If the member is tracked it calls `Store.voice_transition`
with the humans in the new channel as companions. Then, for everyone else, it
calls `Store.companion_transition(previous, member_id, False, now)` and
`Store.companion_transition(current, member_id, True, now)`, using one `now` for
the whole event. `delete_user_data` passes `reset_legacy_modes` when the user is
`config.leland_user_id`.

Serialize state transitions with an asyncio lock. Ignore other guilds, untracked
authors, channels outside an explicit allowlist, AFK channel, messages predating
observation start or pause/resume boundary. While disconnected ignore voice events
(including uncertain replay). On resumed, reconcile the cached snapshot and
conservatively start incomplete segments. Track pause in storage; configured admins
can resume any prior pause.

## Discord adapter (bot.py)

`create_bot(config: Config) -> TrackerClient` initializes storage in `setup_hook`
(`Store.initialize(time.time(), config.guild_id)`), registers guild command groups,
synchronizes only the configured guild, and wires
ready/resumed/disconnect/message/voice events to `Tracker`. Checkpoint, daily
maintenance, avatar-refresh, and update-watch tasks are cancelled/awaited on close.
On every Gateway ready it writes `bot-ready.json` (`ready_at`, `pid`) beside the
database for the updater's health check. Every five minutes it reads the
updater's `update-status.json` there (`update_status.py`) and DMs the owner once
per failure streak, recording the streak start in `update-notified.json`.
`/flock about` adds `update_status.status_line`. `/flock update` (admins only,
private reply) calls `update_status.request_update`, which writes
`update-requested.json` (`requested_at`) there unless the status shows a hold;
`deploy/flock-cctv-update.path` starts `flock-cctv-update.service` while
that file exists, and every polling run of `deploy/update.py` removes it before
taking its lock. Collection
and storage failures surface in status and logs; avatar-refresh failures are
logged. Message payloads and the token are not logged. Gateway
intents: guilds, guild_messages, voice_states, presences, plus message_content
only when `leland_user_id` is set. Use
`AllowedMentions.none()` for bot output. Expose `bot.config`, `bot.store`,
`bot.tracker`, `bot.tree`, and `bot.voice_snapshot() -> dict[int, int]`, built
from the cached guild's voice and stage channels (same AFK, allowlist, and guild
checks; bots excluded; empty when the guild is unknown or unavailable). Every
place that needs voice state passes the bound method `bot.voice_snapshot` itself,
not its result, so the tracker reads the cache only while holding its lock:
ready, guild available and join, the periodic recovery retry, `/flock resume`,
and `/flock track add`.

`on_message` routes by author. A message from `config.leland_user_id` goes to
`tracker.message_with_reaction`; anyone else goes to `tracker.message`, with no
reaction and no repost. The Leland-only features are active only when
`leland_user_id` is set: after one of his ordinary messages is newly counted,
reaction mode adds either 😂 or 👸 when due and evil mode reads the text,
transforms it to upside-down Unicode, and posts the result in the same channel,
skipping empty text and not reposting attachment files (text accompanying an
attachment is still reposted). A direct mention of the bot gets a fixed reply
based on mention metadata, even when collection is paused or that channel is
outside the text allowlist, and only when `leland_user_id` is set. The hourly
avatar task, which mirrors his current server avatar with inverted colours using
the first frame for animated images and refreshing if the source changes or the
bot avatar is manually changed, is not started otherwise. Reaction mode advances
its saved interval only for newly counted ordinary messages. Reaction and repost
failures are logged without message bodies; they do not change collected message
totals.

`__main__.py` loads Config and starts the bot with normal logging. It logs the
version at startup and answers `--version` before reading any configuration; the
console script is `flock-cctv`.
`__init__.py` owns `__version__` (semantic release number, `1.0.0`, read by
`pyproject.toml`) and `version_string()`, which appends the short commit from
`_build.py` (generated and git-ignored; written by `deploy/update.py` during
staging) or, in a development checkout, from `git rev-parse`.

## Commands (commands.py)

`register_commands(bot)` installs the single `/flock` group on `bot.tree`
for the configured guild, with `admin` and `track` subgroups. Implement stats,
records, where, company, leaderboard, trends, online, roast, top, help, about,
version, update, pause, resume, delete-data, evil-mode, reaction-mode, `admin`
add/remove/list (owner only), and `track` add/remove/list.
Runtime checks enforce the guild and optional output channel; only the configured
owner and effective extra admins can use controls. `LELAND_USER_ID` is never a
controller, and `admin add` rejects bots, the owner, and that user. Admin decisions
in SQLite override the environment list. Admin management, status, tracked-list
commands, and controls are private. `/flock about` adds the count of tracked
people, and the Leland mode lines only with `leland_user_id`.

Every per-person report takes an optional `user: discord.User | None`,
described “Whose activity to show (defaults to you)”, defaulting to the
requester: `stats`, `records`, `where`, `company`, `leaderboard`, `trends`,
`online`, and `roast`. The command resolves it before deferring. A bot replies
privately “Bots aren't tracked.”; a person without a `tracked_users` row replies
privately that they aren't tracked and an admin can add them with
`/flock track add`; an inactive person with history gets the normal report plus a
“no longer tracked; showing recorded history” line. `online` requires an active
person (an inactive one replies privately that they are no longer tracked).
Titles and text use the person's resolved display name, escaped for Markdown and
mentions, never a mention. Options and defaults:

| Command | Options (default) |
| --- | --- |
| `stats`, `company`, `roast` | `period` (week), `user` |
| `leaderboard` | `period` (all), `user` |
| `trends` | `period` (last7), `kind` (daily), `user` |
| `records`, `where`, `online` | `user` |
| `top` | `period` (week), `metric`: `messages` (default), `voice`, `active_days` |
| `track add` | `user` (a server member) |
| `track remove` | `user_id` (ID or mention, string) |
| `admin add` | `user` (a server member); `admin remove`: `user_id` (string) |
| `evil-mode`, `reaction-mode` | `mode`: `on` or `off` |
| `delete-data` | `user` or `user_id` (ID or mention string; works for departed members). Both optional, not together; everyone's data if both omitted |

Period choices are `today`, `week`, `month`, `all` (plus `last7` for trends).
General reports (`stats`, `records`, `where`, `company`, `leaderboard`, `trends`,
`roast`, `top`, `help`) are public in report channels and private elsewhere;
`online`, `about`, `version`, `update`, `admin`, `track`, and controls are always
private. Deletion confirmation is private, restricted to its requester, rechecks
access when confirmed, expires, and says exactly whether it erases everyone's data
or one named person's. Without `user` it calls `tracker.delete_data` and the tracked
list is kept; with `user` it calls `tracker.delete_user_data` and that person is
untracked while collection for others continues. Roast uses a shared 30-second
cooldown. Defer slow interactions and use followups; errors get a safe response and
logged traceback.

`track add` rejects bots and calls `tracker.track_user(id, actor, bot.voice_snapshot)`.
Per-person reports read the tracked list before acknowledging the interaction,
with a two-second timeout and a private "busy" reply, so a store busy with a
backup cannot exhaust Discord's acknowledgement window. `delete-data user_id:`
names the person from the cache only for the same reason. Company reports show
the deleted-person member as "Deleted person"; leaderboards and the records top
companion rank only real (positive) user IDs.
`track remove` accepts a departed member, calls `tracker.untrack_user`, and
reports when the person was not tracked. `track list` lists active people with the
date they were added, then formerly tracked people whose all-time activity is
nonzero with the date they stopped, named through the admin-label lookup
(display name, username, ID; “Unknown user” fallback), truncated with an “and N
more people” line.

`top` ranks `Store.ranking`, omitting people with zero for the metric, sorted by
value then user ID, top 10 with an “and N more” line, marking former people as no
longer tracked.

The company pie report filters attribution by source voice channel visibility:
requester's View Channel for private replies, `@everyone` View Channel for public
replies. Missing/deleted channels are omitted. It attaches a PNG generated in
memory; no image or message body is stored. Resolve visible companion names from
the member/user cache, then Discord's member/user API if needed. Use the user ID
only when name lookup fails, and do not persist display names.
`/flock leaderboard period:` (default `all`) uses the company report's
visibility filter and name lookup on `full_seconds`, as text only: top 10
people ranked, ties by user ID, the remainder counted, and alone time listed but not ranked.
`/flock admin list` and the add/remove confirmations use the same lookup to
show each user as display name, username, and ID, escaped for Markdown and
mentions, falling back to “Unknown user” with the ID. Names are resolved per request, never stored.
`/flock trends period: kind:` (defaults last7, daily; `last7` starts at local midnight six days ago) follows general-report
visibility and attaches an in-memory PNG: `daily` and `weekdays` from
`Store.daily_trend` (bucketed by week beyond 62 days, month beyond 62 weeks),
`compare` from `Store.period_comparison`, `hours` from `Store.message_times`
and `Store.voice_hours`, `bursts` from `Store.message_times` (two-minute gap),
and `company` from `Store.company_daily` filtered and named like the company
report. All of them take the resolved person's ID. No activity yields a text-only
reply.

`evil-mode` and `reaction-mode` check admin access, then reply privately that Leland
mode isn't configured when `leland_user_id` is unset.

`flock_cctv.legacy_import` is the one-time Leland Tracker import, run as
`python -m flock_cctv.legacy_import --source OLD [--database NEW] [--backups DIR]
[--guild-id G] [--leland-user-id L] [--timezone TZ] [--dry-run]`; every option
except `--source` falls back to `DATABASE_PATH`, `BACKUP_DIR`, `GUILD_ID`,
`LELAND_USER_ID`, and `TIMEZONE`. `import_legacy(source, database, backups, guild_id,
leland_user_id, timezone, *, dry_run=False) -> dict[str, int]` snapshots the source
into memory with the SQLite backup API (read-only, never modifying it), validates
`PRAGMA user_version == 9` and the source's guild, tracked user, and timezone,
refuses a destination that already has a `settings` row, creates the schema through
`Store.initialize(source tracking start, guild_id)`, then copies in one transaction:
the settings fields (including pause, retention, and Leland modes), a `tracked_users`
row and an open tracking interval for Leland from the source tracking start, his
rows in the per-person tables (visit and segment IDs kept), and coverage and admin
overrides verbatim. After commit it reopens through `Store.initialize` so normal
recovery closes anything left open, verifies row counts, and prints counts only.
`main()` returns 0 on success and 1 on a `LegacyImportError`, with a usage error
exiting 2; a new destination is removed when the import fails.

Run the standard-library tests with `PYTHONPATH=src .venv/bin/python -m unittest
discover -s tests -v`.
