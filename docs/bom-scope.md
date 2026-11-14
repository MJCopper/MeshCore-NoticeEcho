# BOM geographic scope and notification changes

BOM uses the same geographic scope evaluator for normal polling, Dry Run, reprocessing and resend. Geographic diagnostics use that evaluator read-only for cancellation eligibility. Classification and service enablement remain independent controls.

## Announced local scope

Scope evidence is kept separately for each product episode, delivery destination and Dry Run/Live mode. It records selected terms, districts or councils, supported provider aliases/identifiers, content comparison and the message parts needed to introduce each scope.

Queue admission is a reservation, not proof of introduction. A scope becomes introduced when its required parts have accepted, observed or potentially transmitted evidence. A failed multipart message therefore cannot automatically introduce every place named in its untouched parts. Evidence uses the existing shared transmission outcomes and does not claim recipient reception.

An additive `bom_scope_events` ledger records observations and announcement provenance. Delivery outcomes are read from the existing announcement/history records, so delayed acceptance and database-lock recovery use the same evidence. Active scope history remains protected during retention; closed episode scope records can be removed after retention when no pending work requires them. Backups include the ledger.

## CANCELLED

A partial cancellation must explicitly match previously announced local coverage. Current location exclusions apply. An episode being introduced somewhere is insufficient. Broad forecast district names are not guessed to contain selected towns or councils when no reliable relationship is available.

An explicit full-product cancellation closes remaining announced local coverage and names that coverage in the notification. It does not copy unrelated areas from the provider's final notice. An all-CAN marine section list closes the product episode, but only explicitly cancelled coasts authorize notifications; other previously active coasts can have expired and are retired silently.

The same active period closes once. Subsequent provider revisions mentioning that cancellation do not repeatedly notify it. If a selected area becomes active again within the same episode, its introduction is an UPDATE and creates a new active period that can later be cancelled. A new product episode still receives a NEW reference.

Expiry and feed disappearance do not themselves generate cancellation notifications. Deselected or disabled cancellation scope is retired without a later automatic backlog. Definite failures retain ordinary recovery, and multipart recovery preserves the recorded text and skips completed/potentially delivered parts.

## Cancellation setting

**Settings → BOM → Send cancellation notifications** defaults to on. When off, standalone cancellations are suppressed and cancellation sections are removed from mixed active updates. Active NEW/UPDATE handling and provider lifecycle tracking continue. History records the suppression reason.

Re-enabling does not automatically replay cancellations observed while disabled. Explicit operator resend can bypass ordinary notification deduplication but still requires scope eligibility and the setting to be enabled. Queued messages recheck the setting, selection and scope state before each submission; already submitted parts cannot be recalled.

## UPDATE

Updates require current selected geographic coverage. An earlier introduction does not authorize forwarding every later revision after the warning moves elsewhere.

Changes to local coverage, shared warning text, hazards, severity, urgency, certainty, onset/end times and expiry remain material. Unknown geographic attribution uses conservative whole-warning comparison and retains warning context. Publication-time-only refreshes, including the recognized BOM headline timestamp prefix, do not create duplicate updates.

Suppression as an unrelated structured change is deliberately narrow: independently named NSW marine coast sections must cover every matched selected scope, all section areas must be recognized, and shared warning fields must remain unchanged. Other structured or free-text warnings retain conservative comparison. Suppression never relies solely on rendered message text, which can omit provider details during byte budgeting.

A separate delivery fingerprint supplements the original provider revision. Real content reversions, reactivation and material shared-field changes remain eligible. Changing only the cancellation setting does not create an active update.

## Upgrade and diagnostics

The migration is versioned and idempotent. It backfills only available announcement evidence, leaves historical message text/outcomes and references untouched, and does not use unrelated legacy cancellation records as proof of active scope. Matching pre-scope revisions retain their old suppression anchors rather than being rebroadcast solely because the implementation changed.

History's **BOM geographic scope decision** shows announced active/cancelled areas, the decision reason and whether comparison retained the whole warning conservatively. For valid closures, effective coverage identifies the announced local scope and retains the raw current-provider inclusion result separately. Uncertain historical scope remains uncertain.

See [validation results](bom-scope-validation.md) for the saved November examples, regression coverage and isolated upgrade audit.
