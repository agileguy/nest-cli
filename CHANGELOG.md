# Changelog

All notable changes to `nest-cli` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- Documented the 2026 OnHub OAuth dead-end discovered during Phase C live-verification on operator hardware (2026-05-03). Every public method to obtain a Google OAuth refresh token (`1//...`) for the Google Wifi web client_id `936475272427.apps.googleusercontent.com` has been closed by Google:
  - `accounts.google.com/o/oauth2/programmatic_auth` returns server-side HTTP 404 (endpoint retired ~2023; `AngeloD2022/onhubauthhelper` Chrome extension depended on it and is broken).
  - Google OAuth Playground rejects with `redirect_uri_mismatch` (client_id does not whitelist Playground's redirect URI).
  - OAuth 2.0 Device Authorization Grant rejects with `Invalid client type` (it is a Web Application client, not registered for device flow).
  - Substituting the Foyer ya29 access token (the one our gpsoauth + gRPC path mints successfully today) as the bearer for `oauthaccountmanager.googleapis.com/v1/issuetoken` returns HTTP 400 / 403 with insufficient scopes; gpsoauth cannot mint a token with the combined `accesspoints clouddevices` scopes via the Chromecast app signature (`RESTRICTED_CLIENT`), and the OnHub app's signing SHA1 is not public.
- Phase B read verbs (`wifi list groups`, `wifi list points`, `wifi point-health`) remain fully operational via the gpsoauth + gRPC path — verified live against an Active T1 / Nest Wifi Pro mesh on 2026-05-03 (returned `[{id:"036f7d70-...", name:"Home", points:6, ...}]`).
- Phase C action verbs (`wifi pause/unpause/prioritize/list-clients/speedtest/reboot`) ship as architecturally-correct code that fails fast at the auth pre-flight check with an updated `_REFRESH_TOKEN_HINT` reflecting the dead-end. The verbs cannot mutate Foyer state today; even attempting `wifi reboot point` or `wifi reboot group` exits 2 before any HTTP request is sent.
- Possible future paths to unblock Phase C: Frida + mitmproxy on a rooted Android device to capture the live Google Home app's Foyer REST auth header; APK decompile of the current Google Home app to discover its OAuth flow; or wait for the community to publish a new bootstrap (no movement since 2023). Track at https://github.com/agileguy/nest-cli/issues for Phase D auth-discovery work.

### Changed

- `_REFRESH_TOKEN_HINT` updated to surface the 2026 auth dead-end rather than pointing operators at the broken `AngeloD2022/onhubauthhelper` and OAuth Playground methods.
- SRD §17 gains a Phase C addendum (parallel to the Phase B note) documenting the auth investigation and architectural decisions in light of the finding.

## [0.5.0] - 2026-05-03

### Phase C — wifi action verbs implemented via Foyer REST (2026-05-03)

The Phase B work shipped read inventory (list-groups, list-points,
point-health) over Foyer's gRPC HomeGraph but left the action verbs as
clean exit-5 stubs. Phase C lands real implementations for 8 of the 10
action verbs by talking to Foyer's REST endpoints at `/v2/groups/...`,
which require an OnHub-scoped access token derived through a two-step
OAuth chain rooted in a standard Google OAuth refresh token.

### Added

- `WifiCredentials` schema bumped 2 → 3 with optional `refresh_token`
  field (Google OAuth refresh token, validates `^1//[\w-]+$`). v2
  files remain loadable; the loader treats missing `refresh_token` as
  a v2 record (no auto-upgrade), so existing operators retain the
  Phase B gRPC read path until they explicitly bootstrap.
- `auth wifi-refresh-bootstrap` CLI verb. Persists a refresh token
  alongside the existing v2 credentials and upgrades the file to
  schema v3 in place. Token sources: `--refresh-token` flag,
  `GOOGLE_REFRESH_TOKEN` env var, or interactive stdin prompt.
  Bad-format tokens exit 6 with `family=wifi`. Missing v2 credentials
  exit 6 with a hint pointing at `auth wifi-setup`.
- `auth status` extended with `schema_version` and
  `refresh_token_present` fields on the wifi record so operators can
  verify a successful bootstrap.
- `FoyerClient._refresh_onhub_access_token()` — two-step OAuth chain
  that mints OnHub-scoped access tokens via `oauth2/v4/token` then
  `oauthaccountmanager/v1/issuetoken`. Cached with the same 60s skew
  the gRPC path uses; `threading.Lock` serializes the refresh
  critical section.
- `FoyerClient._rest()` — REST transport helper backed by the existing
  `requests.Session` dep (no new HTTP libraries added). Status
  mapping: 401/403 → `EXIT_AUTH_ERROR`, 404 → `EXIT_NOT_FOUND`,
  5xx → `EXIT_NETWORK_ERROR`, other → `EXIT_DEVICE_ERROR`.
- `FoyerClient._wait_for_operation()` — polls `/v2/operations/{id}`
  every 5s until `operationState=DONE` or the per-call timeout trips.
  Used by `run_speedtest` to wait on Foyer's async wanSpeedTest op.
- 8 of 10 wifi action verbs land as real REST implementations:
  - `wifi list clients <group>` (FR-WIFI-3)
  - `wifi pause <client>` / `wifi unpause <client>` (FR-WIFI-4..5)
  - `wifi prioritize <client> --duration <m>` (FR-WIFI-6)
  - `wifi speedtest run <group> --timeout <s>` (FR-WIFI-8)
  - `wifi speedtest history <group> --limit <n>` (FR-WIFI-9)
  - `wifi reboot point <ap>` (FR-WIFI-10)
  - `wifi reboot group <group>` (FR-WIFI-11)
- ~30 new tests covering OnHub OAuth chain, REST helper status
  mapping, async-op poller, schema v3 round-trip, bootstrap CLI happy
  + error paths, per-verb REST shape assertions, and the rewired
  fan-out test suite (`wifi pause @group` now succeeds).

### Changed

- `tests/wifi/conftest.py` — `_patch_skip_extras_check` now
  initializes the OnHub token cache attributes added in 0.5.0. Adds
  `fake_rest_client` fixture and `make_v3_creds` factory used by the
  Phase C action verb tests.
- All Phase B per-verb CLI test files (`test_pause.py`,
  `test_unpause.py`, `test_prioritize.py`, `test_list_clients.py`,
  `test_speedtest_run.py`, `test_speedtest_history.py`,
  `test_reboot_point.py`, `test_reboot_group.py`) rewritten — seed v3
  credentials and monkey-patch `FoyerClient._rest` to record calls
  and assert (method, path, json, params).
- `tests/wifi/test_client_phase_3_1.py` rewritten as Phase C unit
  tests; per-verb classes assert the exact REST shape via the new
  `fake_rest_client` recorder fixture.
- `tests/batch/test_wifi_pause_group.py` — `wifi pause @kids-devices`
  fan-out now produces two success envelopes (was two exit-5
  envelopes in Phase B).

### Wifi action verbs deferred to Phase D (exit 5, `family="wifi"`)

Two verbs continue to exit 5 because their Foyer request body schemas
are undocumented and the risk of corrupting station/group config is
too high to ship a guess:

- `wifi group-assign <client> --group <choice>` (FR-WIFI-7) —
  `POST /v2/groups/{gid}/stationSets` body shape unknown.
- `wifi guest enable / disable <group>` (FR-WIFI-14) —
  `PUT /v2/groups/{gid}/guestWirelessConfig` body shape unknown
  (SSID + password preservation rules unclear).

The CLI surface is unchanged; the `_deferred_phase_d` envelope's hint
explicitly references Phase D so operators can distinguish "deferred
indefinitely" from "Phase B leftover".

### Phase B — wifi side rebuilt on direct gpsoauth + gRPC (2026-05-03)

The wifi side originally wrapped `googlewifi.GoogleWifi` (which itself
depends on `glocaltokens`). Both libraries are dropped in Phase B:
`googlewifi`'s OAuth2 refresh-token flow is incompatible with the AAS
master tokens operators extract from paired Android devices, and
`glocaltokens` 0.7.x has a `get_master_token()` early-return bug that
defeats reuse of an already-populated token. Phase B replaces the path
with a direct call to `googlehomefoyer-pa.googleapis.com:443` over
gRPC, with the access token minted via
`gpsoauth.perform_oauth(email, master_token, android_id, ...)`.

### Added

- `WifiCredentials` schema bumped 1 → 2 with required `android_id: str`
  field (16-char hex, regex-validated). The Foyer access-token mint
  needs the same Android device's `android_id` (from
  `/data/data/com.google.android.gsf/databases/gservices.db` on the
  rooted Android device) alongside the master token.
