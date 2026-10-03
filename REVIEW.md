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

## Flock 1.0.1 review fixes

The review covered collection and Gateway recovery, storage and measurement,
command permissions and reports, deletion, imports, and deployment rollback.
Regression tests use synthetic events and temporary databases.

1. **Failed voice updates left stale activity accruing.** A failed channel or
   companion transition now disables collection and closes voice conservatively.
   Checkpoints and live reports stay disabled until cached snapshots reconcile.
2. **Queued controls and recovery backdated observation.** Control and default
   recovery timestamps are sampled after acquiring the collector lock, so waiting
   resumes cannot count paused time and snapshots cannot backfill an outage.
3. **Admin checks could miss Discord's acknowledgement window.** Permission
   reads for controls and deletion confirmations now time out after two seconds,
   returning a private busy reply; lookup errors also get a safe private reply.
4. **Cancellation raced with deletion.** Confirmation and cancellation now
   share state checks. A cancellation during permission lookup prevents deletion,
   and an operation already processing cannot falsely report that it was cancelled.
5. **Wide trend titles ran outside the image.** Titles use an ellipsis if they
   still exceed the canvas at the minimum font size.
6. **Storage accepted messages while disconnected.** Both ordinary message
   insertion and reaction counting now require connected collection in storage,
   as voice operations already did.
7. **Short gaps could become quiet days.** Fully watched days no longer allow
   a one-second coverage shortfall. Brief outages or untracked stretches remain
   missing coverage and cannot produce ghost days or quiet-day averages.
8. **Rollback followed a predictable temporary symlink.** Restoration now
   writes through a newly created exclusive descriptor and only removes SQLite
   sidecars once copying succeeds. Failed copies preserve the existing database.
9. **Interrupted restores left undeleted snapshots.** Global and per-person
   deletion now remove matching restore snapshots beside the database, including
   the former fixed name, even when the backup directory is absent. Symlink
   targets and unrelated files are preserved.

The updater/recovery helper lives outside the automatically updated code tree.
Reinstall `deploy/update.py` following the README update instructions to apply
the rollback fix on an existing Pi.

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

Flock 1.0.1: the full standard-library suite passes 354 tests, including the
regressions above. `git diff --check` passes.

The unit tests use fake events and a temporary database. They do not verify
live Discord behavior: a live Gateway connection, server permissions,
slash-command delivery, recovery after a machine reboot, and an import of real
Leland Tracker data are checked separately by the operator.
