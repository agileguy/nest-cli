# Architecture

This document captures two layers:

1. **Per-camera-generation capability matrix** (Phase 0 planning artifact, sourced from SRD §3.1.2).
2. **Phase 1 implementation map** (added with v0.1.0): how the verbs that ship today compose, where state lives, what each module owns.

The matrix is **not** a runtime authority — see "How the CLI actually decides" below.

---

## Per-generation capability matrix

The columns are SDM traits the CLI cares about. A `+` means "generation typically exposes this trait"; a `-` means "generation typically does not". This is shaped by Google's hardware revisions, **not** by the SDM contract itself — the SDM contract is "look at the device's `traits` array and gate on what's actually there." Use this table to set expectations at design time, not as runtime input.

| Generation                            | Stream protocol        | `CameraImage` | `CameraEventImage` | `DoorbellChime` |
|---------------------------------------|------------------------|---------------|--------------------|-----------------|
| 1st-gen Nest Cam (Indoor / Outdoor / IQ) | `RTSP` (`GenerateRtspStream`)   | +             | +                  | -               |
| Nest Hello (1st-gen wired doorbell)   | `RTSP` (`GenerateRtspStream`)   | +             | +                  | +               |
| 2nd-gen Battery Cam                   | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | -               |
| 2nd-gen Battery Doorbell              | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | +               |
| 2nd-gen Floodlight Cam                | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | -               |
| post-2021 generic                     | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | (model dependent) |