- `auth wifi-setup --android-id <hex>` flag + `GOOGLE_ANDROID_ID` env
  var. Precedence: flag > env > stdin prompt. Non-hex / wrong-length
  values exit 6 (`config_error`, `family=wifi`) with a hint pointing
  at the bootstrap flow.
- `FoyerClient(creds: WifiCredentials)` — direct gRPC client. Lazy-
  imports `gpsoauth` + `grpc` + `ghome_foyer_api`; cam-only installs
  see exit 5 with install hint. Access-token cache with 60s skew
  before expiry.
- 22 new tests under `tests/wifi/test_client.py` covering token-mint
  happy path, token caching, expiry-and-refresh, skew window,
  missing-`Auth`-key auth-failure (exit 2), gpsoauth network errors
  (exit 3), and exit-5 posture parametrized over all 10 deferred
  action verbs.

### Changed

- `pyproject.toml` `[wifi]` extras: dropped `googlewifi` + `glocaltokens`,
  added `gpsoauth>=1.0,<3`, `grpcio>=1.60,<2`,
  `googleapis-common-protos>=1.60,<2`, `ghome-foyer-api>=1.0,<2`.
- `FoyerClient` constructor signature: `FoyerClient(creds: WifiCredentials)`
  instead of `FoyerClient(master_token=...)`. All 16 construction
  sites in `nest_cli/cli/wifi_cmd.py` updated; `_load_wifi_creds_or_exit`
  now returns the full `WifiCredentials` record.
