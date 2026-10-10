# Database maintenance completion audit

Implemented and reviewed 10 October 2026. All items from the approved maintenance proposal are implemented.

| Requirement | Result |
| --- | --- |
| Independent scheduling | Application lifecycle starts a worker independent of enabled/failing service pollers. Successful runs repeat daily; failures retry hourly; schedule survives restarts. Manual checks/backups do not postpone scheduled cleanup. |
| Configurable retention | Troubleshoot exposes 90-day history/log, 30-day event/error/stale-notice defaults and seven retained verified backups. Settings validation is atomic. |
| Delivery protection | Pending/recoverable rows, latest decision/broadcast/completed anchors, active uncertainty/repeat tracking and direct/legacy JSON-linked attempts are retained. SQL NULL regression coverage prevents unlinked log rows from accidentally disabling cleanup. |
| Current-data cleanup | Stale RFS/Traffic rows require two successful absence observations plus a grace period. Related Traffic feed pending deliveries remain protected. BOM expiry must parse; current and pending BOM state remains protected. |
| Bounded work | At most 20 committed batches of 250 rows per table per run. SQL operations, checks, snapshots, scheduler/status reads and result persistence run in worker threads. Overlapping actions are rejected; shutdown waits for active work. |
| Verified backups | SQLite online snapshots, quick/foreign-key verification, restrictive permissions, atomic publication, disk-space checks and rotation only after verification. Failed snapshots do not remove previous good backups or permit cleanup. |
| Health and SQLite housekeeping | Daily quick/foreign-key checks, passive checkpoints and optimize; optional full integrity checks. Database/WAL/reusable space, free disk, protected history and table counts are exposed. |
| Explicit compaction | Offline CLI requires confirmation that the application is stopped, verified backup, disk-space checks and final integrity verification. Compaction is not scheduled and has no live web action. |
| Troubleshooting | Last action, last/next daily run, error, verified backup path/time, health check and per-table removal counts. Redacted diagnostic bundle includes recorded maintenance outcomes. |
| No transmissions | Maintenance has no feed/radio action. Reporting settings do not invalidate reviewed resend previews; actual notices/delivery changes still invalidate them. |
| Documentation | Operator retention/backup/restore/compaction documentation and README backup instructions updated. |

## Regression validation

Final full suite: **945 passed**, with one existing Starlette/httpx deprecation warning. The 33 new maintenance tests cover retention anchors, pending delivery and attempts, current/stale/unknown expiry handling, bounded batching/idempotence, snapshot restore/rotation, failed verification/disk-space/persistence, disabled services/schedules, overlap, event-loop responsiveness, cancellation/shutdown, API validation, preview compatibility and offline compaction. Existing multipart recovery, deduplication, closure, geographic and classification regressions pass.

Python syntax checks, browser maintenance-script syntax (`node --check`), troubleshooting-page rendering and `git diff --check` passed.

## Isolated real-data run

The active local database was copied through SQLite's backup API into an isolated temporary database. The maintenance implementation ran only against that copy. A temporary-directory ownership restriction was resolved by moving the copy into a private temporary directory; no permissions or files on the live database were changed.

| Table | Before | After |
| --- | ---: | ---: |
| settings | 58 | 62 |
| service_history | 3817 | 3817 |
| transmit_log | 0 | 0 |
| errors | 2 | 2 |
| events | 1957 | 1958 |
| bom_current | 1 | 1 |
| alert_state | 0 | 0 |
| rfs_incidents | 474 | 474 |
| traffic_items | 1199 | 1199 |
| history | 0 | 0 |
| rfs_history | 0 | 0 |

Maintenance completed successfully and the snapshot backup passed verification. All **1731 protected history records** were byte-for-byte unchanged as row dictionaries, and all non-maintenance settings were unchanged. No notices/history/logs were old enough to delete under the configured defaults. Four maintenance status settings and one completion event were recorded only in the isolated copy.

## Practical limits

Changes have not been deployed to the separate test device; the live database was not pruned, compacted or restored. Automatic maintenance starts after the updated application starts. Backups beside the database must be copied to separate storage to protect against loss of that volume/device. Protected identity/recovery evidence and legacy migration backup tables may intentionally outlive retention; retention is not a strict total database-size cap. Offline compaction's confirmation flag is an operator assertion and requires other database users to remain stopped.

See [operator documentation](database-maintenance.md) and [focused tests](../tests/test_maintenance.py).
