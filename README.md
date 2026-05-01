# nest-cli

Deterministic, scriptable command-line tool for two distinct Google device families:

- **Google Nest cameras and doorbells** via the Smart Device Management (SDM) API.
- **Google Nest Wi-Fi mesh routers** via the reverse-engineered Foyer service (**experimental**).

Sibling of [`kasa-cli`](https://github.com/agileguy/kasa-cli), [`tapo-cli`](https://github.com/agileguy/tapo-cli), and [`hue-cli`](https://github.com/agileguy/hue-cli): single binary, one verb per invocation, JSON/JSONL on stdout, deterministic exit codes, no GUI, no daemon.

## Status

**v0.1.0 — Cam-only auth + list + info.** First shipped slice. Streams, snapshots, events, doorbell chime, and the experimental Wi-Fi side land in subsequent releases.

See [`docs/SRD-nest-cli.md`](docs/SRD-nest-cli.md) for the full software requirements document and phase plan.

## Install

```bash
uv tool install nest-cli
```

Optional Wi-Fi extras (experimental, gated behind `--experimental-wifi` per invocation):

```bash
uv tool install 'nest-cli[wifi]'
```

## Quick start

```bash
# One-time OAuth setup (cam side). Requires a Google Cloud project, the Smart
# Device Management API enabled, and a $5 USD Device Access registration.
nest-cli auth setup

# List every device the credentials grant access to.
nest-cli discover --json

# Detailed info on a single camera.
nest-cli cam info front-door --json
```

## Architecture, threat model, prior art

- [`docs/SRD-nest-cli.md`](docs/SRD-nest-cli.md) — full SRD, phase plan, FRs, threat model.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — per-camera-generation capability matrix.

## License

[MIT](LICENSE).
