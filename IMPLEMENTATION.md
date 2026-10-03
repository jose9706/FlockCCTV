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
`token: str` (excluded from repr), `guild_id: int`, `target_user_id: int`,
`owner_user_id: int`, `admin_user_ids: frozenset[int] = frozenset()`,
`output_channel_id: int | None`, `text_channel_ids: frozenset[int] | None`,
`voice_channel_ids: frozenset[int] | None`, `timezone: str`, `database_path: Path`,
`backup_dir: Path`, `public_report_channel_ids: frozenset[int] | None = frozenset()`,
`checkpoint_seconds: int = 60`, `retention_days: int = 90`.
`Config.from_env()` validates required values, explicit nonempty channel allowlists,
IDs, timezone, distinct safe backup/database locations, and positive numeric settings.
The database is tied to its original guild, target, and timezone. Use the same IDs
and timezone when starting an existing database.
Environment: DISCORD_TOKEN, GUILD_ID, TARGET_USER_ID, OWNER_USER_ID,
ADMIN_USER_IDS (optional comma-separated extra admin IDs), OUTPUT_CHANNEL_ID,
PUBLIC_REPORT_CHANNEL_IDS (blank private, `*` public everywhere, or CSV channel IDs),
TEXT_CHANNEL_IDS and VOICE_CHANNEL_IDS (default `*`, represented as None, for all
bot-visible guild channels), OUTPUT_CHANNEL_ID is optional,
TIMEZONE (America/Costa_Rica),
DATABASE_PATH (data/tracker.sqlite3), BACKUP_DIR (data/backups),
CHECKPOINT_SECONDS (60), RETENTION_DAYS (90).
TLDR_MODEL_URL (optional loopback-only llama-server http(s) base URL; blank disables TL;DR; `Config.tldr_model_url`).
`OWNER_USER_ID` is the immutable recovery owner. SQLite admin decisions override
the optional environment admin list without changing the frozen config.

## Storage (storage.py and stats.py)

`Store(path: Path, backup_dir: Path, timezone: str)` exposes async methods:

- `initialize(now: float, guild_id: int, target_user_id: int)` creates schema,
  validates database identity/timezone, closes stale open voice segments at their
  checkpoint as incomplete, and marks coverage lost since the last checkpoint.
- `close()` releases resources.
- `state() -> dict`: `paused: bool`, `paused_by: str | None`,
  `tracking_since: float`, `last_checkpoint: float | None`, `evil_mode: bool`,
  `reaction_mode: bool`.
- `set_paused(paused: bool, actor_id: int, now: float)` persists state; collector
  closes voice/coverage before pausing. Authorization is checked by commands.
- `set_evil_mode(enabled: bool)` persists the optional message-repost setting.
- `set_reaction_mode(enabled: bool)` persists the reaction setting and starts a
  fresh random 15–25 message interval when enabled.
- `add_message_with_reaction(..., ordinary: bool) -> tuple[bool, bool]` inserts
  deduplicated message metadata and advances the saved reaction interval in the
  same transaction, returning whether insertion succeeded and a reaction is due.
- `admin_override(user_id: int) -> bool | None` reads a persisted decision or
  falls back to the environment list when absent. `admin_overrides() -> dict[int, bool]`
  supports the private list command. `set_admin_override(user_id: int, enabled: bool)`
  persists an owner-issued grant or revocation.
- `add_message(message_id: int, channel_id: int, created_at: float) -> bool` inserts
  deduplicated metadata and increments aggregates transactionally; respects pause.
- `voice_transition(channel_id: int | None, now: float, complete_start: bool = True)`
  opens/closes/splits one active visit; tracked-to-tracked moves retain visit ID;
  `complete_start=False` means original join unknown. Optional `companions` is a
  set of currently present human member IDs. Same channel is a no-op.
- `companion_transition(channel_id: int, member_id: int, joined: bool, now: float)`
  attributes observed time through the peer's channel transition, then updates
  the current roster. Events outside the target's current channel are ignored.
- `company_totals(period: str, now: float, *, include_live: bool = True)` returns
  attributed `seconds` (split evenly among peers present) and `full_seconds`
  (the whole shared time per peer) by channel and member ID (`0` means alone). Schema 7
  added `full_seconds`; schema 9 raises any row's `full_seconds` to at
  least its split `seconds`, so earlier time counts as its split share, including
  on a day that spans the upgrade. Daily
  totals survive detail retention; deletion removes them and the current roster.
- `disconnect(now: float)` closes active segment at last checkpoint as incomplete,
  closes connected coverage conservatively and marks unavailable.
- `connect(now: float)` opens coverage interval unless paused; idempotent.
- `checkpoint(now: float)` updates active voice and connected coverage checkpoints.
- `stats(period: str, now: float, *, include_live: bool = True) -> dict`: `messages: int`, `voice_seconds: float`,
  `voice_visits: int`, `active_days: int`, `tracking_since: float`,
  `gap_seconds: float`, `paused: bool`. Periods: today/week/month/all.
  Clip live duration to now only while connected; split at local day boundaries.
  Reports pass include_live=False while runtime reconciliation is incomplete.
