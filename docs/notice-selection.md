# Notice selection

Open the BOM, RFS or Traffic settings page. Notice selection is embedded between monitoring and polling. Geographic Coverage remains a separate shared policy.

| Service | Selection dimensions | Additional requirements |
| --- | --- | --- |
| BOM | Notice type/family | Geographic coverage, enabled service, expiry and delivery state |
| NSW RFS | Alert level AND incident type | Geographic coverage, enabled service and delivery state |
| Live Traffic NSW | Feed AND category | Geographic coverage, enabled service, active lifecycle and delivery state |

Every classification dimension offers **All** or **Selected**. All includes known, future, Unrecognized and Not supplied values. Selected includes only checked stable IDs. Unrecognized means a nonempty provider value outside the saved catalogue; Not supplied means a blank or missing value. Both can be selected independently. Invalid internal Traffic feed identifiers remain diagnostics, rather than becoming selectable categories. Regional roadwork copies still require Roadwork to be selected on Traffic settings.

The catalogue contains 18 BOM families, five RFS levels, 20 RFS incident types and 22 Traffic categories, plus the two special options for each dimension. Not Applicable and Planned Burn are separate RFS levels; operational states such as Under control are not alert levels. Not Applicable is unchecked unless explicitly selected.

## Normalization and raw evidence

Severe Thunderstorm Warning, Detailed Severe Thunderstorm Warning and Severe Thunderstorm Warning – Canberra share one family checkbox. Detailed remains a subtype; Canberra remains a qualifier. Recognition uses reviewed explicit patterns and aliases, with case, spacing and Unicode dash normalization. A title merely containing Warning is not automatically a recognized family. Other hazard qualifiers are retained rather than stripped indiscriminately. Radio formatting retains notice cause and affected locations; the Canberra qualifier is included once when it duplicates a location already supplied.

Raw provider classifications, normalized dimensions and catalogue evidence are stored with new current records and new history decisions. BOM severity, urgency and certainty are preserved and displayed when actually supplied. Classification does not change provider content hashes. Historic delivery decisions are not rewritten; absent historical metadata is not reported as a provider omission. The catalogue does not provide geographic evidence or override jurisdiction checks or occurrence-based location exclusions.

## Migration and catalogue updates

Upgrading leaves legacy rules active until you explicitly save a selection. RFS alert-level choices and Traffic feed choices remain unchanged. The proposed new RFS incident-type and Traffic category dimensions default to All. BOM exact includes, suffix includes and exact exclusions remain stored internally until migration, with exclusions taking precedence. The normalized BOM proposal requires preview and explicit save because family selection can broaden a legacy exact selection or narrow unknown suffix matches.

Use **Preview without saving** to compare stored current notices. Preview does not fetch feeds, save settings, create history or transmit messages. It shows previous and proposed eligibility, raw labels, family reclassification and other blockers. Geographic matches are shown separately from overall eligibility. Eligibility describes current filters; completed deliveries and queued notices remain subject to deduplication.

Saved policies pin the full catalogue snapshot, including reviewed normalization patterns. A later software catalogue update does not silently change an explicit selection. Choose **Adopt current catalogue**, preview the reclassification, then save. An Unrecognized value can become a recognized value after adoption; selecting Unrecognized alone does not select the new recognized entry. All remains broad. After applying the new selection, compatibility controls and migration notices are hidden. Saving ordinary service enablement or polling settings keeps an adopted selection.

Normal polls, replay/resend, queue guards and settings previews use the shared evaluator. Previously transmitted terminal notices retain the existing geographic-policy closure exception. Service enablement and jurisdiction restrictions still apply. Changing classification settings does not automatically resend completed notices. Operator resend on Troubleshooting explicitly requests that behavior.

## Diagnostics

The settings inventory distinguishes current saved records from observed distinct provider IDs, shows catalogue-only entries, and lists raw variants. Historical revisions of the same ID do not inflate an entry's observed count; an ID that changes classification can appear in multiple entries. Current counts represent records still present in saved feed snapshots, not a guarantee of a currently occurring hazard. Traffic IDs include their feed prefix.

Troubleshooting links to all three inventories and exposes `/troubleshoot/notice-selection` as read-only JSON. History shows recorded selection evidence; current service pages show eligibility under current settings independently from Geographic Coverage. Use Poll now for new provider data, then preview before an explicit resend.

See [the snapshot audit](notice-selection-audit.md) for reviewed collected data and [the regression tests](../tests/test_notice_selection.py) for classification, migration, preview, queue and replay coverage. Tests use isolated databases and radio doubles; physical companion delivery requires testing on the separate device.
