# nest-cli

Deterministic, scriptable command-line tool for two distinct Google device families:

- **Google Nest cameras and doorbells** via the Smart Device Management (SDM) API.
- **Google Nest Wi-Fi mesh routers** via the reverse-engineered Foyer service (**experimental**).

Sibling of [`kasa-cli`](https://github.com/agileguy/kasa-cli), [`tapo-cli`](https://github.com/agileguy/tapo-cli), and [`hue-cli`](https://github.com/agileguy/hue-cli): single binary, one verb per invocation, JSON/JSONL on stdout, deterministic exit codes, no GUI, no daemon.

## Status

**v0.0.1 — Phase 0 skeleton.** No CLI verbs yet — first shipped functional slice (cam-only auth + list + info) targets v0.1.0 in Phase 1. See [`docs/SRD-nest-cli.md`](docs/SRD-nest-cli.md) §16 for the phase plan.

## Install

```bash
uv tool install git+https://github.com/agileguy/nest-cli.git
```

(Once published to PyPI, simply `uv tool install nest-cli`.)

Optional Wi-Fi extras (experimental, gated behind `--experimental-wifi` per invocation):

```bash
uv tool install 'git+https://github.com/agileguy/nest-cli.git#egg=nest-cli[wifi]'
```

## Quick start (Phase 1, v0.1.0+)

> Coming in v0.1.0 — these verbs do NOT work in v0.0.1.

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
