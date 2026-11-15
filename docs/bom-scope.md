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

Automatic updates require a meaningful change for a selected location that is represented in the prepared radio message. New selected locations, changed hazards or intensity (including reductions), and conveyed instructions or explicit hazard timing can qualify. Comparison ignores message headers/references, multipart numbering, punctuation, ordering and duplicate generic hail labels alongside large/giant hail. Transmitted wording and formatting are unchanged.

Changes confined to unrelated location lists, publication metadata, routine expiry extensions, provider classifications absent from the message, or narrative omitted by the formatter do not qualify. A selected location disappearing from a list is not itself a cancellation or an update. Observations still refresh current data, expiry and lifecycle state when no update is sent.

The comparison uses structured required sections from a successful render, rather than raw message-text equality. Recognized independent marine zones retain section attribution; unknown broad warning sections retain shared warning context. For free-text warnings, explicitly localised reports/forecasts do not establish hazards for other selected locations. Shared warning-area hazards still apply. Missing local detail is not interpreted as an all-clear. New detail that the formatter does not communicate cannot justify a repetitive update; an operator can review the full provider warning in the website.

The delivery baseline is separate for episode, destination and Dry Run/Live mode, and retained per selected scope. Suppressed observations and definite failures do not advance it. Complete required-part evidence is needed to advance the information baseline; accepted or potentially transmitted parts retain existing duplicate protection and incomplete notices retain exact recovery. Real hazard reversions and local reactivation remain eligible. Changing only the cancellation setting does not create an active update. Explicit resend remains available.

Queued messages survive unrelated provider refreshes. Changed local information, removal of their required local scope, closure, disablement and existing geographic/destination checks can invalidate them. Recovery uses recorded text, references and remaining parts, including when the current byte budget has changed.

## Upgrade and diagnostics

The scope migration is versioned and idempotent. It backfills only available announcement evidence, leaves historical message text/outcomes and references untouched, and does not use unrelated legacy cancellation records as proof of active scope. Matching pre-scope revisions retain their old suppression anchors rather than being rebroadcast solely because the implementation changed. Information comparison is versioned inside existing scope payloads and needs no new database schema or setting. Recognizable historical flat messages can supply comparison facts read-only; unsupported old formats remain unknown.

History's **BOM geographic scope decision** shows announced active/cancelled areas, the comparison reason and locations with changed information. Saved evidence includes per-scope facts, baseline History references and required-part mappings. Current suppressed items explain that no additional information is conveyed; unchanged polling does not add repetitive History records. Older History entries retain their original comparison explanation. For valid closures, effective coverage identifies the announced local scope and retains the raw current-provider inclusion result separately. Uncertain historical scope remains uncertain.

See [validation results](bom-scope-validation.md) for the saved November examples, regression coverage and isolated upgrade audit.
See [local information validation](bom-information-validation.md) for the subsequent update suppression change.
