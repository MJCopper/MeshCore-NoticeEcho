# Database lock recovery

BOM, NSW RFS and Live Traffic NSW share supervised polling behavior. An exception is recorded in memory and process logs first. Failure to save an error to SQLite cannot terminate polling or replace the original failure. Lock errors do not attempt a second failing error-log write.

Unexpected poll failures retry after 5, 15, 30 and then at most 60 seconds. A successful iteration restores the saved normal interval. Manual Poll now can wake a waiting worker. Settings reads, startup queue recovery and poll execution use the same error boundary. Startup queue recovery completes once per worker lifetime; restarting its loop does not repeat recovery against an active transmission queue.

The supervisor retrieves unexpected loop failures and restarts the loop with bounded backoff. Duplicate starts reuse the same task. Intentional shutdown prevents further restarts. This protects against permanently stale polling after a temporary lock has disappeared.

## Transactions and delivery

Application write scopes commit on success and roll back on exceptions or interruption, under the shared connection mutex. Synchronous application SQLite waits are capped at 500ms. Maintenance coordinates its writes through the same mutex and uses smaller waits and bounded query work. Network and radio operations are outside database transactions. No automatic transaction retry repeats a radio operation.

If SQLite becomes locked after companion submission, delivery bookkeeping is retained in memory and retried separately. A background task checks every five seconds; polling and new submission admission also try to flush it. New submissions cannot proceed past pending bookkeeping that still fails. The buffer contains only database updates, never radio commands.

Submission intent is committed before radio bytes are sent. If the process exits before buffered evidence is saved, startup recovery conservatively treats that submission as potentially transmitted, preserving duplicate protection. Accepted parts remain completed; untouched parts of a partial notice remain eligible for ordinary recovery. Recorded message text, references and transmission retry policy are unchanged.

## Diagnosing the original lock

Troubleshoot exposes worker readiness/restart state, bounded recent SQLite failures with error code/name and operation duration, deferred delivery-write count, last database recovery, database path/journal/filesystem type and maintenance stage/progress. Runtime diagnostics are stored outside SQLite so a lock cannot hide them. SQL parameters, notice contents and credentials are not included in contention records.

`GET /readyz` uses runtime state without probing or writing to SQLite. It returns 503 when any polling worker is absent, stopped, restarting or backing off, or when delivery writes remain pending. Disabled services still have living idle workers. `GET /healthz` remains a pure event-loop liveness endpoint. Docker's unhealthy status alone does not restart a container; polling recovery is handled by the application's supervisor.

For an incident on another installation, save its process logs and runtime diagnostics around the **first** lock error. Compare the failing operation with maintenance stages and check the reported filesystem. The initial lock holder cannot be identified from a later stale-poll warning alone.

SQLite WAL requires supported local storage; SQLite documents that WAL does not work over network filesystems. Confirm the affected installation's storage before attributing its fault to NFS. See [SQLite WAL documentation](https://sqlite.org/wal.html). Moving an installation's database requires a verified backup, a stopped application and a controlled restore; no storage migration is performed automatically by this change.

## Validation

Regression tests in [test_database_lock_recovery.py](../tests/test_database_lock_recovery.py) use separate connections to temporary file-backed WAL databases. They exercise source recovery, failure to log failures, transaction interruption, supervisor restart/shutdown, database-independent readiness, post-acceptance bookkeeping, crash recovery, multipart recovery, maintenance contention/query interruption, scan resumption and backup deadlines. An accelerated soak repeats 12 lock/maintenance cycles with polling and delivery recording.

The optional [browser check](../tests/browser_database_recovery.py) exercises blocked and recovered diagnostics in both themes at mobile and desktop widths. See [validation results](database-lock-validation.md) for full-suite, upgrade and larger-dataset checks.
