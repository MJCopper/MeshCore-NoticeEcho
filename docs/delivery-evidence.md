# Transmission acceptance and evidence

BOM, NSW RFS and Live Traffic NSW use the same delivery layer. Service code decides which notice to send; the delivery layer owns submission, evidence, recovery and retry decisions. Message text, references, splitting and normal part spacing are unchanged by this distinction.

| Display | What is known | Automatic uncertainty retry |
| --- | --- | --- |
| Accepted by companion | The companion acknowledged the send command. RF transmission and reception are not established. | Never |
| Local TX activity observed | The companion's overall flood TX counter increased during this attempt. Other radio activity can also increase it. | Never |
| Repeat heard | A received packet exactly matches the complete encrypted channel payload. This establishes propagation, not recipient delivery. | Never |
| Command outcome unknown | Submission began, but no acceptance or stronger evidence was obtained. The message may have transmitted. | Optional, once |
| Rejected / not attempted | A definite rejection or a failure before submission. | Separate bounded recovery where appropriate |

Accepted parts complete normal delivery processing and suppress ordinary repeats on later service polls. They have a neutral display and do not count as observed local TX activity. Multipart notices retain individual part evidence; an accepted part combined with an unknown part leaves the notice unknown. Pending parts remain queued. Restart recovery preserves accepted and potentially transmitted parts rather than blindly sending them again.

## Optional retry and identity

**Retry once when the send command outcome is unknown** defaults to off. Existing saved settings are retained. Enabling it permits one retry only for an unknown command outcome without stronger evidence. A late exact repeat or fresh, valid counter increase cancels a pending retry. The retry budget is reserved durably before waiting, so a restart does not grant another retry.

An automatic uncertainty retry reuses the original timestamp and encrypted payload identity. It requires the same destination, sender, channel key and message, plus SDK support for explicit timestamps. If the original packet identity cannot be reproduced, the retry is suppressed and the reason recorded. A received copy shared by both attempts increments the packet's repeat count once; it is not counted as two separate heard packets. An operator's explicit **Resend** is a new delivery with a new packet identity.

## Counter diagnostics

Packet-statistics reads have a two-second timeout. There are at most three statistics samples for a logical message part, including its automatic uncertainty retry: an initial baseline and up to two follow-ups. A retry needs a fresh baseline within the remaining budget; it cannot compare against a previous attempt. The default observation window after the command response is five seconds. Preparation and command-response time are additional.

Unsupported statistics are cached until reconnect. Other invalid or unavailable responses have a 30-second cooldown. A timed-out statistics request makes the session unsuitable for counter verification until reconnect, because a late reply could otherwise be attributed to a later request. Transmission can still proceed and command acceptance remains meaningful. Counter resets, old sessions and later sends cannot supply counter evidence for an earlier attempt.

On **Troubleshoot → Delivery and radios**, **Check local statistics** performs a bounded read under the shared radio lock. It does not send a channel message or change notice history. It reports the value, failure reason, duration, SDK version and whether the sample is usable for verification. A successful manual read after a timeout does not restore verification trust; reconnect first. Busy and disconnected states are reported directly.

Heard-repeat detection defaults to on, with a 60-second late observation window. Missing repeats can reflect topology, subscriptions or direct reception without retransmission. Neither unavailable counters nor missing repeats alone mean the companion rejected an accepted command.

## BOM episodes, Dry Run and existing databases

For BOM, accepted Live delivery introduces an episode to that destination. A changed revision can subsequently be an UPDATE and an eligible cancellation can be CANCELLED. Dry Run introduction belongs only to its own mode and destination; the first Live introduction is still NEW. Queue admission or definite failure before transmission does not introduce an episode. Potentially transmitted parts retain conservative duplicate protection. See [BOM episodes](bom-episodes.md).

Evidence records carry a version and provenance. Upgrade adds support for accepted outcomes and updates the episode-delivery trigger once, transactionally. Completed historical outcomes and recorded message text are not bulk rewritten. Historical generic success without recorded RF evidence is displayed as **Completed — legacy success (RF evidence not recorded)** rather than presented as a newly observed counter increase.

## Validation

Automated tests cover acceptance without counters, multipart aggregation and restart recovery, all three services' duplicate suppression, unknown-outcome retry limits, exact packet identity, late repeats across attempts, statistics timeouts/cooldown/reset/session isolation, BOM Dry Run/Live introduction and read-only diagnostics. Browser checks exercise the changed controls and evidence display. See [implementation validation](delivery-evidence-validation.md) for coverage and the retained-data audit. Physical companion checks remain necessary to validate actual firmware timing, local RF activity and heard repeats on a real network.
