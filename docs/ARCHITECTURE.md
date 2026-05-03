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

### Wifi-side implementation (Phase B, 2026-05-03)

The wifi side originally wrapped `googlewifi.GoogleWifi` (which itself depends on `glocaltokens`). That path was empirically broken on AAS master tokens: `googlewifi` calls `https://www.googleapis.com/oauth2/v4/token` with `grant_type=refresh_token`, which expects a standard OAuth2 refresh token (`1//09...`) — not the AAS master token (`aas_et/...`) the operator extracts from a paired Android device. Phase B replaces the broken path with a direct call to `googlehomefoyer-pa.googleapis.com:443` over gRPC, with the access token minted via `gpsoauth.perform_oauth(email, master_token, android_id, ...)` against the Google Home Android app's signing constants.

**Implemented (read verbs from `GetHomeGraph`):**

- `auth wifi-setup --experimental-wifi --android-id <hex>` (FR-CRED-7) — accepts the 16-char hex `android_id` from the paired Android device's `gservices.db`. Persisted into v2 `credentials-wifi.json`.
- `auth wifi-revoke --experimental-wifi` (FR-CRED-9) — atomic stub-replace.
- `auth status` (FR-CRED-10) — emits a 2-element JSON array (cam + wifi).
- `wifi list groups --experimental-wifi` (FR-WIFI-1).
- `wifi list points <group> --experimental-wifi` (FR-WIFI-2).
- `wifi point-health <point> --experimental-wifi` (FR-WIFI-15).

**Action verbs ship with exit-5 (`unsupported_feature`, `family="wifi"`) until Phase C maps the specific Foyer RPCs:**

- `wifi list clients <group>` (FR-WIFI-3).
- `wifi pause / unpause / prioritize / group-assign` (FR-WIFI-4..7).
- `wifi speedtest run / history` (FR-WIFI-8..9).
- `wifi reboot point / group` (FR-WIFI-10..11).
- `wifi network` (FR-WIFI-13) — `GetHomeGraph` carries no SSID/IPv4/IPv6/DNS data, so returning placeholder `"<unknown>"` records would be misleading.
- `wifi guest enable / disable` (FR-WIFI-14).

The CLI surface for these verbs is fully wired so operator scripts can be authored today and will start working when Phase C lands without any operator-visible interface change. Each verb returns a structured error with hint pointing at the Phase-C deferral.

**Schema migration:** `WifiCredentials.version` bumped 1 → 2. v1 files (no `android_id`) fail load-time validation with `EXIT_CONFIG_ERROR` and a hint pointing at `auth wifi-setup --overwrite --experimental-wifi`.

**Module map:**

- `nest_cli/wifi/client.py:FoyerClient(creds: WifiCredentials)` — direct gpsoauth + gRPC. Token cache (60s skew before expiry); `_fetch_systems()` calls `StructuresServiceStub.GetHomeGraph()` and projects the protobuf onto the legacy googlewifi-shaped dict that the existing `WifiGroup` / `WifiPoint` model classmethods consume.
- `nest_cli/wifi/types.py` — pydantic models unchanged from v0.3.x (Phase B reuses the existing `from_googlewifi_response` classmethods on a projected dict shape).
- `nest_cli/auth/wifi_types.py` — `WifiCredentials` v2 schema with `android_id: str` (16-char hex regex).
- `nest_cli/cli/auth_cmd.py` — `--android-id` flag + `GOOGLE_ANDROID_ID` env var support.

**Still deferred:**

- **Phase C:** map Foyer RPCs for the action verbs above (`SetStation`, `RunSpeedTest`, etc.) and replace the exit-5 stubs.
- **Phase 5+:** Pub/Sub auto-provisioning, ffmpeg snapshot fallback, multi-account, OS keyring, wifi guest password setting.
- **Cam-side retrofit:** add `family: "cam"` to cam-side error envelopes once a major-version bump can absorb the back-compat break.

The `cam capabilities` verb advertises a `supported_verbs` list including the Phase 1 + Phase 2 cam verbs. The wifi side's "what's implemented vs deferred" is not advertised through `capabilities` because every wifi verb registers in Click; verbs that exit-5 do so via the FoyerClient layer, not via missing Click registration.

---

## Out of scope (still)

This document does not cover:

- The wifi-side per-mesh-firmware matrix (deferred — Phase B was validated against a Nest Wifi Pro on the operator's smoke harness).
- The Pub/Sub events surface beyond Phase 2.1 — Pub/Sub topic provisioning automation is Phase 5+ (SRD §16.7).
- Wifi action verbs (pause / unpause / prioritize / group-assign / speedtest / reboot / network / guest / list-clients / point-health-mutations) — Phase C will map each Foyer RPC and replace the exit-5 stubs.

Phase B's deliverable is a working wifi-side read inventory (groups, points, point-health) plus a deferred-feature posture (every action verb has a wired CLI path that returns a clean exit-5 with hint). Phase C builds on top by replacing each stub method's body with the real Foyer RPC call — no new constructor wiring or test scaffolding required.
