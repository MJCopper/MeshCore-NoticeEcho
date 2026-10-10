# Database maintenance

NoticeEcho runs daily maintenance independently of BOM, RFS and Traffic polling. The first run is due on startup if no completed schedule is recorded, then every 24 hours after completion. Failures retry after one hour. Disabling scheduled maintenance does not disable explicit operator actions.

On **Troubleshoot → Database maintenance**, configure retention, inspect storage and table counts, run maintenance, create a backup, or run quick/full integrity checks. Actions run in a worker thread with one action at a time; shutdown waits for active work before closing SQLite. No action fetches feeds, reprocesses notices or transmits messages.

| Setting | Default |
| --- | --- |
| History | 90 days |
| Transmission logs | 90 days |
| Events | 30 days |
| Errors | 30 days |
| Stale notice grace period | 30 days |
| Verified backups to retain | 7 |

Retention accepts 1–3650 days; backup retention accepts 1–100 snapshots. Protected records can remain longer than the configured retention.

## Protection and cleanup

Maintenance creates and verifies a consistent SQLite snapshot before deleting anything. Backup or health-check failure stops cleanup. Deletes commit in batches of 250, up to 20 batches per table per run; a large backlog is cleared over subsequent runs. Each batch recomputes protection against current delivery state. Lock or storage failures are reported; previously committed batches remain committed.

History protection retains queued, deferred, failed and interrupted delivery records, the latest decision, latest broadcast and latest completed/potentially delivered broadcast for each service/provider ID, plus records involved in submission, pending uncertainty retry or active repeat tracking. These identity anchors support duplicate suppression and closure notices even after old current records are removed. Transmission logs linked to retained history are preserved, including legacy JSON attempt/log ID links. Unlinked submissions, pending retries and active repeat windows are also protected.

RFS and Traffic records are eligible for removal only after two successful-feed absence observations and the stale grace period. Failed/missing feeds do not establish absence. Related Traffic feed copies with pending delivery are protected together. BOM snapshots require a parseable expiry older than the grace period and an old fetch timestamp; unknown expiries remain. BOM state is removed only after 48 hours beyond expiry and when neither a retained current record nor pending delivery requires it.

Settings, feed health, migration markers, classification backups, troubleshooting jobs and legacy migration backup tables are not pruned. Legacy history tables do not receive new normal service records. Storage figures distinguish database size, WAL size, logical pages, reusable pages and free disk space. Reusable SQLite pages are available for subsequent writes without compaction.

## Backups and recovery

Snapshots are written beside the database in `<database filename>.backups/`. New snapshots use temporary files, SQLite's online backup API, quick and foreign-key checks, restrictive file permissions and atomic publication. Rotation occurs only after successful verification. The UI shows the verified path and timestamp. Snapshots contain complete settings, including connection secrets, and history; retain their restricted permissions.

Copy verified snapshots to separate storage to protect against loss of the device or volume. Backups beside the database protect against application mistakes, not loss of that storage. Restore with NoticeEcho stopped: preserve the existing database and its WAL/SHM files, replace the database with the chosen verified snapshot, remove the old database's WAL/SHM files from the active location, then restart. Do not combine a restored database with an unrelated WAL.

For the standard Docker Compose configuration, a verified live backup can be created with:

```bash
docker compose exec -T wx-echo python -m app.maintenance backup --database /data/wx-echo.db
```

Use the active database path shown on Troubleshoot when your configuration differs. Native installations can run the same module with their installed Python environment.

## Health checks and compaction

Daily maintenance performs quick and foreign-key checks, a passive WAL checkpoint and `PRAGMA optimize`. SQLite's automatic WAL checkpoints remain enabled. A busy passive checkpoint is reported and can finish on a subsequent run; it does not force readers or writers to wait.

Full integrity checks are available on Troubleshoot. Compaction is deliberately offline and never scheduled or exposed as a live web action. Use it only when reusable space warrants reclaiming disk space. Stop NoticeEcho first; the command creates a verified backup, checks available disk space, compacts and verifies the result. It requires explicit confirmation that the application is stopped.

For standard Docker Compose:

```bash
docker compose stop wx-echo
docker compose run --rm --no-deps --entrypoint python wx-echo -m app.maintenance compact --database /data/wx-echo.db --confirm-stopped
docker compose start wx-echo
```

For native installations, stop the service and run:

```bash
python -m app.maintenance compact --database /path/to/notice-echo.db --confirm-stopped
```

The confirmation flag is an operator assertion, not automatic detection of every other process using SQLite. Keep other database users stopped during compaction. Health-check CLI commands are `check` and `deep-check`, with the same `--database` argument. Full check and compaction failures return a nonzero exit code.

See [implementation validation](database-maintenance-audit.md).
