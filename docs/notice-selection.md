# Notice selection

Open the BOM, RFS or Traffic settings page. Notice selection is embedded between monitoring and polling. Geographic Coverage remains a separate shared policy.

| Service | Selection dimensions | Additional requirements |
| --- | --- | --- |
| BOM | Notice type/family | Geographic coverage, enabled service, expiry and delivery state |
| NSW RFS | Alert level AND incident type | Geographic coverage, enabled service and delivery state |
| Live Traffic NSW | Feed AND category | Geographic coverage, enabled service, active lifecycle and delivery state |

Each ordinary classification **group** has its own **All in this group / Selected** control. All includes every recognized classification assigned to that group in the active saved catalogue. Only that group's checkboxes are disabled and greyed out. Checked choices survive mode changes, preview, saving and reload, including without JavaScript. Counts and provider examples remain readable.

**Special always remains editable**, separately within every dimension. Unrecognized and Not supplied are included only when their respective checkboxes are checked, regardless of All choices in ordinary groups. Unrecognized means a nonempty provider value outside the saved catalogue; Not supplied means blank or missing. A recognized value with invalid membership fails selection and produces a diagnostic; it does not become Special. Internal invalid Traffic feed identifiers remain diagnostics. Regional roadworks still require the Roadwork feed option.

| Service dimension | Ordinary groups |
| --- | --- |
| BOM type | Warnings; Watches and Advice; Alerts |
| RFS level | Alert levels; Other levels |
| RFS incident type | Fire; Planned activity; Rescue and Medical; Other |
| Traffic category | Fire; Roadworks; Traffic lights; Weather and Flooding; Other |

Group membership is explicit, reviewed catalogue data, never inferred from words during evaluation. Groups provide alternatives within a dimension; separate dimensions still combine with AND.


The catalogue contains 18 BOM families, five RFS levels, 20 RFS incident types and 22 Traffic categories, plus the two special options for each dimension. Not Applicable and Planned Burn are separate RFS levels; operational states such as Under control are not alert levels. Not Applicable is unchecked unless explicitly selected.

## Normalization and raw evidence

Severe Thunderstorm Warning, Detailed Severe Thunderstorm Warning and Severe Thunderstorm Warning – Canberra share one family checkbox. Detailed remains a subtype; Canberra remains a qualifier. Recognition uses reviewed explicit patterns and aliases, with case, spacing and Unicode dash normalization. A title merely containing Warning is not automatically a recognized family. Other hazard qualifiers are retained rather than stripped indiscriminately. Radio formatting retains notice cause and affected locations; the Canberra qualifier is included once when it duplicates a location already supplied.

Raw provider classifications, normalized dimensions and catalogue evidence are stored with new current records and new history decisions. BOM severity, urgency and certainty are preserved and displayed when actually supplied. Classification does not change provider content hashes. Historic delivery decisions are not rewritten; absent historical metadata is not reported as a provider omission. The catalogue does not provide geographic evidence or override jurisdiction checks or occurrence-based location exclusions.

## Migration and catalogue updates

Upgrading leaves legacy rules active until you explicitly save a selection. RFS alert-level choices and Traffic feed choices remain unchanged. The proposed new RFS incident-type and Traffic category groups default to All with both Special options selected, preserving previously unfiltered behavior. BOM exact includes, suffix includes and exact exclusions remain stored internally until migration, with exclusions taking precedence. The normalized BOM proposal requires preview and explicit save because family selection can broaden a legacy exact selection or narrow unknown suffix matches.

Use **Preview without saving** to compare stored current notices. Preview does not fetch feeds, save settings, create history or transmit messages. It shows previous and proposed eligibility, raw labels, family reclassification and other blockers. Geographic matches are shown separately from overall eligibility. Eligibility describes current filters; completed deliveries and queued notices remain subject to deduplication.

Saved policies pin the full catalogue snapshot, including reviewed normalization patterns, group definitions and group membership. A later software catalogue update does not silently change an explicit selection. Choose **Adopt current catalogue**, preview the reclassification, then save. An Unrecognized value can become a recognized value after adoption; selecting Unrecognized alone does not select the new recognized entry. All includes newly catalogued members of its group only after explicit catalogue adoption. Newly introduced groups default to Selected with nothing checked. Group moves and unknown-to-recognized changes are shown in preview. After applying the new selection, compatibility controls and migration notices are hidden. Saving ordinary service enablement or polling settings keeps an adopted selection.

Normal polls, replay/resend, queue guards and settings previews use the shared evaluator. Previously transmitted terminal notices retain the existing geographic-policy closure exception. Service enablement and jurisdiction restrictions still apply. Changing classification settings does not automatically resend completed notices. Operator resend on Troubleshooting explicitly requests that behavior.

## Diagnostics

The settings inventory distinguishes current saved records from observed distinct provider IDs, shows catalogue-only entries, and lists raw variants. Historical revisions of the same ID do not inflate an entry's observed count; an ID that changes classification can appear in multiple entries. Current counts represent records still present in saved feed snapshots, not a guarantee of a currently occurring hazard. Traffic IDs include their feed prefix.

Troubleshooting links to all three inventories and exposes `/troubleshoot/notice-selection` as read-only JSON. History shows recorded selection evidence; current service pages show eligibility under current settings independently from Geographic Coverage. Use Poll now for new provider data, then preview before an explicit resend.

See [the snapshot audit](notice-selection-audit.md) for reviewed collected data and [the regression tests](../tests/test_notice_selection.py) for classification, migration, preview, queue and replay coverage. Tests use isolated databases and radio doubles; physical companion delivery requires testing on the separate device.

## Dimension-wide selection migration

Existing saved dimension-wide policies migrate transactionally at application startup to selection schema 2. The saved recognition catalogue is retained; this migration does not adopt new product classifications. All becomes All in every ordinary group with Unrecognized and Not supplied checked. Selected distributes checked IDs into their groups and retains its Special choices. Preserved inactive checkbox choices also carry across.

The original configuration is backed up under `<service>_notice_selection_before_groups`; migration is idempotent and rolls back on failure. It does not fetch data, create transmission records, rewrite recorded decisions or resend completed notices. Installations still using legacy provider rules keep those rules until explicitly applying the proposed new selection. Older open forms remain accepted and are converted to the grouped schema when saved.
