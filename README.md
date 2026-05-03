# nest-cli

Deterministic, scriptable command-line tool for two distinct Google device families:

- **Google Nest cameras and doorbells** via the Smart Device Management (SDM) API.
- **Google Nest Wi-Fi mesh routers** via the reverse-engineered Foyer service (**experimental**, not in v0.1.0).

Sibling of [`kasa-cli`](https://github.com/agileguy/kasa-cli), [`tapo-cli`](https://github.com/agileguy/tapo-cli), and [`hue-cli`](https://github.com/agileguy/hue-cli): single binary, one verb per invocation, JSON/JSONL on stdout, deterministic exit codes, no GUI, no daemon.

## Status

**v0.1.0 — cam-only auth + list + info.** First functional release. Ships:

- OAuth setup, refresh, revoke, and status (`nest-cli auth ...`)
- Live device discovery via SDM (`nest-cli discover`, `nest-cli list`)
- Per-camera info + capability inspection (`nest-cli cam info <target>`, `nest-cli cam capabilities <target>`)
- Local TOML config with `[aliases]` and `[groups]` sections (`nest-cli config show`, `nest-cli config validate`)
- Output modes: `--json`, `--jsonl`, `--quiet`, plus a human-readable text default
- Deterministic exit codes per [SRD §11.1](docs/SRD-nest-cli.md)

Streams, snapshots, doorbell chime, events, and the experimental Wi-Fi side land in subsequent releases. See [`docs/SRD-nest-cli.md`](docs/SRD-nest-cli.md) §16 for the phase plan.

## Operator onboarding

Before any cam verb works, you need a Google Cloud project, the SDM API enabled, a one-time **\$5 USD** Device Access registration, an OAuth Desktop client, and consent against your own Google account. The full step-by-step is in [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — start there.

## Install

```bash
uv tool install git+https://github.com/agileguy/nest-cli.git
```

(Once published to PyPI, simply `uv tool install nest-cli`.)

Optional Wi-Fi extras (experimental, deferred to a later release; the deps install but no `wifi` verbs are wired up in v0.1.0):

```bash
uv tool install 'git+https://github.com/agileguy/nest-cli.git#egg=nest-cli[wifi]'
```

## Quick start

```bash
# One-time OAuth setup (cam side). See docs/ONBOARDING.md for the
# Google Cloud project + Device Access prerequisites.
nest-cli auth setup

# Confirm credentials are healthy.
nest-cli auth status --json

# List every device the credentials grant access to.
nest-cli discover --json

# Detailed info on a single camera.
nest-cli cam info enterprises/PROJECT_ID/devices/DEVICE_ID --json

# Save aliases to ~/.config/nest-cli/config.toml then refer to them by name:
#   [aliases]
#   front-door = "enterprises/PROJECT_ID/devices/DEVICE_ID"
nest-cli cam info front-door --json
```

## Documentation

- [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — **operator runbook**: Google Cloud + Device Access setup, smoke-test flow, where credentials live.
- [`docs/SRD-nest-cli.md`](docs/SRD-nest-cli.md) — full software requirements document, FRs, phase plan, threat model.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — per-camera-generation matrix + Phase 1 implementation map.
- [`docs/SECURITY.md`](docs/SECURITY.md) — security policy and vulnerability reporting.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.

## Exit codes

| Code | Meaning              | Example                                                |
|------|----------------------|--------------------------------------------------------|
| 0    | Success              | Command completed                                      |
| 1    | Device error         | SDM device returned 4xx (not auth) or device offline   |
| 2    | Auth/credential      | Refresh token expired, credentials missing, chmod loose|
| 3    | Network              | DNS, TLS, 5xx from Google                              |
| 4    | Not found            | Unknown alias, removed device                          |
| 5    | Unsupported feature  | Verb requires a trait the camera doesn't have          |
| 6    | Config error         | Invalid TOML, unknown section, schema violation        |
| 7    | Partial failure      | Batch operation: some targets succeeded, some failed   |
| 64   | Usage error          | Invalid flags, mutually-exclusive output modes         |
| 130  | SIGINT               | Ctrl-C during a long-running verb                      |
| 143  | SIGTERM              | Process terminated                                     |

Errors emit a structured JSON envelope on stderr (`{"error": "<enum>", "exit_code": <int>, "message": "...", "hint": "...", "details": {...}}`) per SRD §11.3. The `error` field is a closed enum string (`auth_failed`, `device_error`, etc.); `exit_code` is the integer mirror of the table above.

## License

[MIT](LICENSE).
