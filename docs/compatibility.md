# Rename and compatibility

[Back to installation and setup](../README.md).

## NoticeEcho rename and compatibility

The full product name is **Meshcore NoticeEcho**; the short name and Windows executable are **NoticeEcho**. New releases produce `NoticeEcho-windows-<version>.zip`; the canonical PyInstaller specification is `packaging/noticeecho.spec`. The old build specification remains compatible.

Bootstrap configuration now accepts `NOTICE_ECHO_HOST`, `NOTICE_ECHO_PORT` and `NOTICE_ECHO_DB`. These take precedence over the supported `WX_ECHO_*` aliases and older `MESH_WX_*` variables. New native user-data directories use `NoticeEcho` (Windows/macOS) or `notice-echo` (Linux), and new databases use `notice-echo.db`. Existing WXEcho/MeshWX directories and database filenames are reused automatically; no rename or data copy is required.

Compose retains its `wx-echo` service key for existing commands and override files. The container is now `notice-echo`. The test database stays in the existing external `wxecho-test-data` volume, and Compose retains `/data/wx-echo.db` explicitly. Do not create a replacement volume simply to change its name. Native installation uses `notice-echo.service` for fresh installs and retains an existing `wx-echo.service` on upgrade.

The source checkout can be called `Meshcore-NoticeEcho`; repository hosting names and published releases are managed separately from the local rename. Runtime logger names and the existing persisted process-log filename remain compatible with earlier diagnostics.

Installer configuration accepts `NOTICEECHO_REPO`, `NOTICEECHO_DIR` and `NOTICEECHO_NO_SELFUPDATE`, with precedence over the supported `WXECHO_*` and older installer aliases. Fresh one-line installs default to `/opt/NoticeEcho`; an existing `/opt/WXEcho` checkout is reused.

The local Docker image is `noticeecho:local`. Release automation publishes `ghcr.io/<owner>/meshcore-noticeecho` and retains the repository-derived image tag for existing consumers. The hosted repository itself is not renamed by a local deployment.