- `nest_cli/wifi/client.py` `_fetch_systems()` now projects the
  `GetHomeGraphResponse` protobuf onto the legacy googlewifi-shaped
  dict that the existing `WifiGroup` / `WifiPoint` model classmethods
  consume. Models, the structured-error envelope, and `family="wifi"`
  discriminator are preserved unchanged.
- `tests/wifi/conftest.py` — fakes patch `FoyerClient._fetch_systems`
  directly (the gRPC seam) instead of `googlewifi.GoogleWifi`. Old
  fixture names (`fake_googlewifi`, `empty_googlewifi`,
  `missing_googlewifi`) aliased to the new fixtures so action-verb
  test files don't need per-signature rewrites.

### Wifi action verbs deferred to Phase C (exit 5, `family="wifi"`)

The `GetHomeGraph` projection covers read inventory (groups, points,
point-health) but not connected-station records, configuration
mutations, or speed-test invocations. Each verb's CLI path is wired
fully so operator scripts can be authored today; the FoyerClient
method body raises `EXIT_UNSUPPORTED_FEATURE` with a hint pointing
at the Phase-C deferral until the specific Foyer RPC is mapped:

- `wifi list clients <group>` (FR-WIFI-3)
- `wifi pause / unpause <client-id>` (FR-WIFI-4..5)
- `wifi prioritize <client-id> --duration <minutes>` (FR-WIFI-6)
- `wifi group-assign <client-id> --group <choice>` (FR-WIFI-7)
- `wifi speedtest run <group> --timeout <s>` (FR-WIFI-8)
- `wifi speedtest history <group> --limit <N>` (FR-WIFI-9)
- `wifi reboot point <point>` (FR-WIFI-10)
- `wifi reboot group <group>` (FR-WIFI-11)
- `wifi network <group>` (FR-WIFI-13) — `GetHomeGraph` carries no
  SSID/IPv4/IPv6/DNS data, so returning placeholder `"<unknown>"`
  records would mislead operators piping output through `jq`. Verb
  exits 5 instead.
- `wifi guest enable / disable <group>` (FR-WIFI-14)

### Migration

Operators with an existing v1 `credentials-wifi.json` will see exit 6
(`config_error`, `family=wifi`) on first wifi command after Phase B.
Re-run `auth wifi-setup --overwrite --experimental-wifi --android-id <hex>`
to write a v2 file.

## [0.4.0] - 2026-05-03

### Added

Phase 4 (SRD §16.6) — bulk operation primitives. Final phase of v1
SRD; cross-cutting cam + wifi.

- `resolve_target_or_group(config, target_or_group, *, expected_family)`
  in `nest_cli/cli/_shared.py` (FR-5, FR-6) — translates a plain
  alias, literal device path, or `@group-name` into an ordered list
  of `ResolvedTarget` records. Cross-family group memberships
  resolve with `family_match=False` so the executor can emit FR-5
  exit-5 records for wrong-family members without aborting the rest
  of the group. Unknown groups and groups referencing missing
  aliases both surface as exit 4.
- `nest_cli/cli/_fanout.py` — fan-out execution helper
  (`fan_out_verb`) wrapping `ThreadPoolExecutor` with order-
  preserving result collection. Default concurrency 3 (FR-7,
  configurable via per-verb `--concurrency N`). Computes the FR-8a
  aggregate exit code (0 / 7 / first-failure-code in resolved-
  config order) and emits one FR-9a envelope per resolved target.
  Synthesizes exit-5 records for `family_match=False` targets
  without invoking the verb callable (FR-5).
- `nest-cli batch --file <path>` / `--stdin` (FR-9, FR-10) — reads
  newline-delimited commands and dispatches each via Click's in-
  process `CliRunner` so SystemExit is captured per-line. Each
  invocation gets `--jsonl` injected into its argv (when no output
  flag is already present) so the inner verb's stdout is parseable
  and stuffable into the FR-9a `result` field. Empty input exits 0
  with no stdout (FR-10b); blank lines and `#` comments are silently
  skipped.
- SIGINT/SIGTERM handling for batch (FR-10c): cease dispatching new
  sub-ops, emit final
  `{"event":"interrupted","completed":N,"pending":M}` summary line,
  exit 130 (SIGINT) or 143 (SIGTERM). Handlers save+restore so
  in-process tests don't leak state.
- `cam stream` and `cam events --follow` reject `@group` targets
  with exit 64 (FR-8c, FR-8d). `cam events` without `--follow` MAY
  accept a group target and the verb does NOT exit 64 on `@group`
  (the one-shot drain has bounded output that fan-out can demux).
