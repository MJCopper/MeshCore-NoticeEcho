# Transmitted classification wording

Classification labels in settings, current notices, history and diagnostics describe provider data and filtering. Radio wording is a separate presentation policy; it never changes classification, geographic matching, revision hashes or eligibility.

Whole values matching Not Applicable, N/A, Unrecognized/Unrecognised, Not supplied, Unknown, Other or the old Notice type not supplied fallback are omitted from classification headings. Comparison ignores case, extra whitespace and punctuation. These rules are never applied to locations or free-text instructions. Useful unfamiliar provider wording remains visible; catalogue membership is not required for transmission wording. Operational uncertainty such as road reopening unconfirmed, Window unknown and schedule not supplied remains intact.

RFS keeps meaningful alert levels. If the level is a placeholder, its meaningful incident type becomes the heading; otherwise the fallback is Incident. A cause equivalent to the heading is included only once. Meaningful statuses and locations remain required.

Traffic prefers the raw category when available, then the useful provider title, then Traffic notice. It removes an equivalent leading category from the title only at a word or punctuation boundary. If a placeholder category prefixes the title, that prefix is removed before choosing the title as the heading. Missing raw classification does not promote a parser-generated display category.

BOM preserves useful warning, watch, advice and alert types, including unfamiliar provider event wording. A placeholder-only event becomes Weather notice. Placeholder section classifications are also omitted while keeping affected locations and cancellation scope. Existing thunderstorm subtype and qualifier handling remains active.

Cleaning happens before byte budgeting. The target remains two parts, with a third permitted only for affected locations. Numbering starts each part; identification appears in the first part only. Notices that exceed the existing policy remain blocked rather than dropping required facts. With no meaningful details, the neutral heading points directly to the provider source without empty punctuation or duplicate links.

Dry runs, normal polls and troubleshooting reprocess/resend use the same formatters. Completed revisions remain deduplicated and are not automatically resent because wording changes. Historical text is unchanged. Ordinary failed/interrupted/deferred delivery recovery uses the exact recorded parts for the same revision, so confirmed or potentially delivered parts are skipped and the remainder keeps its original wording. Existing queued entries are unchanged. Explicit resend starts fresh, except existing capacity-deferred work. If a recorded partial notice cannot fit the current byte budget, recovery blocks rather than mixing formats; an oversized legacy failure with no completed parts may be reformatted.

No database migration or additional setting is required. See [the implementation audit](transmission-wording-audit.md) and [regression tests](../tests/test_transmission_wording.py).
