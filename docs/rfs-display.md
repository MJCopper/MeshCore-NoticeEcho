# RFS incident headings in the web interface

History and dashboard Recent Notices use the incident type and recorded incident name as the RFS heading. For example, `Not Applicable · MANILLA RD, ATTUNGA` becomes `Haystack fire · MANILLA RD, ATTUNGA`.

The provider's original value remains visible as **Alert level: Not Applicable**, alongside an explicitly labelled **Incident status**. Delivery outcomes such as Dry Run, locally confirmed, unconfirmed and failed remain separate. Not Applicable describes the provider's alert-level field; it is not a delivery outcome or an exclusion decision.

Meaningful alert levels, including Emergency Warning, Watch and Act, Advice, Planned Burn and unfamiliar provider levels, remain prominent in the heading. Placeholder classifications are not used as headings. Unknown incident types fall back to Incident. Missing incident names fall back to the saved location; if neither is recorded, the heading uses the type alone. Missing alert levels and incident statuses are labelled Not supplied. An incident type already at the beginning of the name is not repeated. Location names and other free text are not filtered for placeholder words.

The RFS source page retains its dedicated Alert level column and original provider values. BOM and Traffic headings are unchanged. Existing History filters continue to use the recorded classifications.

These are web display rules only. Transmitted message formatting, wording, byte limits and recorded text are unchanged. They do not change filtering, revisions, polling, duplicate suppression, delivery or retry behaviour. No database migration, data rewrite or historical backfill is needed. Older records use only their saved fields; the display never guesses missing historical values from current incident data or transmitted messages.

## Implementation validation — 28 October 2026

The shared helper is in [presentation.py](../app/presentation.py), with regression coverage in [test_rfs_display.py](../tests/test_rfs_display.py). Tests cover placeholder and meaningful levels, incident/location fallbacks, duplicate boundaries, unfamiliar classifications, HTML escaping, old records, full pages and refreshed partials, source-page labels, other services and delivery outcomes.

Page-rendering tests verify that settings, History, Transmit log and incident records remain unchanged, including the exact prepared and transmitted text. Existing transmission-wording tests exercise the unchanged radio formatter.

A read-only transaction examined the latest 100 prepared RFS History records in the installed database. All retained records remained unchanged. Examples included Haystack fire at MANILLA RD, ATTUNGA, Fire Alarm at NEW ENGLAND HWY, METFORD and Planned Event at WESTVIEW RD, ARMIDALE. No production data was written.

The full regression suite passed **1,188 tests** in 52.75 seconds, with one existing Starlette/httpx deprecation warning. Chromium checks passed for History and dashboard pages at 390 px and 1440 px in both dark and light appearances (eight combinations). Screenshots were inspected for readable headings, labelled details and delivery outcomes. Long unbroken incident names and long status labels fit without horizontal page overflow; mobile dashboard cards place delivery details below the incident content. Syntax, local documentation links and `git diff --check` also passed.

No radio formatter, delivery or polling code was changed. No application restart, deployment or physical radio transmission was performed.
