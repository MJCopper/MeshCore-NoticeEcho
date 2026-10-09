# Settings, geographic coverage and delivery

[Back to installation and setup](../README.md).

## Settings and history

The Settings hub has separate pages for General, BOM, NSW RFS, Live Traffic NSW, MeshCore connection and MeshCore companion controls. Saving one section does not replace settings in another. The History page combines BOM, RFS and Live Traffic NSW records by timestamp and can filter by source, delivery status and date. Source-specific details appear when available. Existing records are copied once into the shared history table on upgrade; the old tables remain untouched as a backup.

Additional services register a stable source ID and label in `app/history.py`, then write through `Database.add_service_history`. Optional source-specific metadata and a registered facet can be shown without adding another history table. The dashboard shows the enabled state, poll interval, and last successful poll for BOM, RFS and Live Traffic. Recent Notices contains completed service submissions, with local, repeat or unconfirmed evidence; Dry Run, queued, failed and excluded entries remain in History.

## Message delivery

Automated notices from all services use one bounded radio queue. A multipart notice enters as a complete group or waits for space; older queued parts are never evicted. RFS Emergency Warnings have the highest priority, followed by BOM, other RFS notices and Traffic. Verification messages follow queued notices.

Each part is **locally confirmed**, **repeat confirmed**, **unconfirmed**, **rejected** or **not attempted**. Local confirmation uses a readable baseline and an advancing companion `flood_tx` counter. Repeat confirmation matches the full outgoing encrypted payload using its channel secret, sender text and explicit timestamp, excluding changing route/path bytes. A matching repeat can confirm a part when statistics are unavailable. Neither outcome guarantees that every intended receiver received it; absence of a repeat does not establish failure.

Unconfirmed submissions continue to the next part after normal spacing. Definite unresolved failures stop the remaining group. Current revision, selection, Dry Run and replay cancellation are checked again before submission and retry. Ordinary polls preserve confirmed and unconfirmed parts of unchanged notices, retrying only unsuccessful or untouched parts. Submission intent and retry reservations are committed before radio commands; restart recovery preserves potentially delivered parts. Packet identity tracking survives reconnects and can recover late repeats after restart within its tracking window.

**MeshCore connection → Transmission confirmation and retries** provides matching repeat detection (on), initial confirmation wait (5 seconds), one optional uncertainty retry (off), retry delay (5 seconds), and late-repeat tracking (60 seconds). Wait ranges are 1–30 seconds; late tracking is 10–300 seconds. Counter requests consume the confirmation deadline. Preparation and the command response have their own bounded waits. Late repeats can update completed History records. Unsupported counter statistics are cached until reconnect.

When uncertainty retry is enabled, NoticeEcho first checks for recovered confirmation, then resends only that part once before advancing. A duplicate is possible. A timeout after submission is ambiguous and does not trigger unrestricted reconnect-and-resend attempts. Definite queue/airtime rejection has separate bounded recovery. Retry policy is saved with the notice and its budget survives subsequent polls and restart. An explicit operator resend creates a new delivery request.

History and the Transmit Log retain each attempt, confirmation source, counter readings, elapsed time and repeat observations. Unconfirmed submissions are not counted as confirmed transmissions. Feed polling success remains separate from transmission evidence. Traffic still records its first live poll as a baseline to avoid a burst of existing notices.

## Broadcast content

BOM, NSW RFS and Live Traffic NSW radio notices target at most two parts. Notice type, cause, warning level, critical status, essential timing and distinct affected locations take priority over detailed descriptions. A third part is permitted only when required locations push the notice beyond two; optional descriptions never trigger it. Notices that cannot meet this policy are recorded as formatting-blocked in History and troubleshooting previews, without omitting locations or marking delivery complete. Full provider details remain available in the application.

Multipart numbering appears first, and only the first part carries the source, action, reference and notice type. Cancellations lead their affected scope; mixed warnings distinguish active locations. The complete source note appears once. Dates and times use the configured local timezone.

## MeshCore setup

1. Open Settings.
2. Open Geographic Coverage and select All NSW, councils or additional location terms.
3. Include BOM forecast district names in the same Additional location terms list. Preview coverage against saved notices, then save; upgraded installations must explicitly activate universal coverage.
4. Configure MeshCore as USB serial or TCP. For USB, choose a path from the always-visible device list; Auto-detect USB refreshes every entry in `/dev/serial/by-id/` without probing it. A serial path can also be entered manually. Live and test channel names load automatically from the chosen companion, and their selected indexes are retained when the radio is offline. Click Save settings to persist the port and channels.
5. Leave dry-run enabled while checking the dashboard and history.
6. Send a manual test before enabling live broadcasts.

NoticeEcho retries a saved but disconnected radio in the background about every 15 seconds. A stable by-id path survives `/dev/ttyUSB*` renumbering after a replug; without one, reconnecting requires the saved device path to remain the same.

### Companion settings

The MeshCore Settings page reads the saved radio's name, firmware, battery, TX power, radio parameters and configured channels from the connected companion. When the radio is connected, you can edit its name, TX power, frequency, bandwidth, spreading factor, coding rate and existing channel names. You can add a private channel in an empty slot with a supplied 16-byte key or a generated key, and remove an unused private channel. A generated key appears only on the creation response so it can be shared with other devices. The public slot and any slot selected as Live or Test cannot be removed. Channel renaming preserves the channel keys. Saved values are read back from the radio; model, firmware, battery and PINs are not editable on this page. The page is intended for a trusted network and does not have a separate device-write switch.