- `message_channel_counts(period: str, now: float) -> dict[int, int]` returns
  stored target message counts by channel ID within the period bounds.
- `latest_messages(period: str, now: float, channel_ids: Collection[int], limit: int) -> list[dict]`
  returns up to `limit` rows (`message_id`, `channel_id`, `created_at`) in the
  given channels and period, newest first; empty channels or `limit <= 0` yield `[]`.
- `company_daily(period: str, now: float, *, include_live: bool = True) -> list[dict]`
  returns company rows by `day`, `channel_id`, and `member_id`, with live time
  split by local day; `company_totals` sums them.
- `daily_trend(period: str, now: float, *, include_live: bool = True) -> list[dict]`
  returns one row per local day in the period from the tracking start (`day`, `messages`,
  `voice_seconds`, `voice_visits`, `watched`), zero-filled, with live voice
  split by day under the same rule as `stats`. `watched` is true only for a
  finished day fully covered by coverage intervals.
- `period_comparison(period: str, now: float, *, include_live: bool = True) -> dict`
  returns `current` and `previous` window totals (`start`, `end`, `messages`,
  `voice_seconds`, `voice_visits` with a complete start, `watched_seconds`) from
  retained detail, using `stats.previous_period_bounds` (same local day offset
  and clock time). Live coverage counts through now like live voice. `previous` is `None`
  with `reason` `all`, `untracked`, or `pruned` when no fair comparison exists.
- `message_times(period: str, now: float) -> dict` returns retained message
  send times (`times`, oldest first), `since` (where retained detail starts
  within the period), and `period_start`.
- `voice_hours(period: str, now: float, *, include_live: bool = True) -> dict`
  returns observed voice seconds by local hour (`hours`) from retained segments,
  clipped to the same `since`, and `period_start`.
- `records(now: float, *, include_live: bool = True) -> dict`: `busiest_day: str | None`,
  `busiest_day_messages: int`, `longest_visit_seconds: float`,
  `longest_visit_at: float | None`, `current_visit_seconds: float | None` (observed
  length so far of a live visit), and `current_visit_complete_start: bool`. Only
  fully observed closed visits win records. When reconciliation finds the target
  in the same channel within `VISIT_BRIDGE_SECONDS` (120) of a visit cut short by
  a disconnect or restart gap, the visit continues; the gap stays uncounted.
  Startup reapplies this rule to retained visits and only raises the record.
- `last_voice(now: float, *, include_live: bool = True) -> dict | None` returns
  the latest channel/time and whether the target is currently observed there.
  The latest observation survives detail retention and is cleared by deletion.
- `delete_data(actor_id: int, now: float)` removes stats and local managed backups,
  resets tracking start, turns evil and reaction modes off, leaves collection paused by the invoking
  admin, and clears open state. Admin decisions remain in SQLite.
- `maintenance(now: float, retention_days: int)` rolls up/prunes details without
  changing totals, uses SQLite backup API, retains seven managed daily backups.

Storage owns daily aggregates/retention; stats.py may contain pure date helpers.
Keep deletion/backup operations serialized. Never prune before preserving records
and totals. Replay deduplication applies within detailed retention; reject messages
older than the retention cutoff once their IDs have been pruned.

## Service (collectors.py)

`Tracker(config: Config, store: Store)` exposes `connected: bool`, `guild_is_available: bool`,
`collection_ready: bool` (false until storage reconciliation succeeds),
`last_error: str | None`, and async methods:
`ready(voice_channel_id: int | None, now: float | None = None, *, companions=...)`,
`disconnected(now: float | None = None)`, `message(message)` (Discord-like object),
`message_with_reaction(message, *, ordinary: bool) -> tuple[bool, bool]`,
`voice(member, before, after)`, `checkpoint()`,
`pause(actor_id: int)`, `resume(actor_id: int, voice_channel_id: int | None, *, companions=...)`,
`delete_data(actor_id: int)`, `shutdown()`.
Also `guild_unavailable()` and `guild_available(voice_channel_id)` handle configured
guild outages separately from the Gateway connection.
Serialize state transitions with an asyncio lock. Ignore other guilds/users,
channels outside an explicit allowlist, AFK channel, messages predating observation start or pause/resume
boundary. Discord adapter passes only eligible current voice state to ready/resume.
While disconnected ignore voice events (including uncertain replay). On resumed,
reconcile cached current voice and companions, and conservatively start an incomplete segment.
Track pause in storage; configured admins can resume any prior pause.

## Discord adapter (bot.py)

