# ⚠️ WARNING: UNOFFICIAL PERSONAL PROJECT — DO NOT RELY ON IT

> **This is an unofficial personal project. It is not suitable for serious, emergency, or safety-critical use. Do not trust any of its inputs or outputs.** Incoming data, settings, displayed warnings, and transmitted messages may be wrong, incomplete, delayed, or missing. Verify information independently using official Bureau of Meteorology and local emergency sources. Never make a safety decision based on NoticeEcho.

---

# Meshcore NoticeEcho

A self-hosted web app that collects NSW BOM, RFS and Live Traffic notices and broadcasts selected notices over MeshCore.

## Install

Clone the repository and enter its directory:

```bash
git clone https://github.com/MJCopper/MeshCore-NoticeEcho.git
cd MeshCore-NoticeEcho
```

### Docker

Requires Docker with Docker Compose. USB serial access uses the Linux host's `/dev` devices.

```bash
docker compose up -d --build
```

Open **http://<host>:8110** in your browser.

### Native Linux

For Raspberry Pi, Debian or Ubuntu, run from the cloned directory:

```bash
sudo ./packaging/install-linux.sh
```

The installer creates the environment and a service that starts on boot. Open **http://<host>:8110**.

## First-time setup

1. Leave **Settings → General → Dry Run** enabled while setting up.
2. In **Settings → MeshCore connection**, choose USB serial or TCP and select the Live and Test channels. Prefer a `/dev/serial/by-id/` path for USB.
3. In **Settings → Geographic Coverage**, choose All NSW, councils or additional location terms. Use the same terms list for towns, roads and BOM forecast districts. Preview and save; upgrades may require migration review.
4. Enable the services you want under **BOM**, **NSW RFS** and **Live Traffic NSW** settings, then choose their notice types or alert levels.
5. Check the source pages and **History** to confirm collection and filtering. Use **Troubleshoot** to send a test to the configured Test channel.
6. When ready for automatic broadcasts, switch Dry Run off.

New installations start with monitoring disabled. Traffic records its first live poll as a baseline rather than broadcasting every existing notice. Local transmission confirmation does not establish reception by another node.

## Update and backup

For Docker, update and rebuild from the repository directory:

```bash
git pull
docker compose up -d --build
```

Settings and history persist in `data/wx-echo.db`. Keep the `data/` directory when moving or reinstalling. To back up the Docker database:

```bash
docker compose exec -T wx-echo python -m app.maintenance backup --database /data/wx-echo.db
```

Daily maintenance and verified backups are configurable on Troubleshoot. See [database maintenance](docs/database-maintenance.md) for retention, restore and offline compaction.

For native Linux, rerun the installer to update. Its database is in the installation's `data/` directory; the active path is shown on Troubleshoot.

## Troubleshooting

Open **http://<host>:8110/troubleshoot** for feed health, radio status, logs, and previewed reprocessing or resend actions. See [troubleshooting and recovery](docs/troubleshooting.md), including NFS/test-volume setup.

## More information

- [Services and source attribution](docs/services.md)
- [Settings, geographic coverage and delivery](docs/operation.md)
- [Upgrade compatibility](docs/compatibility.md)
- [Development](docs/development.md)

## License

MIT. See [LICENSE](LICENSE).
