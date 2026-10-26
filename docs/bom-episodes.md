# BOM warning episodes and message references

BOM reuses product URLs, so a product ID alone does not identify a particular storm or warning episode. NoticeEcho tracks an episode separately from the provider product and from each received revision.

An example sequence is:

```text
BOM NEW #2331-A Severe Thunderstorm Warning: …
BOM UPDATE #2331-A Severe Thunderstorm Warning: …
BOM CANCELLED #2331-A Severe Thunderstorm Warning: …
BOM NEW #2331-B Severe Thunderstorm Warning: …
```

`2331` is the short product reference; `A` and `B` are locally allocated episode suffixes. These are NoticeEcho references, not official BOM warning IDs. Suffixes continue through Z to AA, AB, and so on. Product-prefix collisions are detected and disambiguated by extending the prefix. Product identifiers and episode counters survive maintenance so references are not recycled within the database's retained identity history. Independent installations do not share a global reference registry.

## Episode lifecycle

The first observed active warning opens an episode. Changes to hazards, areas and timing continue it. A verified full cancellation, trustworthy full-warning expiry, or absence from two distinct successful BOM snapshots closes it. A later active warning starts the next episode, even when the provider URL and content match an earlier episode. A successful observation before the second absence resets the missing count. Failed requests and disabled monitoring do not establish absence. Clock-based expiry is checked when episodes are evaluated; expiry does not itself send an end message.

Partial cancellations never close the whole episode while active areas remain. A cancellation-only message for the selected geographic scope can be labelled CANCELLED and retain the same reference while the wider product episode remains open. Renewed active content for that audience is UPDATE in that same episode. Missing or timezone-free expiry data and individual hazard-section times are not used to invent a full-warning expiry. A gap between historical messages alone does not establish an episode boundary.

Cancellations are associated by known product identity or an unambiguous provider reference. Unmatched or ambiguous cancellations are retained for inspection without allocating a new active episode or cancelling unrelated products.

## New, Update, Cancellation and Resend

Announcement history is separate for Dry Run and live operation, and for each destination. Destination identity includes the configured companion connection/endpoint, channel, and the channel key's hash when available from channel discovery; stored destination identifiers are hashes. Cached channel identities are used only for the matching companion endpoint. Renaming a channel does not change its audience; a known key change does.

| Situation | Prepared label |
|---|---|
| First eligible announcement of an episode to this mode/destination | NEW |
| Changed revision of an episode already introduced there | UPDATE |
| Cancellation of an episode already introduced there | CANCELLED |
| Explicit operator resend | RESEND (cancellation content stays visibly CANCELLED) |
| Ordinary retry or partial recovery | Original recorded wording and action |
| Unchanged revision or issue-time-only refresh in ordinary polling | No new announcement |

Dry Run records simulated introductions without recording live delivery. Switching to live can therefore produce NEW for the same episode reference. A new destination can also receive NEW. Settings, restarts and mode changes do not create product episodes. Existing geographic-policy rules remain: completed unchanged warnings are not automatically resent by universal coverage changes; legacy council/district expansion retains its existing reconsideration behaviour.

Queue admission alone is not proof of introduction. Capacity deferral or definite failure before transmission leaves the destination eligible for a first NEW. Submitted, potentially transmitted or confirmed parts retain conservative duplicate protections. An optional transmission retry keeps the same message and reference; it is separate from an operator RESEND. A cancellation for an episode never introduced to the corresponding mode/destination does not generate a cancellation broadcast.

The reference appears in a single-part BOM message and in the first part of a multipart message. Existing numbering, UTF-8 byte limits and the location-only third-part exception still apply. Required facts are not dropped to accommodate the reference. RFS and Traffic reference formatting is unchanged.

History records episode reference, action, mode, destination identity, scope and closure evidence with newly prepared messages. Original provider IDs, content hashes and historical message text remain unchanged. Source pages show the episode reference when recorded with their saved provider input. The source-page Current/Upcoming/Past lifecycle remains distinct from episode grouping; see [current items](current-items.md).

## Preview, recovery and upgrades

Troubleshoot previews run on an isolated database and use the selected mode's announcement history. They do not allocate production references, advance observation counters or change live announcement state. Re-process retains ordinary suppression; explicit resend prepares RESEND. Reviewed previews become invalid when relevant episode or delivery state changes.

Replay does not create a missing episode or refresh provider observation age: poll BOM first when episode evidence is unavailable. Normal polling establishes episode identity even for notices excluded by transmission filters.

The additive schema upgrade uses available current observations and conservative suppression anchors for existing prepared messages. Unknown legacy destination/introduction evidence remains unknown rather than being promoted to live delivery proof. Unchanged anchored records are not automatically resent by the upgrade. Older episode boundaries are not reconstructed from gaps. Historical messages keep their old references. Existing partial deliveries retain their exact recorded text, including old references; subsequent newly prepared messages use the new episode format.

Backups include episode and announcement tables. Closed-episode announcement records can be cleaned up after History retention, with pending/recoverable work protected. Product aliases, episode references and sequence counters are retained. See [database maintenance](database-maintenance.md).

Validation uses temporary databases and mock radios. Physical radio transmission is outside this test environment. See [implementation validation](bom-episodes-validation.md) for plan coverage and the retained-data audit.
