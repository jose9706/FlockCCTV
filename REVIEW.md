# Review and validation

## Review findings addressed

These findings come from the review of Leland Tracker, the single-person bot
Flock CCTV is built on. Each fix applies to every tracked person in Flock.

1. **Reconnect recovery could let stale voice intervals advance.** Collection now
   stays disabled until database reconciliation succeeds. Reports suppress live
   extrapolation during recovery, and shutdown preserves the last reliable
   checkpoint. With several tracked people, recovery closes every open segment
   at its own checkpoint and clears every company roster.
2. **Manual backups survived data deletion.** The setup guide uses named manual
   and pre-restore snapshots. Deletion removes these, daily backups, abandoned
   temporary files, and SQLite sidecars, whether it erases everyone's data or one
   person's, because every backup holds that person's data. Copies with other
   names or outside the managed backup directory remain operator-managed.
3. **Compaction failure could make committed deletion look unsuccessful.**
   Database compaction is best effort after the deletion transaction commits.
   If compaction fails, statistics remain deleted. After a global deletion
   collection remains paused; a per-person deletion never pauses collection.

## Current behavior checks

- Guild, channel, and tracked-person filters apply before message and voice
  collection: only people on the tracked list are counted, bots are never
  tracked, and nothing is recorded before a person's own tracking start. Message
  bodies are never stored. The Leland-only evil mode reads eligible message text
  to repost it and, like the direct-mention reply, runs only when
  `LELAND_USER_ID` is set and only for that user.
- SQLite work uses one serialized worker thread. Database identity and timezone
  are checked at startup. A process lock prevents two instances from sharing one
  database.
- Voice moves preserve a visit. Startup, reconnect, pause, untracking, and
  shutdown truncate uncertain visits without awarding a longest-visit record.
  Coverage gaps remain visible through outages.
- Each person has at most one open voice segment, and a join or leave in a
  channel updates the company roster of every tracked person there.
- Untracked stretches show as missing coverage for that person, never as quiet
  time or ghost days, and an untrack and re-track never bridges a visit.
- Per-person deletion keeps other people's shared time but re-keys it to a
  "Deleted person" member, so the erased ID no longer appears in their history.
  Global deletion keeps the tracked list.
- One person's voice or roster event credits every other open segment through
  the same moment, so the shared coverage checkpoint never runs ahead of an open
  segment and a restart cannot leave watched time without its voice time.
- Controls enforce the configured owner and extra admin IDs, and the Leland user
  cannot be an admin. Data-deletion confirmation is scoped to its requester and
  checks access again before acting.
- Per-person reports follow the same private and public reply rules as before,
  and voice-channel names are shown only to an audience that can view them.
- The Leland Tracker import never modifies its source, refuses to merge into an
  existing database, and verifies row counts before it reports success.
- SIGTERM closes the client and database. Startup and daily maintenance create
  consistent backups. Retention preserves daily aggregates and records.

## Validation snapshot

Validation snapshot: see the latest release notes.

The unit tests use fake events and a temporary database. They do not verify
live Discord behavior: a live Gateway connection, server permissions,
slash-command delivery, recovery after a machine reboot, and an import of real
Leland Tracker data are checked separately by the operator.