`create_bot(config: Config) -> discord.Client` initializes storage in `setup_hook`,
registers guild command groups, synchronizes only the configured guild, and wires
ready/resumed/disconnect/message/voice events to `Tracker`. Checkpoint, daily
maintenance, avatar-refresh, and update-watch tasks are cancelled/awaited on close.
On every Gateway ready it writes `bot-ready.json` (`ready_at`, `pid`) beside the
database for the updater's health check. Every five minutes it reads the
updater's `update-status.json` there (`update_status.py`) and DMs the owner once
per failure streak, recording the streak start in `update-notified.json`.
`/leland about` adds `update_status.status_line`. `/leland update` (admins only,
private reply) calls `update_status.request_update`, which writes
`update-requested.json` (`requested_at`) there unless the status shows a hold;
`deploy/flock-cctv-update.path` starts `flock-cctv-update.service` while
that file exists, and every polling run of `deploy/update.py` removes it before
taking its lock. Collection
and storage failures surface in status and logs; avatar-refresh failures are
logged. Message payloads and the token are not logged. Gateway
intents: guilds, guild_messages, voice_states, presences, message_content. Use
`AllowedMentions.none()` for bot output. Expose `bot.config`, `bot.store`,
`bot.tracker`, `bot.tree`, `bot.current_voice_channel_id() -> int | None`, and
`bot.current_voice_companions() -> frozenset[int]`
(eligible channels only).

After an eligible message is counted, evil mode reads its text, transforms it to
upside-down Unicode, and posts the result in the same channel. It skips empty text
and does not repost attachment files; text accompanying an attachment is still
reposted. Independently, a direct mention of the bot gets a fixed reply based on
mention metadata, even when collection is paused or that channel is outside the
text allowlist. An hourly task mirrors the target's current server avatar with
inverted colours, using the first frame for animated images; it refreshes if the
source changes or the bot avatar is manually changed.
Reaction mode advances its saved interval only for newly counted ordinary
messages, and adds either 😂 or 👸 to a message when due. Reaction failures are
logged without message bodies; they do not change collected message totals.

`__main__.py` loads Config and starts the bot with normal logging. It logs the
version at startup and answers `--version` before reading any configuration.
`__init__.py` owns `__version__` (semantic release number, read by
`pyproject.toml`) and `version_string()`, which appends the short commit from
`_build.py` (generated and git-ignored; written by `deploy/update.py` during
staging) or, in a development checkout, from `git rev-parse`.

## Commands (commands.py)

`register_commands(bot)` installs the single `/leland` group on `bot.tree`
for the configured guild. Implement stats, records, where, company, leaderboard, trends, online, roast, tldr, help,
about, version, pause, resume, delete-data, evil-mode, reaction-mode, and owner-only admin add/remove/list.
Runtime checks enforce the guild and optional output channel; only the configured
owner and effective extra admins can use controls. Admin decisions in SQLite override
the environment list. Admin management, status, and controls are private. Deletion confirmation is private,
restricted to its requester, rechecks access when confirmed, and expires. Roast
uses a shared cooldown. Defer slow interactions and use followups; errors get a
safe response and logged traceback.
The company pie report filters attribution by source voice channel visibility:
requester's View Channel for private replies, `@everyone` View Channel for public
replies. Missing/deleted channels are omitted. It attaches a PNG generated in
memory; no image or message body is stored. Resolve visible companion names from
the member/user cache, then Discord's member/user API if needed. Use the user ID
only when name lookup fails, and do not persist display names.
`/leland leaderboard period:` (default `all`) uses the company report's
visibility filter and name lookup on `full_seconds`, as text only: top 10
people ranked, ties by user ID, the remainder counted, and alone time listed but not ranked.
`/leland admin list` and the add/remove confirmations use the same lookup to
show each user as display name, username, and ID, escaped for Markdown and
mentions, falling back to "Unknown user" with the ID. Names are resolved per request, never stored.
`/leland trends period: kind:` (defaults last7, daily; `last7` starts at local midnight six days ago) follows general-report
visibility and attaches an in-memory PNG: `daily` and `weekdays` from
`Store.daily_trend` (bucketed by week beyond 62 days, month beyond 62 weeks),
`compare` from `Store.period_comparison`, `hours` from `Store.message_times`
and `Store.voice_hours`, `bursts` from `Store.message_times` (two-minute gap),
and `company` from `Store.company_daily` filtered and named like the company
report. No activity yields a text-only reply.
`/leland tldr period:` (default week) replies privately when `TLDR_MODEL_URL` is
unset, collection is paused, another TL;DR is running (module lock), or the
shared 60-second cooldown is active; otherwise it defers and holds the lock.
Qualifying channels come from `Store.message_channel_counts`: the report viewer
(requester or `@everyone`, as for company) needs View Channel and the bot's own
member needs View Channel and Read Message History; private threads never
qualify. Too few counted messages
skip the model. Otherwise it takes `Store.latest_messages` (≤40), fetches those
IDs via bounded newest-first `channel.history` scans keeping only the target's
messages,
skips channels that raise HTTP errors, cleans texts in `tldr.py`, keeps the
newest within a 4,000-character prompt budget, calls the
local model, validates the JSON, and posts `tldr.format_tldr` output with
mentions suppressed. Neither message text nor model output is stored or logged.

Run the standard-library tests with `PYTHONPATH=src .venv/bin/python -m unittest
discover -s tests -v`.