Source: [SRD §3.1.2](SRD-nest-cli.md#312-stream-protocol-per-generation-the-headline-asymmetry). The headline asymmetry is the `RTSP` vs `WEB_RTC` split: 1st-gen hardware exposes a stable RTSP URL the operator can pipe straight into `ffmpeg`/`mpv`, whereas every 2nd-gen and later camera ships `WEB_RTC` only — a session-bound SDP-exchange flow with a few-minute `expiresAt` and a downstream WebRTC peer required to actually consume the stream. The same split drives the snapshot fallback chain (1st-gen has `CameraImage`; 2nd-gen and later only expose `CameraEventImage` keyed off the most recent qualifying `eventId`).

## How the CLI actually decides

The CLI **does not gate on model name**. At runtime, every per-device verb calls `devices.get` (cached), inspects the returned `traits` array, and routes based on which traits are present:

- `cam stream` → checks `CameraLiveStream.supportedProtocols`. If `RTSP` is present, calls `GenerateRtspStream` and emits the URL. If only `WEB_RTC` is present, it requires `--offer-sdp` and calls `GenerateWebRtcStream`. *(Phase 2.)*
- `cam snapshot` → checks for `CameraImage` first; falls back to `CameraEventImage` keyed off the most recent qualifying event. *(Phase 2.)*
- `cam chime` → requires `DoorbellChime` to be present in the device's traits; otherwise exits 5 (unsupported feature). *(Phase 2.)*
- `cam capabilities` → emits the device's full `traits` array plus a derived `supported_verbs` list. *(Shipped in v0.1.0.)*

This is **explicitly per-SRD FR**: `nest-cli` does not lookup a hardcoded model-to-capability table at runtime. The matrix above is for human planning only. If Google ships a future generation that flips a column, the CLI's runtime behavior tracks Google's `traits` payload directly with no code change required — though this document will need a row added.

---

## Phase 1 implementation map (v0.1.0)

What's actually wired up today, where state flows, and how to extend.

### Module layout

```
nest_cli/
├── __init__.py        # __version__
├── __main__.py        # `python -m nest_cli` and `nest-cli` console-script entry point
├── errors.py          # StructuredError + EXIT_* constants (SRD §11.1)
├── output.py          # text/json/jsonl/quiet formatters; add_output_options decorator
├── config.py          # tomllib-based parser for ~/.config/nest-cli/config.toml
├── auth/
│   ├── __init__.py
│   ├── types.py       # CamCredentials Pydantic v2 model (extra="forbid", FR-CRED-3)
│   ├── credentials.py # atomic write, chmod-0600, flock with O_NOFOLLOW, refresh-on-expiry
│   └── oauth.py       # google_auth_oauthlib InstalledAppFlow wrapper
├── sdm/
│   ├── __init__.py
│   ├── types.py       # Camera + CameraTrait records (SRD §10.1)
│   └── client.py      # SdmClient — REST wrapper with 401-retry-after-refresh
└── cli/
    ├── __init__.py    # Click root group; wires every subcommand
    ├── _shared.py     # cross-cutting: load_credentials_or_exit, family_for_target, exit_on_structured_error
    ├── auth_cmd.py    # `auth setup/refresh/revoke/status` (FR-CRED-1..6, FR-CRED-10)
    ├── cam_cmd.py     # `cam list/info/capabilities` (FR-CAM-1, FR-CAM-2, FR-CAM-28)
    ├── list_cmd.py    # `list`, `discover` (FR-1..2a)
    └── config_cmd.py  # `config show/validate` (FR-16c)
```

### State and where it lives

| State                          | Path / module                                         | Owner / writer                    |
|--------------------------------|-------------------------------------------------------|-----------------------------------|
| OAuth client credentials       | operator-supplied path (e.g. `~/.config/nest-cli/oauth-client.json`) | operator (downloads from GCP) |
| Persisted CamCredentials       | `~/.config/nest-cli/credentials-cam.json` chmod 0600  | `auth.credentials.save_credentials` (atomic write + flock) |
| Token-cache directory          | `~/.config/nest-cli/.tokens/` chmod 0700              | `auth.credentials.default_token_cache_dir` |
| Local TOML config              | `~/.config/nest-cli/config.toml`                      | operator (hand-edited)            |
| Captured smoke fixtures        | `<repo>/tests/fixtures/{sdm,foyer}/captured/`         | smoke scripts; gitignored         |

### Request flow for a typical cam verb

```
nest-cli cam info <alias>
   │
   ▼
cli/__init__.py                   ← Click root group dispatch
   │
   ▼
cli/cam_cmd.py:cam_info           ← argument parsing, output-mode resolution
   │
   ▼
cli/_shared.py:load_credentials_or_exit
   │                              ← reads creds from disk, enforces chmod 0o600
   ▼
auth/credentials.py:refresh_access_token_if_needed
   │                              ← refreshes if expires_at within 60s; persists atomically
   ▼
sdm/client.py:SdmClient.get_device
   │                              ← REST GET; on 401 forces refresh + retries once
   ▼
sdm/types.py:Camera               ← Pydantic record
   │
   ▼
output.py:emit                    ← per-mode formatter, RFC 3339 Z timestamps
```

Errors at any layer raise `StructuredError(code, message, hint, details)` which `cli/_shared.py:exit_on_structured_error` formats to stderr per SRD §11.2 and exits with the mapped code.

### Output and error contract (SRD §5.8 / §11.2)

Every verb that produces structured output uses the `@add_output_options` decorator and the `emit()` function from `nest_cli/output.py`. This guarantees:

- `--json` emits a single JSON object (or a list, where the verb's contract specifies one — e.g. `auth status` per FR-CRED-10).
- `--jsonl` emits newline-delimited JSON, one record per line.
- `--quiet` suppresses stdout but preserves the exit code.
- `--output {text,json,jsonl,quiet}` is the explicit form; the three flag-style options are sugar.
- All four are mutually exclusive; combining them exits 64.
- Datetime fields serialize as RFC 3339 UTC with the literal `Z` suffix (FR-22), via Pydantic `field_serializer`.

Error envelopes are uniform across all verbs:

```json
{"error": {"code": 2, "message": "credentials not found", "hint": "run `nest-cli auth setup`"}}
```

Optional `details` field for context. **No `family` discriminator in the error envelope** — the family of the originating verb is implicit in the verb path; only payloads (e.g. `auth status`) carry it.

### Threat model (excerpt)

The full threat model lives at SRD §4.7. v0.1.0's relevant defenses:

- Credentials file is `chmod 0o600`, written atomically (tempfile + fsync + rename), under a `flock` sidecar with `O_NOFOLLOW` — symlink substitution at `<creds>.lock` is rejected.
- Token cache directory is `chmod 0o700`.
- Pydantic `extra="forbid"` on `CamCredentials` rejects unknown JSON keys (defends against tampered-credential-file attacks that rely on schema drift).
- OAuth scope is minimal: `sdm.service` only, never broader.
- Tokens are never logged or interpolated into stderr error messages. Bearer headers are constructed at request time and not persisted in any structured-error `details` field.
- The CI workflow has a guard step (first step in the workflow) that fails the build if any of `NEST_CLI_TEST_OAUTH_CREDENTIALS`, `GOOGLE_APPLICATION_CREDENTIALS`, or `NEST_CLI_TEST_FOYER_TOKEN` are present in the runner environment. Real-credential integration tests are operator-side only.

### What v0.1.0 deliberately does NOT include

Per SRD §16:

- **Phase 2:** `cam snapshot/stream/stream-extend/stream-stop/chime/battery/signal/events`
- **Phase 2.1:** `cam events --follow`
- **Phase 3:** all `wifi` subcommands (`--experimental-wifi` gated)
- **Phase 3.1:** `wifi speedtest/reboot/network/guest/point-health`
- **Phase 4:** `groups list`, `batch`, `@group` target syntax, parallel target execution
- **Phase 5+:** Pub/Sub auto-provisioning, ffmpeg snapshot fallback, multi-account, OS keyring, wifi guest password setting

The `cam capabilities` verb already advertises a `supported_verbs` list — today it includes only `info` and `capabilities`. Phase 2's verbs slot into the `_TRAIT_TO_VERBS` table in `cli/cam_cmd.py` without further refactoring.

---

## Out of scope (still)

This document does not cover:

- The wifi-side per-mesh-firmware matrix (deferred until the wifi slice lands in Phase 3 — SRD §16.4).
- The Pub/Sub events surface (separate routing concern; deferred until Phase 2 — SRD §16.2 / §3.1.3).

All of those are intentional Phase 1 omissions. v0.1.0's deliverable is the cam-side foundation that everything later builds on.
