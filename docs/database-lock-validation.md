# Database lock recovery validation

Validated on 7 November 2026. Changes implement [the recovery design](database-lock-recovery.md) without changing transmitted message text or moving any installation's database.

The full Python suite passed: **1,257 tests, four existing dependency deprecation warnings**, in 54.50 seconds. Changed Python syntax, local documentation links and `git diff --check` also passed.

| Requirement | Validation |
| --- | --- |
| Polling survives contention | Separate SQLite connections hold real WAL write locks while BOM, RFS and Traffic workers run. All remain alive, roll back failed transactions and resume after release. |
| Error reporting cannot terminate polling | A non-database failure is followed by an independently locked error-log write; the original error remains visible and the worker keeps retrying. |
| Transactions release ownership | Injected mid-statement failure removes earlier uncommitted changes; another connection can write immediately. Cancellation also rolls back. |
| Supervision and shutdown | An unexpected loop exit starts one replacement with backoff, avoids duplicate start/recovery and stops cleanly without another restart. |
| Delivery is preserved | Database lock immediately after mock companion acceptance produces one send. Deferred evidence flushes separately. Multipart recovery skips accepted parts; abrupt process-loss recovery retains potentially transmitted submission intent. |
| Maintenance yields safely | Real lock contention and SQLite progress interruption defer cleanup, preserve rows and release transactions. Scan cursors advance across protected rows and resume in later runs. |
| Backup is bounded | An exhausted copy deadline aborts and cleans temporary files; the next verified backup succeeds without temporary WAL/SHM leftovers. Existing backup rotation/failure tests continue to pass. |
| Repeated operation | Accelerated soak runs 12 lock/maintenance cycles with source polling and delivery-result recording; accepted anchors remain protected. This simulates repeated daily operations rather than claiming a wall-clock multi-day test. |
| Diagnostics survive database failure | Readiness uses runtime state without SQLite queries. Browser diagnostics remain visible when normal database reads fail and show recovery when reads resume. |

## Browser checks

[browser_database_recovery.py](../tests/browser_database_recovery.py) passed eight blocked/recovered combinations across dark/light themes and 390px/1440px widths. There were no JavaScript errors, horizontal overflow or radio submissions. Readiness changed between 503 and 200 while liveness remained 200.

## Larger retained database

A temporary database containing 100,000 old History records was exercised through three cleanup runs. All protected records remained intact. Scans resumed at row IDs 5,000, 10,000 and 15,000 and reported deferred work at the batch limit. Runs took 0.689, 0.661 and 0.653 seconds on this machine. These are local test measurements, not guarantees for other hardware or network storage. SQLite progress/busy budgets cannot interrupt blocked operating-system I/O.

## Upgrade audit

The configured development database was backed up read-only into temporary copies. Previous committed code initialized one copy; the new implementation initialized another twice. Row counts and SHA-256 digests matched for all 20 application data tables, including 65 settings and 16,270 History records after initialization. The existing startup code removes 14 non-NSW BOM rows from this snapshot (16,284 → 16,270); the new implementation introduced no additional data rewrites. New indexes are additive. The source database was not upgraded or modified by the audit.

The reported incident belongs to another installation. Its initial lock holder and storage type remain unconfirmed; the local NFS finding does not establish the remote cause. This work did not deploy, restart services or migrate storage on either installation.