- Per-verb group fan-out wired into `cam info` and `wifi pause` as
  representative integrations:
  - `cam info @home-cams` → FR-9a JSONL per camera; pre-loads cam
    credentials once before the fan-out so per-target threads share
    a refreshed access token.
  - `wifi pause @kids-devices` → FR-9a JSONL per resolved client
    with the `wifi:` prefix stripped before the FoyerClient call.
- 47 new tests (396 → 443) under `tests/batch/` plus
  `tests/cam/test_stream_group_reject.py` and
  `tests/cam/test_events_follow_group_reject.py`. Coverage:
  resolver shape (single alias, literal path, `@group`, unknown
  group, unknown member alias, cross-family flagged), executor
  ordering / concurrency / exit-code arithmetic / cross-family
  synthesis, batch happy path / partial failure / first-failure-
  code aggregate / empty input / comments / SIGINT / SIGTERM /
  handler restoration, and the cam-info-group + wifi-pause-group
  end-to-end integrations.

### Changed

- `nest_cli/cli/__init__.py` registers the new `batch_cmd` verb on
  the root `cli` group.
- `family_for_target` in `_shared.py` now returns
  `Literal["cam", "wifi"]` instead of `str` so the
  `ResolvedTarget` dataclass stays statically type-checked.

### Deferred (Phase 5+)

- `groups add` / `groups remove` mutations (FR-8b — explicit
  deferral; mutations remain manual TOML edits).
- Whole-batch concurrency (the brief is per-group concurrency, not
  whole-batch concurrency).
- Cron-like scheduled batch.

## [0.3.1] - 2026-05-03

### Added

Phase 3.1 (SRD §16.5) — wifi speedtest, reboot, network info,
guest toggle, point-health verbs (FR-WIFI-8..15). All sub-verbs
gated by `--experimental-wifi`; structured errors carry `family=wifi`.

- `nest_cli/wifi/types.py` extended with three new pydantic records:
  `SpeedTest` (§10.9) with bps→Mbps normalization at the
  `from_googlewifi_response` boundary and FR-22 `Z`-suffix RFC 3339
  serialization; `WifiNetwork` (§10.10) with nested
  `WifiNetworkIPv4` / `WifiNetworkIPv6` sub-models and defensive
  `<unknown>` fallbacks for sparse Foyer payloads;
  `WifiPointHealth` (§10.11) with `from_wifi_point` projection.
- `FoyerClient` extended with seven new methods —
  `run_speedtest(group_id, timeout_s=180)` wraps upstream
  `run_speed_test` inside `asyncio.wait_for`; `get_speedtest_history`
  reads `speed_test_results`, sorts descending by `ts`, truncates
  client-side; `reboot_point` validates the point exists then calls
  upstream `restart_ap`; `reboot_group` resolves the point list,
  calls `restart_system`, returns the rebooted ids; `get_network_info`
  projects `get_systems()` onto §10.10; `set_guest_enabled` raises
  EXIT_UNSUPPORTED_FEATURE (upstream gap, mirrors set_station_group);
  `get_point_health` locates a point across groups and projects onto
  §10.11.
- `wifi speedtest run <group> --timeout <s> --experimental-wifi`
  (FR-WIFI-8) — block until done; emit §10.9 SpeedTest record.
- `wifi speedtest history <group> --limit <1..365>
  --experimental-wifi` (FR-WIFI-9) — descending-by-ts results.
- `wifi reboot point <point> --experimental-wifi` (FR-WIFI-10) —
  TTY confirmation + non-tty `--yes` requirement.
- `wifi reboot group <group> --experimental-wifi` (FR-WIFI-11) —
  single confirmation, names the resolved point list on stderr.
- `--quiet` implies `--yes` for both reboot verbs (FR-WIFI-12).
- `wifi network <group> --experimental-wifi` (FR-WIFI-13) — emit
  §10.10 WifiNetwork.
- `wifi guest enable|disable <group> --experimental-wifi`
  (FR-WIFI-14) — CLI surface ships; FoyerClient raises exit 5
  pending upstream googlewifi guest-network setter.
- `wifi point-health <point> --experimental-wifi` (FR-WIFI-15) —
  emit §10.11 WifiPointHealth.
- 69 new tests (327 → 396): types, client, gates, TTY/non-tty
  confirmation paths, error envelopes, all family=wifi.

### Changed

- `nest_cli/cli/wifi_cmd.py` adds nested Click subgroups
  (`speedtest`, `reboot`, `guest`) so two-level verb names like
  `wifi speedtest run` register cleanly.

## [0.3.0] - 2026-05-03

### Added

Phase 3 (SRD §16.4) — wifi side gated behind `--experimental-wifi`.

- `nest_cli/wifi/{client,types}.py` (new package) — `FoyerClient` sync
  facade over `googlewifi.GoogleWifi` (lazy-imported; lives in optional
  `[wifi]` extra). `WifiGroup` (§10.6), `WifiPoint` (§10.7),
  `WifiClient` (§10.8) pydantic models.
