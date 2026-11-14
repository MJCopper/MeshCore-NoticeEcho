# BOM scope implementation validation

Validated on 15 November 2026 using temporary databases, mock radios and Chromium. No running service was restarted, no settings were changed in the configured database, and no radio transmissions were made.

The full Python suite passed: **1,296 tests, four existing dependency deprecation warnings**, in 62.45 seconds. Changed Python syntax, local documentation links and `git diff --check` passed. The browser checks below passed all eight combinations.

## Historical regression evidence

[Saved History fixtures](../tests/fixtures/bom_scope_history.json) retain the November 13–14 cancellation examples and all six November 3 updates. The original raw provider revisions were not retained; tests explicitly reconstruct provider inputs from the available evidence rather than claiming an exact historical feed replay.

* `#2331-L`: Tamworth/Quirindi introduction followed by six cancellation records. Unrelated or unproven district-only partial cancellations are filtered; explicit full cancellation names remaining announced local scope.
* `#2331-J`: Armidale introduction followed by four cancellations. Unrelated areas cannot authorize cancellation of that scope.
* `#2331-F`: all six saved updates remain deliverable under conservative whole-warning comparison.

## Regression coverage

[test_bom_scope.py](../tests/test_bom_scope.py) covers partial/full cancellation, repeated revisions, local reactivation, exclusions, ambiguous geography, named marine sections and unknown broader marine areas. Tests cover shared hazard/classification/timing changes, issue-only refreshes, real content reversions and concurrent reservation of equivalent revisions.

The cancellation switch is tested on/off, across Dry Run/Live and destinations, with mixed active/cancelled content, queued messages and disable/re-enable without backlog. Other tests verify stale queued-update rejection, required-part introduction evidence, exact multipart cancellation recovery, idempotent migration, retention and verified backups, including backup before schema upgrade.

Existing episode, formatting, byte-budget, queue, transmission evidence, clock, database-lock recovery and source regression tests remain in the full suite. Two earlier tests asserting a whole-product cancellation exception without selected-scope evidence were changed to assert filtering. The fixed-link History test now includes a real coverage change rather than treating publication metadata alone as material.

## Browser checks

[browser_bom_scope.py](../tests/browser_bom_scope.py) passed eight settings/History combinations across dark/light themes and 390px/1440px widths. Checks include switch persistence, related versus unrelated cancellations, expanded geographic evidence, read-only page loads, no JavaScript errors, no horizontal overflow and zero radio submissions.

## Existing database audit

The configured database was opened read-only and backed up into two isolated copies. Previous committed code initialized one; the new implementation initialized the other twice. Counts and SHA-256 digests matched across all 20 existing application data tables, including 20,596 History records and the 65 existing settings. The expected additions were the default-on cancellation setting, one scope migration marker and 27 backfilled scope events. No historical message or delivery outcome was rewritten by this change.

Geographic relationships not established by saved/provider evidence remain ambiguous. The implementation intentionally avoids inferring that a forecast district contains a particular selected town. Physical RF testing is separate from these software delivery and recovery checks.
