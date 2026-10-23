# Current items across BOM, NSW RFS and Live Traffic NSW

All three source pages use the same **Current**, **Upcoming**, **Past** and **All saved** views. Current is the default. The view counts include all provider notices in the saved catalogue, independently of geographic coverage and notice-selection eligibility. Filtering happens before pagination; links retain the selected view.

| State | Rule | View |
|---|---|---|
| Current notice | Present in the last successful feed snapshot, with no explicit end, cancellation, expiry or future start | Current |
| Upcoming notice | Present with a trustworthy future start | Upcoming |
| Cancelled | Explicit provider cancellation | Past |
| Ended | Explicit provider end or recognized terminal incident status | Past |
| Expired | Trustworthy expiry at or before the current time | Past |
| No longer in feed | Absent from a successful complete snapshot for that feed | Past |
| Presence not yet confirmed | Saved evidence does not establish feed membership | All saved |

Cancellation, explicit end and expiry take precedence over absence. Absence takes precedence over a future start. Unconfirmed presence never establishes Current or Upcoming. Missing, malformed or timezone-free dates do not establish time boundaries. Upcoming notices become Current at their start, and notices expire at their end, without requiring another poll. The server captures one time for each listing's classification, counts and rows.

Source freshness is separate from lifecycle. Failed fetches retain the last successful snapshot and display the failed result and the last successful fetch time. A stale snapshot does not establish current provider conditions. Disabled monitoring retains saved observations and shows that monitoring is disabled. A successful empty feed marks its previous items absent; an unsuccessful request does not. Partial Traffic polls update successful feeds only.

Items leave Current on their **first successful missing poll** for all three services. This is a display rule. Existing RFS/Traffic history still records eligible disappearances after two successful missing polls. Disappearance alone never means a fire is resolved or a road has reopened, and does not generate an all-clear transmission.

RFS `Under control` remains a current incident; terminal statuses include `Out`, `Extinguished`, `Resolved`, `Closed` and `Ended`. RFS update strings with unverified timezone are display information, not expiry boundaries. A current Traffic roadwork notice can be between its scheduled work periods: notice lifecycle and scheduled closure activity remain separate. Explicit BOM cancellation records are retained in Past; existing cancellation delivery continues unchanged.

All saved contains the latest observed version of each retained provider item, not every historical revision or transmission attempt. Traffic copies in different feeds retain their distinct identities; existing delivery deduplication still prevents inappropriate duplicate transmissions. Selection and coverage explanations are calculated from current settings and shown separately from lifecycle.

The source catalogue is published atomically for each successful feed snapshot. The existing delivery tables, revision hashes, cooldowns, retries, first-live baseline and queued-message validity checks retain their original roles. Page requests and changing views do not fetch, reprocess or transmit notices.

## Existing installations and retention

The schema upgrade imports available saved BOM/RFS/Traffic records once, without fetching feeds or transmitting. Imported records without established presence appear in All saved as unconfirmed until a successful poll re-establishes their state. Explicitly expired, ended, cancelled or already absent records can still be classified from saved evidence. The upgrade does not fabricate first-observed dates. Older BOM snapshots that were already replaced cannot be reconstructed; retention begins with information available at upgrade. The catalogue upgrade does not change delivery history; normal pre-existing startup recovery still applies.

Catalogue records remain after they leave Current. **Settings → Database Maintenance → Stale notice grace period** controls cleanup (default 30 days since last observed). Cleanup requires absence from at least two successful snapshots and protects related pending deliveries, including matching Traffic provider IDs across feeds. History retention is independent. Current, Upcoming and unconfirmed-presence items are not deleted by this absence cleanup. Catalogue and feed snapshot counts appear in Database Maintenance storage status and are included in database backups.

The shared catalogue adds retained display snapshots to the database. It stores one latest record per service/feed/provider ID, so identical repeat polls do not create a new version on every fetch. The added storage is intentional, particularly to preserve BOM notices after its current snapshot is replaced.

On narrow screens, notice details and Traffic feed health tables scroll horizontally within their panels. These regions can receive keyboard focus for scrolling; the view controls remain outside the scroll region.
