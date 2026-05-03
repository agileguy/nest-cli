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

Errors at any layer raise `StructuredError(code, message, hint, details)` which `cli/_shared.py:exit_on_structured_error` formats to stderr per SRD §11.3 and exits with the mapped code.

### Output and error contract (SRD §5.8 / §11.3)

Every verb that produces structured output uses the `@add_output_options` decorator and the `emit()` function from `nest_cli/output.py`. This guarantees:

- `--json` emits a single JSON object (or a list, where the verb's contract specifies one — e.g. `auth status` per FR-CRED-10).
- `--jsonl` emits newline-delimited JSON, one record per line.
- `--quiet` suppresses stdout but preserves the exit code.
- `--output {text,json,jsonl,quiet}` is the explicit form; the three flag-style options are sugar.
- All four are mutually exclusive; combining them exits 64.
- Datetime fields serialize as RFC 3339 UTC with the literal `Z` suffix (FR-22), via Pydantic `field_serializer`.

Error envelopes are uniform across all verbs:

```json
{"error": "auth_failed", "exit_code": 2, "message": "credentials not found", "hint": "run `nest-cli auth setup`"}
```

The `error` field is the closed enum string from SRD §11.3 (one of `device_error`, `auth_failed`, `network_error`, `not_found`, `unsupported_feature`, `config_error`, `partial_failure`, `usage_error`, `interrupted`); `exit_code` is the integer mirror of the §11.1 table. Tooling MAY pattern-match on either field; both are guaranteed-consistent. Optional `details` field for additional context (status code, target id, etc.).

**`family` discriminator policy is per-family in v0.3.0** (Phase 3A):

- **Wifi-side errors carry `family: "wifi"`** on the envelope per SRD §11.3. The post-audit recommendation was that the wifi side ships SRD-aligned, since the family is the operator's only filter for "is this a Foyer rotation or an SDM hiccup?" Operators piping JSONL through `jq 'select(.family == "wifi")'` can filter cleanly.
- **Cam-side errors omit `family`** for v0.1.0 / v0.2.x back-compat. Operator scripts pinned to the v0.1.0 envelope shape would fail equality assertions if we added `family` retroactively. A follow-up retrofit (cam-side `family: "cam"`) is tracked here as TODO.
- Implementation: `StructuredError` (in `nest_cli/errors.py`) gained an optional `family: Literal["cam", "wifi", "shared"] | None` field. `emit_structured_error_to_stderr` serializes the field only when set. Wifi verbs construct errors with `family="wifi"`; cam verbs leave it unset.

**FR-WIFI-0 vs §11.2 — exit code for missing `--experimental-wifi`:** SRD §11.2 names exit 5 (unsupported feature) for the case where an operator runs a wifi verb without `--experimental-wifi`. SRD FR-WIFI-0 names exit 64 (usage error) for the same case. The implementation follows FR-WIFI-0 (exit 64) — the verb exists, the operator opted for the verb, and a usage-error reads more honestly than "this feature is unsupported." A future SRD revision should reconcile §11.2 to match FR-WIFI-0 explicitly.

### Threat model (excerpt)

The full threat model lives at SRD §4.7. v0.1.0's relevant defenses:

- Credentials file is `chmod 0o600`, written atomically (tempfile + fsync + rename), under a `flock` sidecar with `O_NOFOLLOW` — symlink substitution at `<creds>.lock` is rejected.
- Token cache directory is `chmod 0o700`.
- Pydantic `extra="forbid"` on `CamCredentials` rejects unknown JSON keys (defends against tampered-credential-file attacks that rely on schema drift).
- OAuth scope is minimal: `sdm.service` only, never broader.
- Tokens are never logged or interpolated into stderr error messages. Bearer headers are constructed at request time and not persisted in any structured-error `details` field.
- The CI workflow has a guard step (first step in the workflow) that fails the build if any of `NEST_CLI_TEST_OAUTH_CREDENTIALS`, `GOOGLE_APPLICATION_CREDENTIALS`, or `NEST_CLI_TEST_FOYER_TOKEN` are present in the runner environment. Real-credential integration tests are operator-side only.

### What v0.3.0 (Phase 3 Part A) ships and what it does NOT

**Now shipping (Phase 3 Part A):**

- `auth wifi-setup --experimental-wifi` and `auth wifi-revoke --experimental-wifi` (FR-CRED-7..9).
- `auth status` extended to emit a 2-element JSON array (cam + wifi) per FR-CRED-10.
- `wifi list groups`, `wifi list points <group>`, `wifi list clients <group>` (FR-WIFI-1..3) — all `--experimental-wifi` gated.
- `nest_cli/wifi/` package: `WifiGroup` / `WifiPoint` / `WifiClient` pydantic models; `FoyerClient` lazy-loading sync wrapper around `googlewifi.GoogleWifi`.
- Optional `[wifi]` install extra wired through `pyproject.toml` (already present from Phase 2; FoyerClient lazy-imports it).
- `family="wifi"` on the §11.3 error envelope for wifi-side errors.

**Still deferred:**

- **Phase 3 Part B (Engineer B):** `wifi pause`, `wifi unpause`, `wifi prioritize`, `wifi group-assign` (FR-WIFI-4..7).
- **Phase 3.1:** `wifi speedtest run/history`, `wifi reboot point/group`, `wifi network`, `wifi guest enable/disable`, `wifi point-health` (FR-WIFI-8..15).
- **Phase 4:** `groups list`, `batch`, `@group` target syntax, parallel target execution.
- **Phase 5+:** Pub/Sub auto-provisioning, ffmpeg snapshot fallback, multi-account, OS keyring, wifi guest password setting.
- **Cam-side retrofit:** add `family: "cam"` to cam-side error envelopes once a major-version bump can absorb the back-compat break.

The `cam capabilities` verb advertises a `supported_verbs` list — today it includes the Phase 1 + Phase 2 verbs (`info`, `capabilities`, `snapshot`, `chime`, `battery`, `signal`, `stream`, `stream-extend`, `stream-stop`, `events`). Phase 3 Part B's wifi action verbs will need their own per-family-traits derivation since FR-WIFI-4..7 act on stations, not points.

---

## Out of scope (still)

This document does not cover:

- The wifi-side per-mesh-firmware matrix (deferred — Phase 3 Part A captures only the v1 mesh-AC family that the operator's smoke harness exercised).
- The Pub/Sub events surface beyond Phase 2.1 — Pub/Sub topic provisioning automation is Phase 5+ (SRD §16.7).
- Wifi action verbs (pause / unpause / prioritize / group-assign / speedtest / reboot / network / guest / point-health) — Phase 3 Part B and Phase 3.1.

Phase 3 Part A's deliverable is the wifi-side foundation (types, FoyerClient, credentials, list verbs, experimental gate, family error envelope) that the Phase 3 Part B action verbs build on without further refactoring.
