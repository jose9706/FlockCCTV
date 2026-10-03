# Review and validation

## Review findings addressed

1. **Reconnect recovery could let stale voice intervals advance.** Collection now
   stays disabled until database reconciliation succeeds. Reports suppress live
   extrapolation during recovery, and shutdown preserves the last reliable
   checkpoint.
2. **Manual backups survived data deletion.** The setup guide uses named manual
   and pre-restore snapshots. Deletion removes these, daily backups, abandoned
   temporary files, and SQLite sidecars. Copies with other names or outside the
   managed backup directory remain operator-managed.
3. **Compaction failure could make committed deletion look unsuccessful.**
   Database compaction is best effort after the deletion transaction commits.
   If compaction fails, statistics remain deleted and collection remains paused.

## Current behavior checks

- User, guild, and optional channel filters apply before message and voice
  collection. Evil mode reads eligible message text to repost it but does not
  store message bodies. Direct-mention replies use mention metadata and also work
  while collection is paused or the channel is outside the text allowlist.
- SQLite work uses one serialized worker thread. Database identity, target, and
  timezone are checked at startup. A process lock prevents two instances from
  sharing one database.
- Voice moves preserve a visit. Startup, reconnect, pause, and shutdown truncate
  uncertain visits without awarding a longest-visit record. Coverage gaps remain
  visible through outages.
- Controls enforce the configured owner and extra admin IDs. Data-deletion
  confirmation is scoped to its requester and checks access again before acting.
- SIGTERM closes the client and database. Startup and daily maintenance create
  consistent backups. Retention preserves daily aggregates and records.

## Validation snapshot (2026-09-26)

On Debian 13, Python 3.13, aarch64:

- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`: **78 tests passed**.
- `pip check` passed.
- `flock-cctv.service` is active and running in the current environment.

The service process being active does not verify live Discord behavior. A live
Gateway connection, server permissions, slash-command delivery, and recovery
after a machine reboot have not been recorded as acceptance checks.