- `nest_cli/auth/wifi_credentials.py` + `wifi_types.py` (new) — wifi
  master-token credential I/O with chmod 0600 enforce, atomic write,
  flock-with-O_NOFOLLOW (mirrors v0.1.0 cam-side patterns).
- `nest_cli/cli/wifi_cmd.py` (new) — `wifi` Click group with all 7 verbs.
- `nest_cli/cli/_shared.py:experimental_wifi_gate()` — shared FR-WIFI-0
  enforcement helper.
- `auth wifi-setup --experimental-wifi` (FR-CRED-7) — accepts Google
  email + Android master token via stdin / `--master-token-file` /
  `GOOGLE_ANDROID_MASTER_TOKEN`; persists Foyer-usable token to
  `~/.config/nest-cli/credentials-wifi.json` chmod 0600.
- `auth wifi-revoke --experimental-wifi` (FR-CRED-9) — atomic stub-replace
  + stderr reminder pointing at `myaccount.google.com/permissions`.
- `auth status` (FR-CRED-10) — extended to emit a 2-element JSON array
  with both cam and wifi family records (`configured: false` when
  credentials absent).
- `wifi list groups --experimental-wifi` (FR-WIFI-1).
- `wifi list points <group> --experimental-wifi` (FR-WIFI-2).
- `wifi list clients <group> --experimental-wifi` (FR-WIFI-3).
- `wifi pause <client-id> --experimental-wifi` (FR-WIFI-4) — idempotent.
- `wifi unpause <client-id> --experimental-wifi` (FR-WIFI-5) — idempotent.
- `wifi prioritize <client-id> --duration <1..240> --experimental-wifi`
  (FR-WIFI-6) — Google Wi-Fi boost; default 60min.
- `wifi group-assign <client-id> --group <family|parental|guest|none>
  --experimental-wifi` (FR-WIFI-7) — case-insensitive choice.
- 108 new tests (219 → 327): auth-wifi I/O, FoyerClient surface, list
  verbs, action verbs, experimental-wifi gate enforcement, chmod
  invariants, error envelope shape, mock googlewifi corpus.
- `pyproject.toml` `[wifi]` optional extra — `glocaltokens` + `googlewifi`
  pinned per SRD §13.2.
- `tests/fixtures/foyer/samples/{groups,access_points,devices}.json`
  (new) — sanitized mock corpus.

### Changed

- `nest_cli/errors.py:StructuredError` — added optional
  `family: Literal["cam", "wifi"] | None = None` field. Wifi verbs
  emit `family="wifi"` per SRD §11.3; cam-side errors keep v0.1.0 /
  v0.2.x back-compat (no `family` field — documented deviation in
  ARCHITECTURE.md).
- `auth status --json` payload changed from a 1-element array (cam
  only) to a 2-element array (cam + wifi). Wire-level change for any
  operator scripting against `len(...)`.
- `docs/ARCHITECTURE.md` — documents the FR-WIFI-0 (exit 64) vs
  SRD §11.2 (exit 5) ambiguity resolution and the family-field policy.

### Deferred

