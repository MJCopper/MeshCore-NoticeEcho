# Troubleshooting and recovery

Open `/troubleshoot` to inspect BOM, NSW RFS, Live Traffic NSW and MeshCore together.

The dashboard distinguishes current saved notices from lifetime History totals, and shows the last recorded selection/delivery counts. It reports service settings, poll interval, attempt/success times, scheduled poll, last poll duration, exclusions, enrichment availability and geographic uncertainty. Each Traffic feed has independent collection status and publication/observation information. Poll duration includes parsing and processing; recent HTTP logs show response status. Fresh fetching does not guarantee fresh provider data.

## Operator actions

**Poll now** uses the ordinary collection and delivery pipeline, with saved settings and duplicate suppression. It does not enable a disabled service. It runs as a tracked background job.

**Re-process current notices** applies the current matching/filtering policy to stored provider inputs, preserving ordinary duplicate suppression and first-live baseline rules. It does not refresh provider observation age, advance disappearance counters or complete a feed baseline. Legacy rows without provider inputs require a successful poll first.

**Resend eligible current notices** intentionally bypasses completed-delivery suppression and the Traffic first-live baseline for the selected current notices. It preserves source enablement, selection, lifecycle/expiry, fire suppression, dry-run and pre-send guards. Notices already queued are not admitted again. Resending a previously sent notice starts a new complete delivery; ordinary recovery preserves both confirmed and potentially delivered parts.

Choose one service or all, then preview. The preview lists eligible notices, estimated message parts, exclusions, source content and dry-run/live mode. Re-processing can leave already delivered notices unchanged. Preview runs on an isolated database and cannot send to a radio or modify production dedupe/settings/history. Tokens expire after five minutes, are single-use, and become invalid if settings, saved notices or delivery state changes.

Start the reviewed action to create a serialized background job. Progress separates queued, deferred, locally confirmed, failed, dry-run and unchanged notices. Capacity-deferred notices are retried without resending notices already confirmed by that job. Failed or uncertain radio sends are not automatically repeated by the replay job. Scheduled source polling continues while a replay waits for the radio. Cancel stops remaining replay work and invalidates queued replay parts at their next pre-send check; an in-flight radio operation cannot be recalled.

Job results are stored in SQLite (the latest 20 jobs). A restart marks unfinished work interrupted; inspect History before requesting another resend. Existing queued-part recovery still records interrupted sends.

## Delivery and application diagnostics

The page reports radio connection/target/channel and connection error, effective byte budget, pending notice/part counts, stored bytes, capacity, oldest pending age, active notice and last local confirmation. Application information includes version, uptime, start count, previous start, database path/size, a reversible write probe, SQLite version, migration records and boundary snapshot provenance.

Local radio confirmation does not establish remote reception. Simplified geographic boundaries and a road point do not establish the whole affected footprint. Missing provider timezone remains explicitly uncertain.

## Captured logs and export

Application logging plus Python stdout/stderr are captured to a live window. Pause/resume, service and severity filters, search, and filtered JSONL download are available. The memory/window buffer holds 1,000 records and each record is capped at 8 KiB. Redacted disk logs rotate at 1 MiB with two backups beside the database; recent captured output is reloaded on restart.

Process output before capture starts, subprocess output and native writes bypassing Python's streams remain available through `docker compose logs`. The application does not require a Docker socket mount to read its logs.

The diagnostic bundle downloads JSON containing redacted settings, service/radio/queue/database status, recent errors, captured logs and latest job outcome. Known credential fields and common token/password/authorization patterns are redacted.

## Validation

290 tests pass. Automated regression coverage uses temporary databases and mock radios. It covers preview isolation, all sources, selection and expiry, disabled services, dry-run, stale/used preview tokens, duplicate queue prevention, deferral retries, reused history rows, cancellation, restart recovery, independent polling, log filtering/redaction/rotation and page rendering. Test-environment deployment retains its external database volume and saved service settings.

The test image was rebuilt and deployed on 3 October 2026. Health, Troubleshoot, source pages, status/log APIs and both downloads returned HTTP 200. An all-service dry-run resend preview reported two current BOM notices and ten message parts; execution completed with two dry-run outcomes. Disabled RFS/Traffic and saved dry-run settings remained intact. Docker reported running/healthy. The pre-deployment backup is `/data/wx-echo-before-troubleshooting-20261003.db` in the external test volume.

## Docker test database on NFS

If the test checkout is on NFS and startup fails with `sqlite3.OperationalError: unable to open database file`, use local Docker storage for the database:

```bash
docker volume create wxecho-test-data
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d
```

This stores test settings and history in the external Docker volume `wxecho-test-data`. Creating the volume again is safe and retains its contents. Use both Compose files for subsequent commands. It does not copy an existing `data/wx-echo.db`; back up and migrate that database before switching if it contains settings or history you need. Compose preserves this external volume, including with `down -v`; removing it explicitly with `docker volume rm` deletes the test database.

## Transmission confirmation

“Local TX counter unavailable” means the statistics check failed; it does not establish that the message was not sent. The queue continues the remaining parts. A matching heard repeat confirms propagation even without readable counters. Inspect **Delivery and radios** for counter support/error, repeat subscription status, tracked packets, policy, pending retries and recent unconfirmed submissions. Expand a Transmit Log or History record for individual part evidence. Repeated capability failures do not create duplicate service error records.

To reduce duplicate reception, leave **Retry unconfirmed transmissions once** off unless the network needs that recovery trade-off. When enabled, a late repeat or recovered local counter cancels the pending retry. No repeat can also mean that a receiver heard the original transmission directly.

### Physical companion checks

Automated tests simulate SDK responses and exercise the SDK packet parser; they cannot measure your radio or local network. On the test environment, verify a controlled multipart notice with retry disabled: both parts should arrive even if statistics are unavailable. Check that heard repeats change the correct part to **Repeat confirmed**. Repeat with the optional retry enabled and verify at most one duplicate of an unconfirmed part, ordered before the next part. Confirm that late repeats suppress retries and that reconnecting does not lose tracking. Choose timeout/delay values using the observed timings in the Transmit Log.

### Companion clock verification on a physical device

Compare `clock` with server time, introduce an offset with `time <epoch>`, and verify `clock sync` reads back the correction. Repeat with automatic synchronization enabled, then reboot and reconnect the companion through USB/TCP. Confirm a periodic correction and check that a multipart notice completes while a clock operation is due. Test failure recovery and verify diagnostics do not claim success without readback or cause duplicate notice transmissions. Automatic synchronization requires a correct server clock; daylight saving is a display concern. Physical-device validation is separate from automated protocol/scheduler tests.
