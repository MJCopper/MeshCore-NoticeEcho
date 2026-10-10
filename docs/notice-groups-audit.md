# Group selection implementation audit

Reviewed 10 October 2026. Service pages now expose independent modes for each ordinary classification group. Special remains independently selectable, even when every ordinary group uses All. Stable membership and group definitions are pinned in catalogue snapshots. Catalogue adoption previews report group moves and reclassification; new groups start unselected.

The SQLite data comparison used a backup made through a read-only connection. No live settings, notices, history or radio work were changed by the audit.

| Service | Retained classifications compared | Retained selection changes | Historical records compared | Historical selection changes |
| --- | ---: | ---: | ---: | ---: |
| BOM | 1 | 0 | 119 | 0 |
| RFS | 473 | 0 | 980 | 0 |
| Traffic | 909 | 0 | 1752 | 0 |

These comparisons use each saved explicit policy, or the previously defined proposal when the installation still uses legacy provider rules. Legacy provider rules themselves remain active until explicit application. Historic records with unavailable classification fields are not fabricated. No catalogue adoption was combined with migration.

Implemented: explicit reviewed membership; schema/version and original-policy backup; transactional repeatable startup migration; grouped settings validation; per-group controls and choice preservation; independently editable Special; snapshot adoption for additions/moves/new groups; shared poll/replay/guard evaluation; recorded group and mode evidence; backwards-compatible historical rendering and old-form submission; operator documentation.

Tests cover all catalogue memberships, independent group choices, Special, AND dimensions, migration equivalence, startup backup/idempotence, group validation, fail-closed membership, catalogue adoption, read-only preview, disabled-input preservation and replay queue invalidation. Existing cancellation, closure, provider-revision and delivery-deduplication regressions remain in the complete suite. Physical-radio delivery and deployment on the separate test device are outside this local audit.

Final validation: **833 tests passed** in 33.95 seconds in the isolated Python 3.12 development-container copy, including 96 group regressions. The existing Starlette/httpx deprecation warning remains. Python syntax, JavaScript syntax and group-toggle/form-preservation behavior passed; `git diff --check` passed. The read-only retained/history comparison was repeated after implementation and still reported zero selection changes.
