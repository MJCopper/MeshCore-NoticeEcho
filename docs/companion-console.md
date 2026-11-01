# Direct companion CLI

The MeshCore Settings console sends a single command directly to the connected local companion using the native CLI protocol. Enter the same text you would enter in the MeshCore app, such as `get name`. Do not add a `cli` prefix. Commands, arguments, spaces and quotes are forwarded as entered; NoticeEcho does not translate them or maintain a whitelist of firmware commands. The console does not execute server shell commands or invoke the desktop meshcore-cli dispatcher.

Native CLI requires a compatible SDK and companion protocol 14 or newer, introduced in official companion firmware 1.18. Custom firmware determines which individual commands it implements. See the [official release description](https://blog.meshcore.io/2026/10/15/release-1-18-0) and [firmware command handler](https://github.com/meshcore-dev/MeshCore/blob/dev/examples/companion_radio/MyMesh.cpp).

## Using the console

Send with Enter or the Send button. The transcript shows the entered command and the companion's response. Up/Down recalls commands and restores an unfinished draft. Clear output clears the transcript. History is bounded and kept only in the open page; refreshing the whole page clears it. Refresh connection updates the connection/settings view without losing the transcript.

Connected, waiting, busy, unsupported, disconnected and timed-out states are displayed separately. If the full settings/channel snapshot cannot be read but the link is connected, submission still checks native CLI capability directly. A failed settings snapshot does not by itself disable the console.

Responses retain their text, line breaks and whitespace, including empty replies, `Unknown command`, firmware error messages, and explicitly requested sensitive values. A received reply is not labelled as proof that a configuration change succeeded. Output is rendered as plain text, so firmware-returned HTML cannot run in the browser. The console accepts one nonempty command of at most 200 UTF-8 bytes without control characters. This is the console's input bound; firmware may impose additional restrictions.

## Connection and configuration handling

The console shares NoticeEcho's existing companion connection. Commands are serialized with settings/clock operations and are refused while a multipart notice is active. There is no second serial connection and no automatic command retry.

The complete native exchange, including its capability query, has a 15-second outer timeout; SDK timeouts can return earlier. A timed-out, interrupted or malformed exchange drops the old SDK session before another command can run. The normal connection-maintenance process reconnects it; use Refresh connection to check readiness. This isolates old replies from the next command's listener. Reboot/poweroff commands may therefore report an unknown outcome if the device disconnects before replying, even when the action took effect. Commands are never repeated automatically.

Commands outside the known read-only forms conservatively invalidate cached sender information and trigger a bounded refresh of sender/channel state. A refresh failure is shown separately from the original device reply; the connection is reset and stale channel cache is cleared. Existing automatic clock policy remains in force. Dedicated settings and clock controls continue to use their existing binary protocol operations; old web-console aliases and shell quoting rules are not applied to native commands.

Device commands apply to the real companion even when service transmission is in Dry Run. The transcript is not written to History, Transmit log or persistent command storage. SDK logging is temporarily suppressed during the exclusive exchange/cache refresh so raw commands, keys and returned values are not copied into process logs.

## Validation — 1 November 2026

Regression tests in [test_standard_companion_cli.py](../tests/test_standard_companion_cli.py), [test_meshcore_settings.py](../tests/test_meshcore_settings.py) and [test_companion_clock.py](../tests/test_companion_clock.py) cover:

- Exact forwarding of ordinary/custom commands, quotes, spaces, Unicode and former web aliases; no dispatcher or host execution.
- Verbatim multiline, empty, firmware-error, HTML-like and long responses.
- Unsupported firmware/SDK, protocol errors, disconnected/busy states, cancellation and timeout handling without retry.
- Real SDK command frames (`0x42` plus the entered command), old-session late-reply isolation and fresh-session replies.
- Sender/cache refresh under the manager lock, preservation of the original response when refresh fails, and command/reply exclusion from SDK logs.
- Web endpoint state/status handling and console availability when a full settings read fails.

Chromium checks exercise Enter-to-send, Up/Down history/draft recall, unchanged response text, empty/error replies, HTML-safe rendering, UTF-8 input limits, transcript preservation during refresh, unsupported/connected transitions, one in-flight request, timeout/reconnect behaviour, clearing and page-local history. Layouts are checked at 390 px and 1440 px in light and dark appearances.

Final full-suite result: **1,196 passed** in 50.48 seconds, with one Starlette/httpx deprecation warning and three SDK/asyncio deprecation warnings. All four Chromium layout combinations and the browser interaction checks passed with no JavaScript errors, including recovery of the settings panels after a failed full settings read. Python syntax, local documentation links and `git diff --check` also passed.

The tests use simulated companions and the installed SDK; they do not connect to or modify a physical companion. The configured serial device was absent from the development environment. Physical acceptance still requires comparing a read command and a reversible setting change with the MeshCore app on a supported companion, checking its actual firmware replies and reconnect behaviour. No deployment or application restart was performed during implementation.