- `wifi speedtest run / history` (FR-WIFI-8..9) → Phase 3.1.
- `wifi reboot point / group` (FR-WIFI-10..12) → Phase 3.1.
- `wifi network` / `wifi guest enable|disable` (FR-WIFI-13..14) → Phase 3.1.
- `wifi point-health` (FR-WIFI-15) → Phase 3.1.
- Cam-side `family` field retrofit → follow-up.
- `scripts/smoke-wifi.py` rewrite to match real `googlewifi` async API
  (current script references methods that don't exist upstream).

## [0.2.1] - 2026-05-03

### Added

Phase 2.1 (SRD §16.3) — long-running `cam events --follow` event subscription.

- `cam events --follow` long-poll loop with capped exponential backoff
  (FR-CAM-21, FR-CAM-23). Emits each event as JSONL on stdout as it
  arrives. On SIGINT/SIGTERM: ceases pulling, ack's any in-flight
  consumed messages, emits a final JSONL summary line
  `{"event": "interrupted", "received": N}` to stdout, exits 130
  (SIGINT) or 143 (SIGTERM). Backoff schedule
  `1s → 2s → 4s → 8s → 16s → 32s → 32s`; five consecutive failures
  exit 3 (network) with a structured error naming the last failure;
  a successful pull resets the counter.
- `--types <comma-list>` filter (FR-CAM-22) applies in both follow
  and one-shot drain modes; valid tokens are the §10.3 enum
  (`motion`, `person`, `package`, `sound`, `doorbell-press`,
  `unknown`). Invalid tokens exit 64 with a hint listing valid values.
- 10 new tests in `tests/cam/test_events_follow.py` covering happy path,
  SIGINT/SIGTERM exit-code mapping, type filter, invalid-type usage
  error, backoff schedule durations, five-consecutive-failures exit,
  counter-reset-on-success, target+type combined filtering, and the
  `--quiet` summary-line override (FR-CAM-21 explicitly emits the
  summary in all output modes).

### Changed

- `nest_cli/cli/cam_events_cmd.py` — refactored the message-processing
  loop into a shared `_process_messages` helper that returns
  `(ack_ids, emitted_count)`. The follow loop uses both; the one-shot
  drain ignores `emitted_count`. Target-filter ack semantics
  (reviewer feedback C7 from Phase 2) carry forward unchanged: events
  whose target does NOT match are LEFT in the subscription; events
  whose target matches but type is filtered ARE acked.

## [0.2.0] - 2026-05-03

### Added

Phase 2 ships the full cam control surface beyond v0.1.0's read-only verbs
(SRD §16.2 / FR-CAM-3..16, FR-CAM-19..27).

- `nest_cli/sdm/client.py:execute_command()` — public POST wrapper for SDM
  `:executeCommand` with auth-refresh + 401-retry + status mapping that
  mirrors `_get_with_refresh`. Used by every command-issuing verb.
- `nest_cli/sdm/stream_types.py` — typed result records for the stream
  surface (`Stream`, `RtspStreamResult`, `WebRtcStreamResult`).
- `nest_cli/sdm/event_types.py` — typed result records and
  `parse_pubsub_event()` for the events surface.
- `nest_cli/cli/cam_stream_cmd.py` — `cam stream`, `cam stream-extend`,
  `cam stream-stop` verbs (RTSP and WebRTC variants).
- `nest_cli/cli/cam_events_cmd.py` — `cam events` one-shot Pub/Sub drain.
- `cam snapshot <target>` — two-tier fallback (CameraImage →
  CameraEventImage). FR-CAM-3..5, FR-CAM-4a tier-1 auth short-circuit.
- `cam stream <target>` — RTSP variant emits directly-usable URL +
  extension token; WebRTC variant requires `--offer-sdp <path-or-stdin>`
  and emits answer SDP + media session id (Decision 6: operator owns
  SDP generation in v1).
- `cam stream-extend / stream-stop` — RTSP session lifecycle.
- `cam chime <target>` — DoorbellChime invocation; non-doorbells exit 5
  with a hint listing chime-capable aliases.
- `cam battery <target>` / `cam signal <target>` — predicate-gated on
  Camera-record presence (SDM has no public `Battery` / `RSSI` trait).
- `cam events [<target>]` one-shot drain — `--follow` deferred to
  Phase 2.1 (FR-CAM-21..23). Emits §10.3 Event records as JSONL.
- 71 new tests (138 → 209 passing) covering each verb, the
  `executeCommand` wrapper, stream-result parsers, Pub/Sub event
  shaping, and reviewer-flagged edge cases.

### Changed

- `nest_cli/cli/cam_cmd.py:_TRAIT_TO_VERBS` — extended for Phase 2 verbs.
  `DoorbellChime` widened to `["chime", "events"]` since the trait
  gates both verbs.
- `nest_cli/cli/cam_cmd.py:_PREDICATE_VERBS` — new companion table for
  verbs gated on parsed-record presence (battery, signal).
- `nest_cli/cli/_shared.py` — `exit_on_structured_error` annotated as
  `NoReturn`, fixing mypy narrowing across all callers.

### Fixed

Multi-reviewer feedback on PR #4 addressed in 9 fix commits:

- Snapshot tier-1 failure now correctly advances to tier 2 per FR-CAM-4
  (previously only advanced on missing-trait, contrary to spec).
  `EXIT_AUTH_ERROR` continues to short-circuit per FR-CAM-4a.
- `cam events <target>` no longer ack-leaks events for OTHER cameras —
  ack ids are appended after the target filter, not before, so events
  for non-matching targets remain in the subscription for a future
  drain.
- Snapshot error envelopes no longer include token-bearing SDM result
  dicts. `_parse_image_url_and_token` emits `result_keys` instead;
  `_download_snapshot_bytes` redacts URL query strings before logging.
- Pub/Sub ack failures now surface as a stderr warning instead of being
  silently suppressed; the narrower exception family is caught
  (`google.api_core.exceptions.GoogleAPICallError`, `OSError`,
  `TimeoutError`).
- `--offer-sdp` capped at 64KB and required to start with `v=0`
  (RFC 4566 protocol-version line).
- Stream protocol detection now distinguishes "trait absent" (exit 5)
  from "trait present, protocols unrecognized" (exit 1) instead of
  silently defaulting to webrtc.
- `cam snapshot --output -` paired with `--quiet` now exits 64 (the
  combination would silence the only output channel).
- Hardcoded `code=3` literal in `cam_events_cmd.py` replaced with
  `EXIT_NETWORK_ERROR` constant.

### Documentation

- `README.md` — status block updated to v0.1.0-shipped reality;
  quick-start examples now reflect verbs that actually work; added a
  link to the operator runbook.
- `docs/ONBOARDING.md` (new) — operator runbook: Google Cloud + Device
  Access setup, OAuth client creation, `auth setup` walkthrough,
  smoke-test flow, troubleshooting, where credentials live.
- `docs/ARCHITECTURE.md` — added a Phase 1 implementation map: module
  layout, request flow for cam verbs, output/error contract, threat
  model excerpt, what v0.1.0 deliberately does NOT include.
- `docs/SECURITY.md` — replaced the pre-release contact-email TODO with
  a GitHub Security Advisory link; updated supported-versions table.

### Deferred

- `cam events --follow` long-poll mode → Phase 2.1.
- `auth setup --pubsub` topic + subscription provisioning → Phase 5+.
  Operator manually creates the subscription and grants
  `roles/pubsub.publisher` to Google's SDM service account.
- WebRTC `mediaSessionId`-keyed `stream-extend` / `stream-stop` (RTSP
  form ships in v0.2.0; WebRTC form uses a different flag).
- ffmpeg-from-RTSP snapshot tier 3 fallback.

## [0.1.0] - 2026-05-01

### Added

- `nest_cli/errors.py` — SRD §11.1 `EXIT_*` constants and the `StructuredError`
  dataclass with a stderr emitter that honors text vs JSON output mode.
- `nest_cli/output.py` — `add_output_options` decorator (`--json`, `--jsonl`,
  `--quiet`, `--output`) plus a mode-aware `emit()` function. Mutually
  exclusive flag combinations exit 64 with a structured error.
- `nest_cli/config.py` — `tomllib`-based TOML parser for the local config
  with `[aliases]` and `[groups]` sections (extra="forbid"), plus
  `default_config_path()` honoring `XDG_CONFIG_HOME`.
- `nest_cli/sdm/types.py` — Pydantic `Camera` and `CameraTrait` records per
  SRD §10.1, normalizing the SDM API's trait-dict into a list of name-keyed
  records.
- `nest_cli/sdm/client.py` — `SdmClient` thin wrapper around `requests`
  with auto-refresh on 401 and structured-error mapping for 4xx/5xx and
  network failures.
- `nest_cli/cli/list_cmd.py` — `list` (FR-1, FR-1a, FR-1b, FR-1c, FR-1d) and
  `discover` (FR-2, FR-2a) commands.
- `nest_cli/cli/cam_cmd.py` — `cam` subgroup with `list`/`info`/`capabilities`
  (FR-CAM-1, FR-CAM-2, FR-CAM-28).
- `nest_cli/cli/config_cmd.py` — `config` subgroup with `show`/`validate`
  (FR-16c).
- `nest_cli/cli/__init__.py` — root Click group `cli` wiring all subgroups.
- `requests>=2.31,<3` as an explicit dependency (was previously transitive
  via `google-auth-oauthlib`).
- Comprehensive mocked test coverage: `tests/test_errors.py`,
  `tests/test_output.py`, `tests/test_config.py`, `tests/sdm/test_client.py`,
  `tests/test_cli_list.py`, `tests/test_cli_cam.py`, `tests/test_cli_config.py`.

### Changed

- `nest_cli/__main__.py` — replaced the Phase 0 stub with a thin
  `from nest_cli.cli import cli as main` entry point.
- `nest_cli/__init__.py` — `__version__` bumped to `0.1.0`.
- `pyproject.toml` — `version` bumped to `0.1.0`.
- `tests/test_skeleton.py` — version assertions updated to `0.1.0`.
- `nest_cli/cli/auth_cmd.py` — rebased onto the shared `add_output_options`
  decorator and `emit()`/`exit_on_structured_error` infrastructure. Every
  `auth` verb now honors `--json`, `--jsonl`, `--quiet`, and `--output`
  uniformly with the rest of the CLI. The local `_emit`,
  `_exit_with_credential_error`, and `_OUTPUT_*` helpers were removed.
  Error envelopes no longer carry a `family` discriminator (not in SRD
  §11.3); the discriminator surfaces in the `auth status` payload only.
- `nest_cli/cli/auth_cmd.py` — `auth status` emits a JSON array per
  FR-CRED-10 (one element for the cam family; Phase 3 will add the wifi
  element).
- `nest_cli/cli/config_cmd.py` — `config show` text mode emits TOML per
  FR-16c (round-trips through `tomllib`); JSON modes continue to emit
  the structured-record dict.
- `nest_cli/auth/types.py`, `nest_cli/sdm/types.py`, `nest_cli/output.py`
  — datetime fields now serialize as RFC 3339 UTC with the literal `Z`
  suffix per FR-22, both via Pydantic `field_serializer` and through the
  shared `_to_jsonable` / `_pydantic_default` paths.
- `nest_cli/auth/credentials.py` — `EXIT_*` constants now imported from
  `nest_cli.errors` (single source of truth, SRD §11.1). The lock-file
  open path was hardened against a symlink-substitution race
  (`O_CREAT|O_EXCL|O_NOFOLLOW` first, `O_NOFOLLOW` fallback); a
  pre-existing symlink at `<creds>.lock` is rejected with a structured
  auth error.

### Fixed

- Re-sort the stdlib import block in `nest_cli/auth/credentials.py` so
  ruff I001 stops failing CI (`random` and `time` had been inserted out
  of alphabetical order by the lock-jitter fix).

## [0.0.1] - 2026-05-01

### Fixed

- Commit `uv.lock` so GitHub Actions `setup-uv@v3` cache restoration succeeds
  (the default `cache-dependency-glob: **/uv.lock` was failing against an
  ignored lockfile and breaking CI on every push).
- Move the CI credentials guard to the FIRST workflow step so a malicious
  transitive dependency cannot read secret env vars during `uv sync` before
  the guard fires. Broaden it from one variable to three:
  `NEST_CLI_TEST_OAUTH_CREDENTIALS`, `GOOGLE_APPLICATION_CREDENTIALS`,
  `NEST_CLI_TEST_FOYER_TOKEN`.
- `scripts/smoke-cam.py`: register `assignee` (mapped to `ASSIGNEE_PATH`) in
  the redaction registry. Real SDM `devices.get` responses include
  `assignee: enterprises/{project_id}/structures/{structure_id}/rooms/{room_id}`,
  which the post-scan veto was hard-failing on — blocking every operator
  fixture-capture run.
- `scripts/smoke-wifi.py`: comprehensive redaction overhaul.
  - Snake_case / kebab-case / camelCase key normalization in `_classify`
    so `friendly_name`, `friendly-name`, and `friendlyName` all hit the
    same rule.
  - Replace bare `id` substring matcher with a curated exact-match
    registry plus a separator-bounded endswith regex
    (`(?:_id|-id|[a-z]Id)$`) — catches `group_id`, `device-id`, `groupId`
    without over-firing on `paid`, `solid`, `valid`, `width`, `guidance`.
  - Add LAN-topology classes (`subnet`, `gateway`, `dhcp_range_start`,
    `dhcp_range_end`, `dns_servers`, `dns`, `wan_ip_address`).
  - Mirror cam-script's post-scan veto: `_LEAK_PATTERNS` for MAC,
    email, IPv4 (with `0.0.0.0`/`127.0.0.1`/`255.255.255.255` allowlist),
    UUID. `_write_fixture` raises `RedactionError` on any hit; `main`
    returns exit code 4.
  - Distinguish exception categories in `main` — network errors,
    upstream-shape errors (Foyer rotation), and redaction errors each
    print a category-specific message instead of a single bare blanket.
  - Validate that `--master-token` flag value is non-empty / non-whitespace
    (previously only the stdin path checked).
  - List-of-strings under a classified key (e.g. `dns_servers: ["8.8.8.8",
    "1.1.1.1"]`) now redact every element instead of slipping through.
- README v0.0.1-honest scope: status block now reflects skeleton state,
  install command points at git+https, Quick start codeblock prefaced
  with a "Coming in v0.1.0" warning.

### Added

- Initial repo skeleton, dependency pinning, CI baseline.
- `pyproject.toml` with hatchling build backend, pinned cam-side dependencies
  (`google-nest-sdm>=7.1,<8`, `google-cloud-pubsub>=2.36,<3`, `click>=8.1,<9`,
  `pydantic>=2.5,<3`) and `[wifi]` optional extras (`googlewifi>=0.0.21,<0.1`,
  `glocaltokens>=0.7,<0.8`).
- `nest_cli/__main__.py` Click stub — `--version` and `--help` only; no verbs
  registered until Phase 1.
- GitHub Actions CI on Python 3.11 and 3.12: ruff lint + format check, mypy,
  pytest, plus a hard guard that fails the build if real OAuth credentials
  leak into the runner environment.
- Smoke tests (`tests/test_skeleton.py`) covering import, `--version`, and
  no-arg help output.
- `docs/SECURITY.md` stub pointing at the SRD threat model (§4.7).

### Notes

- SRD §15 Decision 23 prefers git-SHA pins for the wifi-side libraries
  (`googlewifi`, `glocaltokens`) because they are single-maintainer with a
  history of upstream rotations. Phase 0 ships tight semver pins as a
  pragmatic first cut; tightening to SHAs is tracked for a future release
  before Phase 3 (the wifi slice) lands.
- `google-nest-sdm` is pinned to the 7.1.x line (latest 7.x is 7.1.5) rather
  than the current 9.x release. The 8.0.0 cut raised the Python floor to
  3.13, which would conflict with SRD §11's stated `requires-python = ">=3.11"`.
  Phase 0 holds the 3.11 floor; bumping to 3.13 (and to `google-nest-sdm` 9.x)
  is a deliberate future decision, not a Phase 0 carry-along.
