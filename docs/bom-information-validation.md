# BOM local information comparison validation

Implemented on 15 November 2026. Automatic BOM UPDATEs now require changed information for a selected location that the prepared radio message conveys. Message wording, references, formatting and byte limits are unchanged. See [behaviour and diagnostics](bom-scope.md).

Final validation: **1,331 tests passed in 56.84 seconds**, with four existing dependency deprecation warnings. All **eight browser combinations passed**, with no JavaScript errors, horizontal overflow or radio submissions. Changed Python syntax, local documentation links and `git diff --check` passed. The isolated upgrade audit below passed.

The full-suite command was:

```sh
UV_CACHE_DIR=/tmp/noticeecho-uv-cache uv run --no-project --with-requirements requirements.txt --with pytest --with pytest-asyncio --with respx pytest
```

Validation logs: `/tmp/bom-information-completed-tests.log`, `/tmp/bom-information-complete-browser.log` and `/tmp/bom-information-upgrade.log`. Browser screenshots and results: `/tmp/noticeecho-bom-information-browser/`.

## Saved November 15 dry runs

[The fixture](../tests/fixtures/bom_information_nov15.json) contains actual message text, timestamps and selected scope metadata for episode `#2331-M`. Original raw revisions were not retained. Replay inputs are explicitly reconstructed from those records, rather than claimed to be an exact feed replay.

| Sydney time | Saved History ID | Replay result |
|---|---|---|
| 12:46 | 20713 | NEW retained: Tamworth, damaging winds and large hail |
| 15:09 | 20789 | Suppressed: same local information |
| 15:39 | 20806 | Suppressed: same local information |
| 16:10 | 20825 | Suppressed: same local information |
| 16:51 | 20856 | UPDATE retained: Armidale added |
| 18:53 | 20949 | Suppressed: no changed Armidale information; Tamworth disappearing is not an explicit cancellation |

The two retained messages match the recorded wording, with only the isolated test database's episode reference substituted. Suppressed observations add no History rows and do not advance delivery baselines. Earlier November 3 reconstructed updates now suppress location-list changes while retaining a changed local hazard. Existing November 13–14 cancellation fixtures remain covered.

## Regression coverage

[test_bom_information.py](../tests/test_bom_information.py) covers order/duplicate normalization, negation, escalation and reduction, numeric qualifications, local versus remote reports, omitted provider details, selected location additions/removals, content reversions, conveyed instructions and explicit timing, routine expiry, byte budgets, UTF-8, independent modes/destinations, explicit resend, queue guards, exact recovery, partial outcomes, restart, historical comparison reconstruction and concurrent useful-update reservation.

Reprocess previews obey the same suppression rule, report suppression reasons and leave source state untouched. Explicit resend previews remain available. Older tests that used untransmitted headline/classification changes to demand UPDATE now use actual conveyed hazard changes or explicitly assert suppression. The full suite retains cancellation settings/scope, expiry, retention/backup, geographic selection, database-lock recovery, radio acceptance and other service checks.

## Browser checks

[browser_bom_scope.py](../tests/browser_bom_scope.py) checks settings and History at 390px and 1440px in both themes. It includes an eligible hazard update, an unrelated suppressed refresh and related/unrelated cancellations. Expanded evidence shows location names, update comparison explanations and cancellation scope. Checks require no JavaScript errors, no horizontal overflow, read-only page loads and zero radio submissions.

## Database upgrade audit

The configured database was opened read-only and backed up into isolated copies. Previous committed code initialized one copy; the new implementation initialized the other twice. Row counts and SHA-256 digests matched for all **22 application tables**, including **20,920 History records, 67 settings and 56 scope events**. No schema or settings migration is needed for information comparison. Historical text, outcomes and references were unchanged.

## Practical limits

Only information represented in the existing brief radio format can justify UPDATE. Full provider detail remains available on the website; changing the formatter to transmit additional detail is outside this change. Unknown broad structured sections retain shared context; no unsupported district-to-town containment is inferred. Unsupported legacy message formats retain existing suppression anchors without inventing historical facts. Software delivery evidence does not establish physical RF reception.

No running service was restarted, no configured settings/data were edited and no radio transmissions were made during validation.
