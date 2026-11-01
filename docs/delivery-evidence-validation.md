# Transmission evidence implementation validation

Validated on 1 November 2026 using temporary databases, mock companions, SDK packet parsing and Chromium. The implemented behavior is described in [transmission acceptance and evidence](delivery-evidence.md).

| Area | Coverage |
| --- | --- |
| Shared outcomes | Acceptance, local activity, exact repeat, unknown submission, rejection and no submission; mixed multipart aggregation; neutral acceptance display and observed-activity counts |
| Service behavior | BOM, RFS and Traffic consume shared completion states; accepted and potentially transmitted parts suppress ordinary duplicates; maintenance retains accepted broadcast anchors |
| Retry | Default off and saved settings retained; accepted commands send once; unknown outcomes have one durable optional retry; late evidence cancels it; pre-send lifecycle guards remain active |
| Packet identity | Automatic retry preserves timestamp and complete encrypted payload; changed channel keys and SDKs without explicit timestamp support suppress retry; shared packet repeats count once across attempts |
| Statistics | Two-second bounds; three-sample budget across attempts; missing/invalid values, unsupported capability, cooldown, resets and timed-out-session quarantine; reconnect and in-flight session changes; no comparison across later sends |
| Recovery | Immediate durable acceptance, restart recovery, late-repeat restoration/upgrades and conservative partial recovery without rewriting transmitted text |
| BOM episodes | Dry Run and Live introductions remain separate; accepted Live introduction allows subsequent UPDATE/cancellation for its destination |
| Operator UI | Read-only statistics API, shared busy guard, failure reasons, SDK version, evidence detail and honest legacy-success labels |

The full Python suite passed: **1,240 tests, four existing dependency deprecation warnings**, in 52.31 seconds. Changed Python syntax, documentation links and `git diff --check` also passed.

The optional [browser check](../tests/browser_delivery_evidence.py) passed all **20 page/theme/viewport combinations**. It covers Dashboard, History, Transmit Log, MeshCore connection settings and Troubleshoot in dark/light themes at 390px and 1440px widths. It verifies neutral acceptance, legacy evidence labels, the default retry control, successful and busy statistics checks, no JavaScript errors, no horizontal overflow, and zero channel-message submissions.

## Existing database audit

The configured database was opened read-only and backed up into isolated temporary files. One copy was opened using the previous committed application; another was opened twice using this implementation. Row counts and SHA-256 digests of all 20 application data tables matched between those startup results. The new `delivery_evidence_v2` migration marker appeared once, and the delivery trigger included accepted outcomes after both opens.

The comparison includes 65 settings, 13,271 History records, two current BOM records, 11 BOM episodes and four announcements. The source had no Transmit Log records, so tests additionally seed historical and active delivery evidence in temporary databases. The previous startup code already removes eight old non-NSW BOM History rows from this database copy (13,279 → 13,271); this change introduced no additional deletions or historical message rewrites. The configured source database was not upgraded or modified by this audit.

Message formatters and part-spacing rules are unchanged. Existing wording, byte-budget, multipart, terminal, clock and source regression tests remain in the suite.

## Physical validation

No serial companion is available in this environment. Actual firmware timing, local RF activity and heard-repeat propagation remain unverified on hardware. Follow [physical companion checks](troubleshooting.md#physical-companion-checks) with controlled messages before relying on those observations in deployment. No running service was restarted or deployed for these checks.
