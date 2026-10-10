# Transmission wording implementation audit

Reviewed 10 October 2026. All eight implementation-plan steps are complete. The change is presentation-only and requires no database migration or operator setting.

## Plan checks

| Requirement | Implementation and evidence |
| --- | --- |
| Explicit placeholder policy | Shared whole-value comparison in `app/brief.py`; case, whitespace and punctuation variants tested. Useful unfamiliar provider labels remain intact. |
| Shared helpers | Label cleaning, heading selection, classification equivalence and bounded prefix removal are independent of selection evaluation. |
| All service formatters | RFS promotes useful incident types; Traffic prefers raw category/title; BOM uses neutral fallback and cleans section classifications. |
| Message limits and structure | Cleaning precedes framing. Two-part target, location-only third-part exception, UTF-8 limits, cancellation placement and identification rules remain tested. Empty details link directly to the source without repeated links or empty punctuation. |
| Every transmission path | Normal pollers, dry-run previews and troubleshooting resend/reprocess use the same formatters. Integration tests cover preview isolation, actual send text, eligibility evidence and completed delivery suppression. |
| Retry continuity | Same-revision ordinary recovery retains exact stored parts, skips completed/potentially delivered parts and never mixes formatter versions. Cross-service integration tests cover partial failure, changed formatter, smaller byte budget, restored budget and completed deduplication. |
| Diagnostic evidence | Raw data, normalized selection dimensions, catalogue IDs and stored historical messages remain unchanged. Integration tests check recorded dimensions and filtering metadata. |
| Collected data review and validation | Read-only snapshot comparison below; 71 new focused tests; complete regression suite passed. |

## Retained-data comparison

A SQLite backup of the local database was reviewed independently of the application runtime. Every retained notice was rendered with the previous and updated formatters at a 126-byte budget. These are formatting comparisons of retained records, not counts of currently eligible transmissions.

| Service | Retained records | Changed wording | Formatting blocks before → after |
| --- | ---: | ---: | ---: |
| BOM | 1 | 0 | 0 → 0 |
| RFS | 473 | 348 | 0 → 0 |
| TRAFFIC | 911 | 5 | 18 → 18 |

RFS retained records contain 348 Not Applicable levels and 38 Other incident types. All 348 changed RFS messages had administrative wording removed; useful incident types were promoted where available. Five Traffic messages improved category-prefix punctuation. No additional formatting blocks were introduced. The 18 existing Traffic blocks continue to enforce the established message-size policy rather than omit required locations or context.

Historical metadata was reviewed across 120 BOM, 999 RFS and 1,754 Traffic records (2,873 total). Missing historic classification evidence was not treated as a provider omission. Recorded classification evidence included 24 RFS Not Applicable levels; existing records were not rewritten. Current and historic counts reflect this snapshot only.

## Actual retained examples

### RFS

Before:

```text
1/2 NSW RFS NEW #0BE7 Not Applicable: Bush Fire; Under control for DONNYBROOK FIRETRAIL, BACK CREEK 2372; Tenterfield council; || 2/2 Reported size: 3479 ha; Agency: Rural Fire Service; check rfs.nsw.gov.au
```

After:

```text
1/2 NSW RFS NEW #0BE7 Bush Fire: Under control for DONNYBROOK FIRETRAIL, BACK CREEK 2372; Tenterfield council; || 2/2 Reported size: 3479 ha; Agency: Rural Fire Service; check rfs.nsw.gov.au
```

### TRAFFIC

Before:

```text
1/2 Live Traffic NSW NEW #5CBA CHANGED TRAFFIC CONDITIONS: Princes Highway, St Peters, at Canal Road. - tidal flow in AM || 2/2 configuration; Reduce your speed; Exercise caution; Inner West council; check livetraffic.com
```

After:

```text
1/2 Live Traffic NSW NEW #5CBA CHANGED TRAFFIC CONDITIONS: Princes Highway, St Peters, at Canal Road. tidal flow in AM || 2/2 configuration; Reduce your speed; Exercise caution; Inner West council; check livetraffic.com
```

## Validation and practical limits

Final complete regression run: **912 passed**, with one existing Starlette/httpx deprecation warning. The 71 new wording tests cover exact placeholders, informative unknown values, location preservation, heading promotion, duplicate boundaries, neutral fallbacks, cancellation sections, UTF-8 limits, the third-part location exception, preview/resend evidence, partial recovery and restoration of a reduced byte budget. Python syntax checks and `git diff --check` passed.

Existing failed/interrupted/deferred notices may deliberately retain old wording while completing their recorded parts. Explicit operator resend starts fresh except existing capacity-deferred work. Completed notices are not automatically resent. Filtering and geographic criteria remain active, including Special selections for missing/unrecognized classifications.

Validation used isolated databases and radio doubles. Changes have not been deployed to the separate test device, and physical MeshCore delivery was not tested in this environment.

See [operator documentation](transmission-wording.md) and [focused regression tests](../tests/test_transmission_wording.py).