## Universal geographic coverage

All three services use one active geographic policy. Terms are literal whole words or phrases, case insensitive, with HTML, punctuation and whitespace normalised. For example, `New England Highway` can match the traffic road field, but `Hunter` does not match `Hunterville`. Terms do not infer region boundaries or search generic advice/background descriptions. BOM uses provider affected areas and explicit affected-area clauses, RFS uses incident locations and location-bearing names, and Traffic uses roads, suburbs and road details. Cancelled-only BOM scopes cannot qualify unrelated active areas.

All NSW includes established NSW notices without requiring a resolved council. Proven non-NSW notices remain excluded. With All NSW off and no applicable selections, no notices qualify; District names entered as location terms also apply to RFS and Traffic affected-location fields. The optional **Include notices with uncertain geographic coverage** fallback can admit notices without a matching term when council geography or NSW jurisdiction cannot be reliably resolved. With the fallback off, unresolved jurisdiction is excluded even when a location term matches. Provider state, a recognised NSW council, or a point within the bundled NSW boundary snapshot establishes jurisdiction; confirmed interstate states and points outside the snapshot are excluded. Points close to simplified exterior borders remain uncertain. It requires applicable configured coverage and is disabled by default on new installations.

On upgrade, existing service filters remain active until the universal page is saved and activated. The proposed councils combine every saved service selection, including disabled services, and any saved All NSW selection proposes All NSW globally. BOM forecast districts are preserved in the shared location terms; the proposed uncertainty fallback follows the existing BOM setting. The page shows each service's existing settings and a comparison against saved current notices so broadening can be reviewed before activation. Original geographic settings are archived once; individual service forms no longer edit them. New installations begin with all monitoring disabled and empty universal coverage.

**Preview coverage** evaluates geographic eligibility from saved evidence only. It performs no provider fetching, saving, reprocessing or transmission; alert levels, notice types, activity and expiry may still exclude geographically eligible notices. Missing saved evidence is reported rather than guessed. Closure exceptions for previously transmitted notices use the same decision in previews, current diagnostics and History. Save applies coverage to polling, troubleshooting previews/replays and queued-message validation. Confirmed unchanged notices are not bulk resent when coverage changes; use troubleshooting to explicitly resend eligible current notices. Traffic retains its first-live baseline. Explicit BOM cancellations, RFS closure statuses and provider-ended Traffic notices for previously transmitted notices can still be delivered after coverage narrows. Feed disappearance does not establish resolution or road reopening. Failed Traffic council boundary lookups retry on the next poll; identical errors are recorded once and successful recovery is logged.

Existing universal policies with a separate BOM district list show a merged proposal on Geographic Coverage. Terms are combined and deduplicated using the same case, punctuation and whitespace normalisation as matching. The current policy remains in effect until the merged proposal is saved; the preview shows any broadened RFS or Traffic coverage. Saving archives the prior universal policy and stores one version-2 shared list, with no separate BOM district configuration. Up to 400 terms are supported so the previous two 200-entry lists can be preserved without dropping entries. Legacy service-specific settings remain archived for reference.

### Companion path hashes and command console

On **Settings → MeshCore companion**, select 1, 2, or 3 bytes per hop when the connected firmware reports path-hash support. Larger hashes reduce collisions but use more packet space. Saving reads the value back before reporting success.

The collapsible command console at the bottom supports `help`, `info`, `channels`, `stats core|radio|packets`, `path-hash [1|2|3]`, `name <name>`, `tx-power <dBm>`, and `radio <MHz> <kHz> <SF> <CR>`. Use `cli <firmware command>` for the companion's native CLI on protocol 14+; commands vary by firmware. This is a companion console, not a host shell or remote repeater terminal.

Commands share the transmission lock, reject submission while the companion is busy, and have a 15-second execution timeout. A timeout does not prove that a setting was unchanged: refresh before retrying. Native commands refresh cached channel names and sender identity; the page refreshes displayed settings after edits. Output and history are bounded, common key/PIN output is redacted, and command history is kept only in the current page. Do not enter credentials in the console.

### Geographic location-term exclusions

Geographic Coverage accepts **Additional location term exclusions**, one exact phrase per line (maximum 400 phrases, 120 characters each). `Moonbi Street` suppresses an additional-term occurrence of `Moonbi` inside that street name. A separate occurrence in `Moonbi Street, Moonbi`, or a `Moonbi` suburb field, remains valid. Selected councils, All NSW, independent district matches, enabled uncertainty fallback and previous-transmission closure/cancellation exceptions retain their existing behaviour.

Matching ignores case, Unicode presentation, punctuation and whitespace, but requires complete consecutive words within one geographic field. Add `Moonbi St` separately to exclude that abbreviation. Exclusions never search general advice and never veto a whole notice. Preview shows geographic eligibility and suppressed/surviving term evidence without saving or transmitting. Saving affects subsequent eligibility checks; historical decisions remain unchanged and completed notices are not automatically resent. Explicit troubleshooting reprocessing uses the current policy.
