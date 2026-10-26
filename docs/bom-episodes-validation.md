# BOM episode implementation validation

Validated on 26 October 2026. The implementation covers the episode/reference and announcement-label plan. Testing uses temporary databases and mock companion/radio interfaces; no physical radio transmission was performed.

## Plan coverage

| Requirement | Implementation and evidence |
|---|---|
| Separate product, episode and revision identity | Persistent product/alias, episode and announcement tables; canonical BOM URL aliases; prefix collision tests and A–Z–AA suffix rollover tests. |
| Reliable episode boundaries | Full cancellation, timezone-aware full-warning expiry and two successful missing snapshots close episodes. Tests cover expiry extensions, reappearance, partial cancellations, unknown dates, duplicate snapshots and ambiguous references. Failed polls do not replace snapshots. |
| Correct audience-specific labels | Integration tests exercise NEW → UPDATE → CANCELLED → NEW, live/Dry Run separation, channel/destination changes, known key changes and stable identity across renames/settings/restarts. |
| Meaningful change detection | Unchanged revisions and issue-time-only refreshes are suppressed. Tests cover real content changes and reversions, universal geography deduplication and legacy council-expansion reconsideration. |
| Cancellation scope | Never-introduced cancellations are inspectable without broadcasting; selected-area cancellations retain their episode while other warning areas remain active. |
| Delivery evidence and recovery | Queue deferral and definite pre-submission failures do not introduce an episode. Submitted/uncertain parts retain duplicate protection. Tests cover partial delivery, restart, late confirmation, exact recorded recovery text, explicit RESEND and partial resend recovery. |
| Concurrency | History insertion and announcement reservation are atomic. Concurrent episode allocation, identical revisions and issue-time-only variants cannot allocate duplicate normal announcements. |
| Payload limits | Single- and multipart references, Unicode byte budgets, multipart location rules and smaller-budget recovery are tested. Required content remains protected by existing formatting policy. |
| Safe troubleshooting | Preview tests verify isolated state, selected live/Dry Run audience, reprocess versus resend behaviour and invalidation after episode changes. Replay cannot allocate episodes or advance observations. |
| Additive upgrade | Tests cover conservative legacy anchors, unchanged historic messages/settings, idempotence and transaction rollback without consuming a suffix. Legacy anchors do not invent live introduction evidence. |
| Maintenance and display | Retention tests preserve counters/identities and pending work. Existing backup checks include the new tables. History and Transmit log rendering tests verify references/actions; source pages expose saved episode references. |

Regression coverage is in [episode tests](../tests/test_bom_episodes.py), [BOM geography tests](../tests/test_bom_geography.py), [council tests](../tests/test_bom_nsw_councils.py), [universal geography tests](../tests/test_universal_geography.py) and [companion settings tests](../tests/test_meshcore_settings.py). The full suite also covers RFS, Traffic, formatting, delivery, troubleshooting and maintenance behaviour.

## Follow-up implementation review

A subsequent review checked the lifecycle store, polling integration, audience identity, atomic reservation, recovery, preview isolation, maintenance and display changes against the plan. It found and corrected an explicit-resend edge case: cancellation resends now use the RESEND label while retaining visibly CANCELLED content and the same episode reference. Regression tests cover full-warning resends in both live and Dry Run modes, selected-area cancellation resends, subsequent duplicate suppression and renewed active updates. No remaining implementation gaps were identified in the reviewed plan scope.

## Retained-data audit

The installed SQLite database was opened read-only and backed up into a temporary copy. The episode schema/bootstrap was applied twice to that copy. Hash comparisons confirmed that settings, service History, Transmit log, alert state and current BOM records remained unchanged. Production records were not rewritten.

Fourteen prepared records for product IDN21033 were replayed using their retained revision identities: one initial NEW, eleven UPDATE decisions and two suppressed duplicate revisions. All retained the provisional reference `#2331-A`. The old records do not retain complete provider payloads, authoritative full cancellations or successful missing-feed snapshots, so this audit cannot determine historical episode boundaries or reproduce historical message content. Time gaps alone were deliberately not treated as new episodes. New polling supplies the boundary evidence for subsequent episodes.

## Test execution

Run the full suite from the repository root with the application requirements, pytest, pytest-asyncio and respx installed:

```sh
python -m pytest -q -o addopts='' --tb=short
```

Final full-suite result after the follow-up review: **1,153 passed** in 50.10 seconds. The environment emits one existing Starlette/httpx deprecation warning. Application syntax, changed documentation links and `git diff --check` also pass.

No deployment or application restart was performed. The additive upgrade runs when the updated application opens its database. See [BOM episode behaviour](bom-episodes.md).
