# Software Requirements Document: nest-cli

**Document ID:** SRD-NEST-CLI-001
**Version:** 1.0.0
**Date:** 2026-04-30
**Status:** Draft — Initial
**Author:** Dan Elliott
**Source:** Derived from operator requirements + Google Smart Device Management (SDM) API public documentation at `developers.google.com/nest/device-access` + Device Access Console at `console.nest.google.com/device-access` + community libraries `glocaltokens` (PyPI), `googlewifi` (PyPI), `python-google-wifi` (PyPI), and Home Assistant's `google_nest`, `nest`, and `google_wifi` integrations as the de-facto reverse-engineering reference. v1.0.0 is the initial scoping draft; no code has shipped. Phase 0 hardware smoke-test gate (§16.0) ratifies all Source claims against live devices before Phase 1 begins.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Background and Prior Art](#3-background-and-prior-art)
4. [Architecture Decision: Wrap vs Reimplement (per family)](#4-architecture-decision-wrap-vs-reimplement-per-family)
5. [Functional Requirements](#5-functional-requirements)
6. [Authentication and Credentials](#6-authentication-and-credentials)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [CLI Surface](#8-cli-surface)
9. [Configuration File](#9-configuration-file)
10. [Data Model](#10-data-model)
11. [Error Model and Exit Codes](#11-error-model-and-exit-codes)
12. [Testing Strategy](#12-testing-strategy)
13. [Distribution and Install](#13-distribution-and-install)
14. [Out of Scope](#14-out-of-scope)
15. [Resolved Decisions](#15-resolved-decisions)
16. [Phase Plan](#16-phase-plan)
17. [Open Questions and Decisions Deferred](#17-open-questions-and-decisions-deferred)

---

## 1. Overview

`nest-cli` is a deterministic, scriptable command-line tool for querying and controlling two distinct Google device families that share little more than a brand: **Google Nest cameras and doorbells** (governed by the official Smart Device Management — SDM — API), and **Google Nest WiFi mesh routers** (governed by no official API at all, only an undocumented internal gRPC surface used by the Google Home mobile app, plus a small ecosystem of reverse-engineered Python libraries on top of it). It is the third sibling in the Dan operator-toolchain after `kasa-cli` and `tapo-cli`: same product philosophy — single binary, one verb per invocation, JSON/JSONL on stdout, deterministic exit codes, no GUI, no daemon, no automation rules engine — but a fundamentally different transport story. Where the LAN-only siblings deliberately refuse to make outbound connections, `nest-cli` cannot avoid Google's cloud control plane on **either** side.

**This is the central architectural fact and the SRD will not paper over it.** Nest cameras require a Google Cloud project, a one-time $5 USD per-developer Device Access registration, an OAuth 2.0 user-consent flow, and a refresh-token-driven session against `smartdevicemanagement.googleapis.com` for every command. Nest WiFi has no public API at all — its only programmatic path is the internal **Google Home Foyer** gRPC service used by the Home/Nest mobile apps, which the open-source community accesses via a master-token bootstrap derived from `glocaltokens` and consumed by `googlewifi` / `python-google-wifi`. The two sides do not share an auth token, do not share a transport, and do not share a maintenance risk profile. Pretending they do — by hiding the asymmetry behind a unified facade — would produce a tool that lies about its operational risk to the operator. The CLI's surface therefore exposes the asymmetry **on purpose** through two top-level command groups, `cam` and `wifi`, each with its own auth flow, its own credential file, and its own honest documentation of what it can and cannot promise.

The CLI deliberately accepts a stricter contract on the cam side (where the API is stable, documented, and honored by Google as a supported product) and a softer, opt-in contract on the wifi side (where the surface is reverse-engineered, the libraries are single-maintainer-fragile, and Google has historically rotated endpoints with no notice). WiFi sub-verbs ship behind a runtime `--experimental-wifi` posture mirroring `tapo-cli`'s `--experimental-clips` precedent: the operator opts in to the firmware-fragility risk per-invocation, and the CLI emits a documented warning every time the experimental path fires. Cam sub-verbs carry no such guard — they ship as supported features the operator can rely on in long-running scripts and cron jobs.

The CLI is **not** a video player, not a NVR, not an event-clip downloader, not a HomeKit bridge, not a Matter bridge, not a Google Cloud Pub/Sub long-runner, not a rules engine, and not a GUI. It does not transcode WebRTC streams to RTSP for downstream consumption (that is `aiortc` plus a media server's job). It does not subscribe to Pub/Sub event streams as a daemon (that violates the deterministic-CLI ethos and is what Home Assistant exists for). It does not own the operator's Google Cloud project or pay the $5 fee on the operator's behalf. It is a single binary that takes a verb, a target, and flags, performs one operation against one device family's cloud control plane, prints a result on stdout — typically JSON — and exits with a meaningful status code. Its job is to be the leaf node in a shell pipeline or cron job, the same as its siblings.

The reader of this SRD should leave with three durable mental models: (1) **cam** verbs talk to the SDM API and rest on a documented Google contract; (2) **wifi** verbs talk to Foyer through reverse-engineered libraries and rest on community goodwill; (3) when (3) breaks — and it will, repeatedly, over the lifetime of the tool — the cam side keeps working, and the operator's scripts that touch only the cam side keep working too. The architectural division is what makes that compartmentalization possible.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- **Cam: Discover** every Nest camera, doorbell, display, and thermostat the user's Google account is authorized to access via SDM, and emit a structured device list (filterable to cameras-only by default).
- **Cam: Query** per-camera state — model, generation, traits array, room/structure assignment, online/offline, last-event timestamp, battery percentage and signal strength on battery-powered cameras (Battery Cam, Battery Doorbell), firmware version where exposed.
- **Cam: Emit live-stream URLs** for downstream consumption. RTSP for 1st-gen Nest Cams (via `GenerateRtspStream`); WebRTC offer/answer SDP exchange for 2nd-gen Battery Cam, Battery Doorbell, Floodlight Cam, and post-2021 hardware (via `GenerateWebRtcStream`). The CLI does NOT decode, transcode, or proxy video — it emits the negotiated session metadata and exits.
- **Cam: Pull a snapshot still image** to a local file via SDM's `CameraImage.GenerateImage` on supported generations, falling back to event-image retrieval (`CameraEventImage.GenerateImage` keyed off the most recent qualifying `eventId`) on WebRTC-only cameras that do not expose `CameraImage`.
- **Cam: List events** — motion, person, package, sound, doorbell-press — pulled from the pub/sub-driven SDM event surface in either a one-shot mode (last N events) or, behind `--follow`, a long-running subscription that emits each new event as JSONL on stdout.
- **Cam: Doorbell chime** — fire the doorbell-press chime sound via `DoorbellChime.Chime` on doorbell hardware that supports it. **(There is no talkback in SDM, see §2.2.)**
- **Cam: Quiet hours / arming / disarming** — surface these only insofar as SDM exposes them. **As of v1.0.0 of this SRD, SDM does NOT expose arming/disarming or quiet hours;** the CLI reflects that honestly with documented `unsupported_feature` exit codes (5) and a §17 open question to revisit if Google adds the surface.
- **WiFi (experimental): Discover routers, points, and clients** behind the operator's Google account, via the Foyer-backed `googlewifi` library plus `glocaltokens` master-token bootstrap.
- **WiFi (experimental): Per-client controls** — pause, unpause, prioritize for a TTL window, group assignment (family / parental / guest).
- **WiFi (experimental): Speed test** — trigger a fresh test (`speedtest run`) and emit the result; list historical test results (`speedtest history`).
- **WiFi (experimental): Router and point health** — uptime, current signal strength to upstream point, connected-clients count.
- **WiFi (experimental): Reboot** a single point or the whole mesh, with explicit confirmation in tty mode and `--yes` requirement in non-tty.
- **WiFi (experimental): Network info** — SSID, guest network state, IPv4/IPv6 status, WAN connection state.
- **Be scriptable**: deterministic exit codes, JSON/JSONL output, no interactive prompts in non-tty mode (except `auth setup` whose interactivity is the entire point).
- **Group devices logically** via local TOML config (alias-to-target map and group-to-alias-list map). The CLI never writes the config; mutations are by hand-editing.
- **Run batch operations** across multiple devices in parallel, with bounded concurrency and structured per-target failure reporting.
- **Cache OAuth refresh tokens and Foyer master tokens** between invocations to avoid per-command auth latency, with chmod-0600 enforcement and per-device session-level locking on writes.

### 2.2 Non-Goals

- **No video display in the terminal.** `cam stream` emits session metadata. Decoding is the consumer's job (`mpv`, `ffplay`, an RTSP-to-HLS gateway, etc.).
- **No WebRTC-to-RTSP relay or transcoding.** A genuine WebRTC client requires a full ICE/DTLS/SRTP stack (e.g., `aiortc`) and a media server downstream. That is a separate project (and probably not a CLI). The CLI emits the offer SDP and the credentials needed for a downstream consumer to negotiate; what the consumer does is the consumer's job.
- **No talkback (two-way audio).** SDM does not expose talkback on any camera or doorbell. Out of scope at all phases of this SRD. Documented as a §17 open question only because operators may believe SDM exposes it; it does not.
- **No siren control.** Nest cameras do not have a programmable siren in the SDM trait set. Out of scope.
- **No quiet-hours / arming / disarming.** SDM does not expose these as of v1.0.0 of this SRD. **§17 tracks the open question** in case Google ships the trait; the CLI reflects current reality and exits 5 if asked.
- **No NVR functionality.** Long-running multi-day recording, retention policies, motion-triggered DVR — wrong tool. Use Frigate, Shinobi, or Synology Surveillance Station downstream of `cam stream`.
- **No event-clip download.** SDM does not expose stored video clips for download via the public API. Even on Nest Aware subscriptions, the clip-replay surface is mobile-app-only. Out of scope at all phases.
- **No automation rules engine.** "If motion at front-door, turn on porch-light" belongs in Home Assistant or a cron job that pipes `cam events` into `kasa-cli` / `hue-cli`.
- **No GUI dashboard.** This is a CLI.
- **No Matter or Thread support.** Different protocol stack entirely.
- **No `cam` <-> `wifi` cross-device verbs.** The CLI deliberately keeps the two families on parallel tracks. Verbs like "pause the camera client on the WiFi mesh" require the operator to chain two invocations; the CLI does not provide a fused verb.
- **No Google Cloud project provisioning from the CLI.** The operator owns the Google Cloud project, pays the $5 Device Access fee, and downloads OAuth client credentials. The CLI consumes those artifacts; it does not create them. `auth setup` is a guided walkthrough with stderr links, not an automated provisioner.
- **No multi-account support in v1.** One Google account at a time per credential file. Multi-account is §17 deferred.
- **No camera firmware updates.** Use the Home/Nest mobile app.
- **No Wi-Fi mesh provisioning.** Use the Home app.
- **No write paths to bridge-side schedules, structures, or rooms.** Read-only only.
- **No Apple Home / HomeKit translation.** That's `homebridge`'s job.
- **No Linux/macOS support for Google's 2024-announced Home APIs.** As of 2026, those SDKs are iOS/Android only; the CLI does not pretend to consume them. §17 tracks the open question.

---

## 3. Background and Prior Art

### 3.1 Google Smart Device Management (SDM) API

The official, supported, documented API for Nest cameras, doorbells, displays, and thermostats is the **Smart Device Management API** (`smartdevicemanagement.googleapis.com`). Documentation lives at `developers.google.com/nest/device-access`. Onboarding requires:

1. A Google Cloud project (free-tier acceptable).
2. The Smart Device Management API enabled in the project's API console.
3. A registered application in the **Device Access Console** at `console.nest.google.com/device-access`. Registration costs **$5 USD per developer, one-time** (not per project, not per user — per developer Google account). This is a real out-of-band step the operator must complete before Phase 1; the CLI cannot bypass it.
4. An OAuth 2.0 client (Web or Desktop) with the SDM scope `https://www.googleapis.com/auth/sdm.service`. For Pub/Sub event delivery, a separate Pub/Sub-related scope and topic-subscription configuration is required (§3.1.3).
5. A consenting end-user who completes the OAuth flow against their own Google account, which authorizes the CLI's project to read/control the user's Nest devices.

The result is a **refresh token + access token pair** scoped to the SDM API. The refresh token is long-lived (Google's standard refresh-token semantics — no documented expiration unless the user revokes consent); the access token expires on a 1-hour cadence and the CLI rotates it on demand.

#### 3.1.1 Camera trait surface

SDM models cameras as a `device` with a `traits` array. The traits relevant to this CLI:

| Trait | Purpose | Notes |
|-------|---------|-------|
| `sdm.devices.traits.Info` | Device name, room, structure | Always present. |
| `sdm.devices.traits.CameraLiveStream` | Stream URL acquisition (RTSP or WebRTC) | Present on all cameras. Stream protocol depends on hardware generation. |
| `sdm.devices.traits.CameraImage` | On-demand snapshot | **Absent on 2nd-gen WebRTC-only hardware.** See §3.1.2. |
| `sdm.devices.traits.CameraEventImage` | Snapshot keyed off an event id | Present on all event-emitting cameras; the only snapshot path on WebRTC-only models. |
| `sdm.devices.traits.CameraMotion` | Motion event emission | Drives Pub/Sub motion events. |
| `sdm.devices.traits.CameraPerson` | Person event emission | Pub/Sub. |
| `sdm.devices.traits.CameraSound` | Sound event emission (barking, alarms, etc.) | Pub/Sub. |
| `sdm.devices.traits.DoorbellChime` | Doorbell-press event emission **and chime command** | Doorbells only. |
| `sdm.devices.traits.CameraClipPreview` | Clip-preview metadata (preview URL, ttl) | Subset of cameras; **no actual clip download** is exposed. |

The capability matrix is **per-device-generation**, not per-model name. The CLI SHALL NOT attempt to gate on model name; it SHALL gate on the actual `traits` array Google returns at info time.

#### 3.1.2 Stream protocol per generation (the headline asymmetry)

SDM's `CameraLiveStream` trait offers two methods, and which one a given camera honors depends on hardware generation:

| Method | Hardware | Returns |
|--------|----------|---------|
| `GenerateRtspStream` | 1st-gen Nest Cam (Indoor/Outdoor/IQ), Nest Hello (wired doorbell, 1st-gen) | `streamUrls.rtspUrl` (full RTSP URL with embedded session token), `streamExtensionToken`, `streamToken`, `expiresAt` |
| `GenerateWebRtcStream` | 2nd-gen Battery Cam, Battery Doorbell, Floodlight Cam, all post-2021 hardware | `answerSdp`, `expiresAt`, `mediaSessionId` — requires the caller to have first generated and submitted an offer SDP |

The **RTSP path** mirrors `tapo-cli`'s `stream` verb shape: emit a URL, exit, downstream consumer pipes into `ffmpeg`/`mpv`. Trivial.

The **WebRTC path** does not. There is no stable URL — there is a session whose existence is bounded by `expiresAt` (typically a few minutes), keyed off a `mediaSessionId`, negotiated between an offer SDP from the consumer and an answer SDP from Google. To consume the stream, the downstream client must be a WebRTC peer that completed ICE/DTLS/SRTP setup. The CLI's job stops at emitting the answer SDP plus session metadata; the operator points a WebRTC-capable downstream client at it. v1 ships this honestly: `cam stream <target>` on WebRTC hardware emits a JSON object containing `answerSdp`, `mediaSessionId`, `expiresAt`, and the operator-supplied `offerSdp` round-trip. **The CLI does not generate the offer SDP itself in v1** — that requires either an embedded WebRTC stack (rejected as out-of-scope per Decision 6) or a subprocess to a WebRTC tool. v1 requires `--offer-sdp <path-or-stdin>` on WebRTC cameras and is honest about this in `--help`. See FR-CAM-15..18.

#### 3.1.3 Events via Pub/Sub

SDM does not expose events via REST polling. Events flow through Google Cloud Pub/Sub: the operator's GCP project owns a Pub/Sub **subscription** to a Google-published topic, and event JSON is delivered to that subscription. The CLI's `cam events` verb pulls from the operator's subscription, with `--follow` long-running and a one-shot default that drains pending messages and exits. This requires an **additional out-of-band setup step** beyond OAuth: creating the Pub/Sub topic subscription and granting the CLI's auth principal the `roles/pubsub.subscriber` role. v1 does NOT automate this step; `auth setup --pubsub` is a stretch for Phase 2 and explicitly out of scope for v0.1 / v0.2.

### 3.2 The Foyer API and the WiFi reality

There is **no public Google API for Nest WiFi.** None. Google has not shipped one, has not announced one, and the 2024-announced Home APIs are iOS/Android only with no Linux/macOS Python path as of 2026.

The only programmatic surface is the **Google Home Foyer gRPC service** at `googlehomefoyer-pa.googleapis.com`, used internally by the Google Home and Nest mobile apps. It is reverse-engineered, undocumented, and authenticated by a user-scoped **master token** that itself derives from the Google account's Android pairing — i.e., the master token is the same kind of credential the Android Google account framework hands to apps on the device. There is no OAuth flow to obtain it; it is bootstrapped by impersonating the Android `Auth` library against `IssueToken`.

Three layered libraries make this approachable:

#### 3.2.1 `glocaltokens` (Python)

`glocaltokens` (PyPI: `glocaltokens`) is the canonical Python implementation of the master-token derivation flow. It accepts a Google account email plus an Android master token (typically extracted by the operator from a previously-paired Android device via the `gpsoauth` Android app, the `auth` library, or a one-time `androidaml` bootstrap), and returns short-lived per-device "local authentication tokens" usable against the Google Home/Nest LAN protocol. **For Nest WiFi specifically, the path it enables is layered:** `glocaltokens` provides the master token, which `googlewifi` then uses to call Foyer.

The library is small (~1k LOC), single-maintainer, and used in production by Home Assistant's `google_home` integration. It has broken twice between 2022 and 2025 when Google rotated internal endpoints; the maintainer responded within 1-2 weeks each time, but the breakage is real.

#### 3.2.2 `googlewifi` and `python-google-wifi`

Two PyPI packages wrap Foyer's Wi-Fi mesh surface:

- `googlewifi` (PyPI: `googlewifi`) — currently the more actively maintained of the two; used by Home Assistant's `google_wifi` integration as of recent versions. Exposes: list groups (mesh networks the user owns), list access-points (routers and points within a group), list connected devices, pause/unpause stations, prioritize a station, set guest network on/off, fetch system metrics, run a speed test.
- `python-google-wifi` — older, less active. Functional overlap is significant; mostly retained for legacy installs.

Both depend on `glocaltokens` for the master-token bootstrap and break in the same windows when Foyer rotates. Decision 7 in §15 picks `googlewifi` as the v1 dependency, with a documented escape hatch in `pyproject.toml` to swap to `python-google-wifi` if `googlewifi` is unmaintained at Phase 3 start.

#### 3.2.3 What this means for the SRD

The WiFi side of `nest-cli` is **strictly opt-in, gated behind `--experimental-wifi`, and ships with documented breakage risk.** The CLI is honest about this: every `wifi` sub-verb's `--help` text names the upstream library, names the bootstrap requirement (Android master token), and names the rotation risk. The structured error on a Foyer-side failure includes `library: "googlewifi"` and `library_version` so operators can correlate breakage to upstream changes. When the `googlewifi` library returns an error indicating the endpoint shape changed, the CLI exits a dedicated code (1, device-error, with a hint pointing at upstream issues).

### 3.3 Why a thin custom CLI is justified

The SDM library ecosystem in Python is fragmented (`google-nest-sdm`, hand-rolled aiohttp wrappers, the official `google-api-python-client` discovery client) and the WiFi side is library-specific. Each library targets a different audience: `google-nest-sdm` is built for Home Assistant; `googlewifi` is also built for Home Assistant; the Google API discovery client is general-purpose and verbose. None of them is shaped for shell scripting.

The shell-friendly affordances `nest-cli` adds — alias/group resolution, chmod-0600 dual-credential file handling for OAuth refresh tokens AND Foyer master tokens, deterministic exit codes, JSON/JSONL output across all verbs, parallelized batch operations with structured failure reporting, the snapshot fallback chain across `CameraImage` and `CameraEventImage`, the WebRTC vs RTSP per-generation routing, the `wifi` experimental gate — are not present in any existing tool. The wrapper does not duplicate protocol work; it adds a config-and-output layer over maintained libraries and keeps the cam/wifi asymmetry surfaced.

### 3.4 Sibling tooling reference

For style and convention, this SRD inherits from:

- `kasa-cli` v1.0.0 — TP-Link Kasa LAN devices (`SRD-KASA-CLI-001`).
- `tapo-cli` v1.2.0 — TP-Link Tapo cameras over LAN with cloud-credential session derivation (`SRD-TAPO-CLI-001`).
- `hue-cli` v0.2.0 — Philips Hue Bridge over LAN with cloud-discovery fallback (`SRD-HUE-CLI-001`).

Where Nest's nature requires a divergence from these (it always does), the SRD calls it out and explains; where it does not (output formats, exit codes, batch verb shape, group config), the SRD mirrors the siblings deliberately so the operator's muscle memory carries.

---

## 4. Architecture Decision: Wrap vs Reimplement (per family)

### 4.1 Decision

**Hybrid wrap, two independent stacks per family.** Specifically:

- **Cameras (cam):** Wrap `google-nest-sdm` (PyPI: `google-nest-sdm`, the maintained Home-Assistant-adjacent SDM client). Use the official Google Pub/Sub Python client (`google-cloud-pubsub`) for the events surface. Use `aiohttp` directly for any thin endpoints not covered by `google-nest-sdm` (rare; we expect the wrapper to suffice).
- **WiFi (wifi):** Wrap `googlewifi` (PyPI: `googlewifi`) for the mesh control surface. Wrap `glocaltokens` (PyPI: `glocaltokens`) for the master-token bootstrap. Both are gated behind the `--experimental-wifi` runtime flag and are optional install extras (§13.2).
- **Subprocess `ffmpeg`** for any future RTSP-derived snapshot fallback. v1 does not currently use ffmpeg; the dependency is held in reserve and documented.

Do not reimplement the SDM REST surface, the Pub/Sub protocol, the Foyer gRPC envelopes, or the master-token bootstrap. Each is a months-of-work undertaking that adds zero operator value where a maintained library exists.

### 4.2 Rationale (cam side)

| Factor | Wrap `google-nest-sdm` + Pub/Sub | Reimplement SDM REST |
|--------|----------------------------------|----------------------|
| OAuth 2.0 flow | Free (via `google-auth-oauthlib`) | ~400 lines + token refresh edge cases |
| SDM REST envelopes | Free | ~600 lines + per-trait variance |
| Per-trait command builders | Free | Per-camera-generation churn |
| Pub/Sub subscriber | Free (`google-cloud-pubsub`) | Months of gRPC work |
| WebRTC offer/answer flow | Library-handled metadata | We'd own the SDP wrangling |
| Refresh-token rotation | Free | Custom implementation |
| Hardware generation coverage | Tracks upstream | We re-test every Google rollout |
| v1 ship time | 2-3 weeks | 3-6 months |
| Long-term burden | Minor version bumps | Multi-protocol stack maintenance |

### 4.3 Rationale (wifi side)

| Factor | Wrap `googlewifi` + `glocaltokens` | Reimplement Foyer gRPC |
|--------|-------------------------------------|------------------------|
| Foyer gRPC envelope decoding | Free | Reverse-engineer it ourselves |
| Master-token bootstrap | Free | Re-derive Android `Auth` library logic |
| Mesh group / point / station shapes | Free | Per-firmware variance |
| Rotation response time | Upstream patches (1-2 wks) | We patch every regression |
| Maintenance risk | Single-maintainer dependency | We own the whole reverse-engineered surface |
| v1 ship time | 1 week (after cam done) | Months |

### 4.4 Implementation language

**Recommendation: Python 3.11+ with `uv` for dependency and tool management.** Reasoning:

- All four primary dependencies (`google-nest-sdm`, `google-cloud-pubsub`, `googlewifi`, `glocaltokens`) are Python; using them from Python is idiomatic and avoids a process-boundary tax.
- Matches `kasa-cli` and `tapo-cli` — Dan can carry one mental model across the toolchain.
- `uv tool install nest-cli` gives single-command global install with isolated venv; updates are `uv tool upgrade nest-cli`.
- Python's async support cleanly handles the long-poll Pub/Sub subscription path required by `cam events --follow`.

### 4.5 Considered alternative: TypeScript shell-out wrapper

A Bun TypeScript wrapper that shells out to a Python helper was considered for stack consistency. Rejected for the same reasons `tapo-cli` rejected it: the Python startup tax plus a process-spawn round-trip on every invocation is roughly 2x the latency floor; the SDM and Foyer sessions become harder to cache; two languages to maintain for a single tool. If cross-toolchain stack consistency becomes a hard requirement later, a future RPC-daemon refactor is the path.

### 4.6 Considered alternative: separate binaries per family

Separately-shipped `nest-cam-cli` and `nest-wifi-cli` binaries were considered to physically isolate the experimental wifi side from the supported cam side. **Rejected** because (a) operators who own both Nest cameras and Nest WiFi want one config file and one credentials directory, not two; (b) the `auth status` verb benefits from showing both auth states side-by-side; (c) a single binary with a runtime experimental gate is the established sibling pattern (`tapo-cli motion download-clip --experimental-clips`). One binary, two command groups, asymmetric guards. Same operator surface, honest about the asymmetry.

### 4.7 Threat model

The CLI's threat surface is materially different from its siblings because **two long-lived Google credentials live on disk**:

- **OAuth refresh token (cam side).** Long-lived, scope-limited to `sdm.service` (and optionally Pub/Sub scopes for events). Compromise grants control of the operator's Nest cameras and event stream. Mitigations: chmod 0600 file mode enforced (FR-CRED-2 — exits 2 if more permissive); credential file path in the CLI's own config namespace; `auth revoke` verb that calls `https://oauth2.googleapis.com/revoke` to invalidate the token at Google's end before file deletion; structured-error redaction of the token in all log output.
- **Foyer master token (wifi side).** Long-lived, broadly scoped (Google account level — not just Wi-Fi). **This is a higher-blast-radius credential than the OAuth token.** Compromise grants Google-account-wide privileges through the Foyer service. Mitigations: same chmod 0600 enforcement; a separate credentials file (NOT in the same JSON as the OAuth token); the `--experimental-wifi` runtime gate means an operator who only uses cam features never has this token on disk; structured-error redaction; a dedicated `auth wifi-revoke` verb that scrambles the file and emits a stderr reminder that **Google's only revocation path is the Google account security panel** (the Foyer master-token API does not expose a programmatic revoke).
- **Token-cache directory.** `~/.config/nest-cli/.tokens/` chmod 0700, containing per-device session-state blobs treated as opaque to the CLI (FR-CRED-9 mirrors `tapo-cli`).
- **No telemetry, no analytics.** The CLI never makes outbound connections except to Google's documented endpoints. No Sentry, no Posthog, no version-check pings.
- **Scope minimization.** OAuth scopes requested are exactly `sdm.service` (always) plus, when the operator opts in to events, `pubsub` scopes. Asking for more is forbidden.
- **No credential file in $TMPDIR or world-readable paths.** Paths are operator-relative under `~/.config/nest-cli/`.

The threat model assumes a single-user host. Multi-user shared hosts are not a v1 target — operators on shared hosts SHALL run the CLI under their own user account.

---

## 5. Functional Requirements

Each FR is atomic and independently testable. Functional requirements are grouped by domain: shared infrastructure (FR-1 through FR-29), camera surface (FR-CAM-1 through FR-CAM-30), WiFi surface (FR-WIFI-1 through FR-WIFI-25), credentials (FR-CRED-1 through FR-CRED-15).

### 5.1 Discovery and listing (shared)

- **FR-1:** `nest-cli list` SHALL print every alias defined in the local config file with its resolved family (`cam` or `wifi`), target identifier (SDM device id for cameras, Google Wi-Fi ap id for points), and configured group memberships. By default, list does NOT issue a per-device probe — output reflects config-resolved data only.
- **FR-1a:** `nest-cli list --probe` SHALL additionally probe each device for liveness within `--timeout` and include an `online: bool` field. For cam targets, the probe is an SDM `devices.get` call. For wifi targets, the probe is a Foyer mesh-group fetch followed by an ap-online check.
- **FR-1b:** `nest-cli list --groups` SHALL print every group defined in config with its member alias list.
- **FR-1c:** `nest-cli list --family <cam|wifi>` SHALL filter output to the named family.
- **FR-1d:** `nest-cli list --online-only` SHALL imply `--probe` and filter the output to devices that responded.
- **FR-2:** `nest-cli discover` SHALL be a synonym for `list --probe --no-config` — it queries Google for the full set of devices the credentials grant access to, regardless of whether they are in the operator's config file. The output is the inventory; copying entries into config is a manual step.
- **FR-2a:** `nest-cli discover --family cam` SHALL emit only SDM-visible devices (cameras, doorbells, displays, thermostats — the CLI surfaces everything SDM returns and tags each by `type` field).
- **FR-2b:** `nest-cli discover --family wifi` SHALL emit only Foyer-visible Wi-Fi mesh groups, points, and access-points (REQUIRES `--experimental-wifi` per FR-WIFI-0).
- **FR-3:** Discovery zero-result with no error SHALL exit 0 with empty output (`[]` in `--json`/`--jsonl`) and emit a single INFO log line on stderr stating "no devices found." Exit code 3 (network error) SHALL be reserved for cases where the underlying API request fails (DNS, TLS, 5xx from Google).

### 5.2 Info (shared)

- **FR-4:** `nest-cli info <target>` SHALL issue a live call against the target's home API and print full state.
  - For a cam target: SDM `devices.get` on the device, parse the traits array, surface the §10.1 Camera record.
  - For a wifi target (point or whole group): Foyer-backed point-info or group-info, surface the §10.5 Wifi record.
- **FR-4a:** Info output in `--json` mode SHALL be a single JSON object whose key set matches the target's family record exactly, with stable key names across SDM/Foyer minor versions.
- **FR-4b:** `info <target>` against an unknown alias SHALL exit 4. Against an alias whose target id no longer exists at the home API (camera removed from the user's account; point factory-reset out of the mesh) SHALL exit 4 with a hint to run `discover`.

### 5.3 Camera surface (`cam`)

The `cam` group is the core of the v0.1 / v0.2 deliverable. All `cam` sub-verbs require successful OAuth setup (§6.2) and at least one cam target in config or a direct device-id argument.

#### 5.3.1 Cam list and info

- **FR-CAM-1:** `nest-cli cam list` SHALL be a synonym for `nest-cli list --family cam`.
- **FR-CAM-2:** `nest-cli cam info <target>` SHALL issue an SDM `devices.get` and emit the full Camera record per §10.1 including `traits`, `online`, `room_name`, `structure_name`, `battery_pct` (null if not battery-powered), `signal_strength` (null if not exposed), `firmware_version` (null if not exposed), `last_event_ts` (RFC 3339 UTC `Z`, null if no recent events).

#### 5.3.2 Snapshot

- **FR-CAM-3:** `nest-cli cam snapshot <target> --output <path>` SHALL write a JPEG still image to `<path>`.
- **FR-CAM-4:** Snapshot SHALL try mechanisms in this order, advancing on failure: (1) SDM `CameraImage.GenerateImage` if the trait is in the camera's `traits` array; (2) `CameraEventImage.GenerateImage` keyed off the most recent `eventId` from the past 60 seconds (queried via the Pub/Sub subscription's pull endpoint, opportunistic — no event in window means tier 2 is unavailable); (3) **deferred to a Phase 2+ ffmpeg-from-RTSP fallback** for 1st-gen RTSP cameras. v1 ships only tiers 1 and 2.
- **FR-CAM-4a:** Auth-rejection at any tier (HTTP 401 from SDM, refresh-token-expired) SHALL NOT advance to the next tier — exit 2 immediately with a structured error naming the credential.
- **FR-CAM-4b:** A camera with neither `CameraImage` nor `CameraEventImage` traits, AND no event in the 60-second window, SHALL exit 5 (unsupported feature) with a hint pointing at the `traits` array. This is a real failure mode for some 2nd-gen battery cameras between events.
- **FR-CAM-4c:** The mechanism that succeeded SHALL be reported in `--json` output as `{"mechanism": "camera_image"|"camera_event_image"}`. Observability, not contract.
- **FR-CAM-5:** `--output -` SHALL write the JPEG bytes to stdout. `--json` and `--jsonl` SHALL exit 64 with `--output -` as mutually exclusive.

#### 5.3.3 Stream

- **FR-CAM-6:** `nest-cli cam stream <target>` SHALL emit a Stream record on stdout describing the negotiated session.
- **FR-CAM-7:** For RTSP-protocol cameras, the Stream record SHALL include `protocol: "rtsp"`, `url` (full RTSP URL with embedded session token), `expires_at` (RFC 3339 UTC `Z`), `stream_token`, `extension_token`. The URL is directly usable by `ffmpeg`/`mpv`.
- **FR-CAM-8:** For WebRTC-protocol cameras, the Stream record SHALL include `protocol: "webrtc"`, `answer_sdp`, `media_session_id`, `expires_at`. Generating the offer SDP is the operator's responsibility in v1 (Decision 6).
- **FR-CAM-9:** WebRTC stream invocation SHALL require `--offer-sdp <path-or-stdin>`. Without it, `cam stream` against a WebRTC camera SHALL exit 64 with a hint pointing at FR-CAM-8 and §3.1.2.
- **FR-CAM-10:** `--offer-sdp -` SHALL read the offer SDP from stdin.
- **FR-CAM-11:** `cam stream` SHALL NOT decode, transcode, or proxy video. It is a session-metadata emitter.
- **FR-CAM-12:** `--quiet` SHALL suppress stdout but the exit code still indicates negotiation success/failure. When `--quiet` is paired with `cam stream`, the operator gets exit-code-only feedback; this is uncommon but documented.

#### 5.3.4 Stream extension and stop

- **FR-CAM-13:** `nest-cli cam stream-extend <target> --extension-token <tok>` SHALL call SDM's stream-extend method to refresh an active session. Returns the updated Stream record (new `expires_at`, new `extension_token`).
- **FR-CAM-14:** `nest-cli cam stream-stop <target> --extension-token <tok>` SHALL call SDM's stream-stop method to invalidate an active session. Exits 0 on success.

#### 5.3.5 Doorbell chime

- **FR-CAM-15:** `nest-cli cam chime <target>` SHALL invoke `DoorbellChime.Chime` if the trait is present. Doorbell-only.
- **FR-CAM-16:** Cameras without the `DoorbellChime` trait SHALL exit 5 (unsupported feature) with a hint listing the cameras in the operator's config that DO support the trait.

#### 5.3.6 Talkback (NOT IN v1)

- **FR-CAM-17:** Talkback (two-way audio) is **out of scope at all phases of this SRD** because SDM does not expose a talkback verb on any camera or doorbell. `nest-cli cam talk` SHALL NOT be a registered verb in v1; if added in a future version contingent on SDM support, it gets its own FR block.

#### 5.3.7 Quiet hours, arming, disarming (NOT IN v1)

- **FR-CAM-18:** Quiet-hours, arming, and disarming are **out of scope at all phases** because SDM does not expose these. `nest-cli cam arm` / `cam disarm` / `cam quiet-hours` SHALL NOT be registered verbs. §17 tracks the open question for revisit if Google adds the trait surface.

#### 5.3.8 Events

- **FR-CAM-19:** `nest-cli cam events [<target>]` SHALL pull pending events from the operator's Pub/Sub subscription and emit each as JSONL on stdout per §10.3. Without a target, events from all cameras are emitted; with a target, only events tagged for that camera.
- **FR-CAM-20:** Without `--follow`, `cam events` SHALL drain the subscription's currently-pending messages once and exit 0. The default `--max-messages` is 100; the default per-pull deadline is 5 seconds.
- **FR-CAM-21:** `cam events --follow` SHALL stream-pull continuously, emitting each event as it arrives, until SIGINT or SIGTERM. On signal: cease pulling, ack any in-flight messages already returned to the CLI, emit a final JSONL summary line `{"event":"interrupted","received":N}` to stdout, exit 130 (SIGINT) or 143 (SIGTERM).
- **FR-CAM-22:** `--types <comma-list>` SHALL filter events to a comma-separated subset of `motion`, `person`, `package`, `sound`, `doorbell-press`, `unknown`. Default is no filter (all types emitted).
- **FR-CAM-23:** Auto-reconnect on transport error: capped exponential backoff `1s → 2s → 4s → 8s → 16s → 32s → 32s`. Five consecutive failures exit 3 (network) with a structured error naming the last failure. A successful pull resets the counter.
- **FR-CAM-24:** Event payloads SHALL be normalized to the §10.3 Event record: `{ts, target, event_type, has_image, image_eligibility_window_s, room, structure, source: "pubsub"}`. `ts` is RFC 3339 UTC `Z`. `has_image` is true if the event includes an `eventId` usable for `CameraEventImage.GenerateImage`. `image_eligibility_window_s` is the seconds remaining in the event's image-fetch window (typically 30s). `source: "pubsub"` is constant.
- **FR-CAM-25:** Pub/Sub subscription not configured for the operator's project SHALL exit 6 (config error) with a hint pointing at `auth setup --pubsub` (Phase 2+; v0.1 / v0.2 ships without this verb and exits 6 with a manual-setup link).

#### 5.3.9 Battery and signal status

- **FR-CAM-26:** `cam battery <target>` SHALL emit the `battery_pct` and last-charged metadata for battery-powered cameras. Cameras without a battery trait SHALL exit 5 with the device name and `is_battery_powered: false` in the structured error.
- **FR-CAM-27:** `cam signal <target>` SHALL emit the camera's signal-strength (RSSI in dBm if exposed) and last-online timestamp. Cameras without a signal-strength surface SHALL exit 5.

#### 5.3.10 Cam capabilities query

- **FR-CAM-28:** `cam capabilities <target>` SHALL emit the camera's `traits` array as JSON, plus a derived `supported_verbs` field listing which `nest-cli cam` sub-verbs are supported on the device. Useful for scripts that want to skip unsupported verbs without trial-and-error.

### 5.4 WiFi surface (`wifi`, experimental)

The `wifi` group is the Phase 3 deliverable. **Every `wifi` sub-verb requires `--experimental-wifi` on each invocation.** Without it, the verb exits 64 with a hint pointing at §3.2.3 and FR-WIFI-0. The flag's purpose is to prevent operators from depending on this surface in long-lived scripts; every script that uses it must explicitly opt in to firmware-fragility risk.

#### 5.4.1 Experimental gate

- **FR-WIFI-0:** Every `wifi` sub-verb SHALL require the `--experimental-wifi` flag on each invocation. Without it, the verb SHALL exit 64 with a hint pointing at §3.2.3. The flag SHALL NOT be settable in config (no `[defaults] experimental_wifi = true`); it must be explicit per-invocation. Rationale: the flag's friction is the feature.

#### 5.4.2 List

- **FR-WIFI-1:** `nest-cli wifi list groups --experimental-wifi` SHALL emit every Wi-Fi mesh group the operator's Google account owns. Each group is a §10.6 WifiGroup record.
- **FR-WIFI-2:** `nest-cli wifi list points <group> --experimental-wifi` SHALL emit every router/point in the named group. Each point is a §10.7 WifiPoint record with `id`, `name`, `is_master`, `model`, `firmware_version`, `mesh_role`, `signal_strength_to_upstream_dbm`, `connected_clients_count`, `online`, `uptime_s`.
- **FR-WIFI-3:** `nest-cli wifi list clients <group> --experimental-wifi` SHALL emit every connected client across the named group. Each client is a §10.8 WifiClient record with `id`, `friendly_name`, `mac` (where available), `ip`, `connected_to_point_id`, `connection_type` (`wifi`|`ethernet`), `band` (`2.4`|`5`|`6`|null), `tx_rate_mbps`, `rx_rate_mbps`, `paused`, `priority_until` (RFC 3339 UTC or null), `group_assignment` (`family`|`parental`|`guest`|null).

#### 5.4.3 Per-client controls

- **FR-WIFI-4:** `nest-cli wifi pause <client-id> --experimental-wifi` SHALL call Foyer's pause-station endpoint via `googlewifi`. Idempotent — pausing an already-paused client returns OK with no error.
- **FR-WIFI-5:** `nest-cli wifi unpause <client-id> --experimental-wifi` SHALL unpause. Idempotent.
- **FR-WIFI-6:** `nest-cli wifi prioritize <client-id> --duration <minutes> --experimental-wifi` SHALL prioritize the client for the given duration (Google Wi-Fi's "boost" feature). Default duration is 60 minutes. Min 1, max 240 (Foyer-imposed).
- **FR-WIFI-7:** `nest-cli wifi group-assign <client-id> --group <family|parental|guest> --experimental-wifi` SHALL set the client's group assignment. Use `--group none` to remove. The mapping of `family`/`parental`/`guest` to Foyer's internal group ids is library-handled.

#### 5.4.4 Speed test

- **FR-WIFI-8:** `nest-cli wifi speedtest run --experimental-wifi` SHALL trigger a fresh speed test on the master router. The verb SHALL block until the test completes (typically 30-90 seconds) with a `--timeout` default of 180. Output is a §10.9 SpeedTest record: `{ts, group_id, point_id, download_mbps, upload_mbps, ping_ms, source: "router"}`.
- **FR-WIFI-9:** `nest-cli wifi speedtest history --limit N --experimental-wifi` SHALL emit recent speed-test results stored on the router. Default limit 30; max 365 (Foyer-imposed). Sorted descending by `ts`.

#### 5.4.5 Reboot

- **FR-WIFI-10:** `nest-cli wifi reboot point <point-id> --experimental-wifi` SHALL reboot a single point. In tty mode, prompts on stderr ("Reboot <name>? [y/N] ") and aborts on no/empty. In non-tty mode, requires `--yes`.
- **FR-WIFI-11:** `nest-cli wifi reboot group <group-id> --experimental-wifi` SHALL reboot every point in the group. Same confirmation rules as FR-WIFI-10. The verb prompts ONCE for the entire group, names the resolved point list on stderr, and proceeds without per-point prompts.
- **FR-WIFI-12:** `--quiet` SHALL imply `--yes` for reboot verbs (mirroring `tapo-cli` FR-38).

#### 5.4.6 Network info

- **FR-WIFI-13:** `nest-cli wifi network <group-id> --experimental-wifi` SHALL emit the §10.10 WifiNetwork record: `{group_id, ssid, guest_ssid (null if disabled), guest_enabled, ipv4: {wan, lan_subnet, dhcp_range_start, dhcp_range_end}, ipv6: {wan, prefix_len, enabled}, dns_servers: [...]}`.
- **FR-WIFI-14:** `nest-cli wifi guest enable <group-id> --experimental-wifi` and `wifi guest disable <group-id> --experimental-wifi` SHALL toggle the guest network. Setting the guest SSID's password from the CLI is **out of scope** in v1 (Decision 9).

#### 5.4.7 Point health

- **FR-WIFI-15:** `nest-cli wifi point-health <point-id> --experimental-wifi` SHALL emit §10.11 WifiPointHealth: `{id, online, uptime_s, signal_to_upstream_dbm, connected_clients_count, mesh_role}`.

### 5.5 Auth (shared)

The `auth` group manages OAuth (cam) and Foyer master-token (wifi) credentials.

#### 5.5.1 Cam OAuth

- **FR-CRED-1:** `nest-cli auth setup` SHALL walk the operator through OAuth setup interactively: prompt for Google Cloud project id, OAuth client id, OAuth client secret, then open a local-callback HTTP listener (default `127.0.0.1:8765`, override via `--callback-port`), print the consent URL on stderr, wait for callback completion, and persist the resulting refresh token + access token + expiry to `~/.config/nest-cli/credentials-cam.json` chmod 0600.
- **FR-CRED-2:** `auth setup` SHALL refuse to overwrite an existing credentials file unless `--overwrite` is passed. Without `--overwrite` against an existing file, exit 2 with a hint to either `auth revoke` or `auth setup --overwrite`.
- **FR-CRED-3:** The cam credentials file format SHALL be JSON: `{"version": 1, "type": "oauth", "google_cloud_project_id": "<id>", "oauth_client_id": "<id>", "oauth_client_secret": "<secret>", "refresh_token": "<token>", "access_token": "<token>", "expires_at": "<rfc3339>"}`. Unknown additional keys SHALL cause a config-validation error and exit 6.
- **FR-CRED-4:** `nest-cli auth refresh` SHALL force-refresh the access token using the stored refresh token. Useful for testing and debugging.
- **FR-CRED-5:** `nest-cli auth revoke` SHALL call `https://oauth2.googleapis.com/revoke` against the stored refresh token, then atomically replace the credentials file with an empty stub. After this, all `cam` verbs exit 2 until `auth setup` is rerun.
- **FR-CRED-6:** Access-token rotation SHALL be automatic on every cam command — if the cached token's `expires_at` is within 60 seconds of now (or in the past), the CLI SHALL refresh before issuing the request, write the new token back to the credentials file atomically (`write tmpfile + fsync + rename`), and proceed.

#### 5.5.2 WiFi master token

- **FR-CRED-7:** `nest-cli auth wifi-setup --experimental-wifi` SHALL accept a Google account email and an Android master token (provided via stdin, `--master-token-file`, or `GOOGLE_ANDROID_MASTER_TOKEN` env var) and persist a derived Foyer-usable master token to `~/.config/nest-cli/credentials-wifi.json` chmod 0600. The verb SHALL document the bootstrap step (extracting the Android master token from a paired device) in `--help` and stderr.
- **FR-CRED-8:** The wifi credentials file format SHALL be JSON: `{"version": 1, "type": "foyer", "google_account_email": "<email>", "master_token": "<token>", "issued_at": "<rfc3339>"}`. Unknown additional keys SHALL cause a config-validation error.
- **FR-CRED-9:** `nest-cli auth wifi-revoke --experimental-wifi` SHALL atomically replace the wifi credentials file with an empty stub AND emit a stderr reminder that the only programmatic Google-side revocation path is the Google account security panel (`myaccount.google.com/permissions`). The Foyer service does not expose a token-invalidate endpoint.

#### 5.5.3 Auth status

- **FR-CRED-10:** `nest-cli auth status` SHALL emit, for each configured credential type, a record with `family` (`cam`|`wifi`), `configured` (bool), `expires_at` (cam only — RFC 3339 UTC `Z` or null), `issued_at` (wifi only), `google_account_email` (where derivable), and `last_refresh_ts` (cam only). Output is a JSON array in `--json` mode. `--no-probe` skips any live token-validity check (default does NOT probe — token validity is checked only on the next operational verb invocation).
- **FR-CRED-11:** `auth status --probe` SHALL additionally validate the cam token by issuing a no-op SDM call and the wifi token by issuing a Foyer ping. Probe results SHALL include a `probe_status: "ok"|"failed"|"skipped"` field and a `probe_error` field where applicable.
- **FR-CRED-12:** Both credential files SHALL be subject to chmod-0600 enforcement (mirrors `tapo-cli` FR-CRED-2). A file with mode more permissive than 0600 SHALL exit 2 with the current mode in the error message.
- **FR-CRED-13:** Concurrent invocations writing to the same credentials file (e.g., two parallel `cam` invocations both refreshing the access token) SHALL serialize via `flock` on a per-file lock token. Lock-acquisition timeout = `--timeout` seconds (default 30 for OAuth refresh due to Google's occasional latency); timeout exits 3 (network) per `tapo-cli` FR-CRED-13 precedent.
- **FR-CRED-14:** `--credential-source <env|file|none>` flag SHALL constrain credential resolution per-invocation (mirrors `tapo-cli` FR-CRED-15):
  - `env` — only `NEST_CLI_OAUTH_REFRESH_TOKEN` + `NEST_CLI_OAUTH_CLIENT_ID` + `NEST_CLI_OAUTH_CLIENT_SECRET` + `NEST_CLI_GCP_PROJECT_ID` (cam); `NEST_CLI_FOYER_MASTER_TOKEN` + `NEST_CLI_GOOGLE_ACCOUNT_EMAIL` (wifi).
  - `file` — only the persisted credentials files. Skip env vars.
  - `none` — skip all sources; commands requiring credentials exit 2.
- **FR-CRED-15:** Partial env-var fall-through: if not all required env vars for a family are set, the resolver treats env as "not set" and falls through to file. Verbose mode (`-v`) SHALL log the partial-set as a single WARN line on stderr.

### 5.6 Groups

- **FR-5:** Groups SHALL be defined locally in the CLI config file's `[groups]` table, NOT on the devices themselves. Groups MAY be cross-family — a group can contain both cam and wifi aliases — but verbs that fan out a group SHALL skip aliases of the wrong family with a per-target exit-5 record.
- **FR-6:** A group target (`@group-name` or `--group group-name`) SHALL resolve to its member aliases at command execution time.
- **FR-7:** Group operations SHALL execute device commands in parallel up to a configurable concurrency limit (default 3 — lower than `tapo-cli`'s 5 because Google's APIs rate-limit harder; per-command override via `--concurrency N`).
- **FR-8:** Group operations SHALL report per-device success/failure individually; a single device failure SHALL NOT abort the group operation.
- **FR-8a:** Group exit code SHALL be:
  - **0** if every sub-operation succeeded.
  - **7** (partial failure) if at least one sub-operation succeeded AND at least one failed.
  - When **all** sub-operations failed, the exit code SHALL be the failure code of the sub-operation whose target appears first in the resolved alias list (the alias-config-file ordering of the group's members) — NOT the execution-completion order. Mirrors `tapo-cli` FR-43a.
- **FR-8b:** v1 SHALL NOT support `groups add` / `groups remove` sub-verbs. `nest-cli list --groups` is the only group sub-verb in v1; mutations are by hand-editing the config.
- **FR-8c:** `cam stream` SHALL refuse group targets and exit 64. Streaming against multiple cameras simultaneously is a footgun; per-camera invocation only.
- **FR-8d:** `cam events --follow` SHALL refuse group targets and exit 64 — one subscription per stdout. (`cam events` without `--follow` MAY accept a group target and fan out.)
- **FR-8e:** Fan-out verbs SHALL emit one JSONL record per resolved member in resolved-alias-list order, with the standard envelope `{target, status, exit_code, result?, error?}` per FR-44a.

### 5.7 Batch

- **FR-9:** `nest-cli batch --file <path>` SHALL read newline-delimited commands from a file and execute them, emitting one JSONL result per line on stdout.
- **FR-9a:** Each emitted line SHALL conform to:
  ```json
  {
    "command": "<verb-and-flags-string>",
    "target": "<resolved-alias-or-id>",
    "status": "ok" | "error",
    "exit_code": <int>,
    "result": <verb's normal JSON payload, present iff status == "ok">,
    "error": {
      "code": "<error-enum-from-§11.2>",
      "message": "<human-readable>",
      "hint": "<optional actionable hint>"
    }
  }
  ```
  Mirrors `tapo-cli` FR-44a exactly.
- **FR-10:** `nest-cli batch --stdin` SHALL accept the same format from stdin.
- **FR-10a:** Batch exit code semantics SHALL match FR-8a (0 / 7 / first-failure-code).
- **FR-10b:** Empty-input batch SHALL exit 0 with no stdout output. Blank lines SHALL be skipped silently. Lines beginning with `#` SHALL be treated as comments.
- **FR-10c:** On SIGINT or SIGTERM during batch execution, the CLI SHALL: (1) cease dispatching new sub-operations, (2) wait up to 2 seconds for in-flight sub-operations to complete and have their results emitted, (3) emit a final JSONL summary line `{"event":"interrupted","completed":N,"pending":M}` to stdout, (4) exit 130 (SIGINT) or 143 (SIGTERM).

### 5.8 Output formats

- **FR-11:** Default output SHALL be human-readable text on a tty, JSONL otherwise (any non-tty stdout — pipes, redirects, command substitutions). Mirrors `tapo-cli` FR-46.
- **FR-12:** `--json` SHALL force pretty JSON output regardless of tty detection.
- **FR-13:** `--jsonl` SHALL force one-JSON-per-line output regardless of tty detection.
- **FR-14:** `--quiet` SHALL suppress all stdout output; only the exit code communicates result.
- **FR-15:** In `--json` and `--jsonl` modes, on **any** non-zero exit, stdout SHALL be valid parseable JSON or empty. The CLI SHALL never emit malformed JSON. For batch and group operations with mixed results, stdout JSONL SHALL contain one result object per attempted operation including those that failed (each with its own `error` field per §11.2). Stderr SHALL emit the structured summary error per §11.2 once.

### 5.9 Configuration resolution

- **FR-16:** Config file resolution order: (1) `--config <path>` flag if present, (2) `NEST_CLI_CONFIG` env var if set and non-empty, (3) `~/.config/nest-cli/config.toml` if it exists.
- **FR-16a:** If `--config` or `NEST_CLI_CONFIG` is set and the referenced file does not exist or cannot be read, the CLI SHALL exit 6 (config error). Silent fallback is forbidden.
- **FR-16b:** If only the default location is consulted and it does not exist, the CLI SHALL operate with built-in defaults and emit a single INFO log line on stderr. This SHALL NOT be an error.
- **FR-16c:** `nest-cli config show` SHALL print the effective resolved config in TOML format. `nest-cli config validate [<path>]` SHALL load and validate a config file and exit 0/6.
- **FR-16d:** `config show` output SHALL redact secrets — OAuth client secret, refresh token, master token — to `***`. There is no `--show-secrets` flag in v1.

### 5.10 Error handling

- **FR-17:** Network errors (DNS, TLS, 5xx from Google, gRPC unavailable, Foyer rotation) SHALL exit 3 with a structured stderr error.
- **FR-18:** Authentication failures (refresh token revoked, 401 on access token after refresh, master token rejected) SHALL exit 2 with a credential-source hint.
- **FR-19:** Unknown alias / unresolved target SHALL exit 4.
- **FR-20:** Unsupported feature on the target hardware (CameraImage trait absent on a 2nd-gen WebRTC camera between events; talkback verb on any camera; quiet-hours verb on any camera) SHALL exit 5.
- **FR-21:** Verbose mode (`-v`, `-vv`) SHALL emit progressively detailed JSON-structured logs to stderr; stdout SHALL remain clean. `-vv` SHALL include raw SDM/Foyer envelopes with credentials redacted.

### 5.11 Determinism

- **FR-22:** All emitted timestamps SHALL be RFC 3339 UTC with the literal `Z` suffix.
- **FR-23:** Multi-record output SHALL be sorted deterministically — by `target` ascending in resolved-config order with ties broken by event timestamp ascending. Single-target streams (events) sort by `ts` ascending.
- **FR-24:** Numeric fields SHALL be JSON numbers, not strings.
- **FR-25:** Identical input SHALL produce identical output structure (JSON key set is stable).

### 5.12 Verbose logging and observability

- **FR-26:** `-v` SHALL emit single-line JSON INFO logs to stderr.
- **FR-27:** `-vv` SHALL emit single-line JSON DEBUG logs including raw protocol envelopes (with credentials, tokens, and event ids redacted to `***`).
- **FR-28:** Optional file logging: when `[logging] file = "<path>"` is set in config, JSON log lines SHALL be tee'd there (append, line-buffered). No rotation in v1.
- **FR-29:** All log lines SHALL include `family` field (`cam`|`wifi`|`shared`) for filterability with `jq`.

---

## 6. Authentication and Credentials

### 6.1 Two distinct auth flows

The CLI maintains **two separate credential files** because the cam and wifi flows have nothing in common operationally:

- `~/.config/nest-cli/credentials-cam.json` — OAuth refresh token + access token + GCP project metadata for SDM. Created by `auth setup`.
- `~/.config/nest-cli/credentials-wifi.json` — Foyer master token + Google account email. Created by `auth wifi-setup --experimental-wifi`.

Both files chmod 0600. Both subject to atomic-rename writes. Neither references the other. An operator who only uses cam features never has the wifi file on disk; an operator who only uses wifi (rare — most operators use both) never has the cam file. `auth status` shows both side-by-side regardless.

### 6.2 Cam OAuth setup (interactive)

The `auth setup` flow:

1. Operator has already (out of band) created a Google Cloud project, enabled the Smart Device Management API, registered an application in Device Access Console (paying $5 USD one-time), and downloaded an OAuth Desktop client JSON from `console.cloud.google.com/apis/credentials`. The CLI prompts the operator to confirm these prerequisites are done; it does NOT automate them.
2. Operator runs `nest-cli auth setup`.
3. CLI prompts for: Google Cloud project id (required), OAuth client id (required), OAuth client secret (required, prompted via getpass to avoid shell history). Optionally accepts `--client-secrets <path>` to read all three from a downloaded JSON.
4. CLI starts a local HTTP listener on `127.0.0.1:8765` (override via `--callback-port`).
5. CLI prints the consent URL on stderr and tells the operator to open it in a browser. The URL requests scope `https://www.googleapis.com/auth/sdm.service`. (Pub/Sub scope is added in Phase 2.)
6. Operator completes consent in the browser. Google redirects to the local listener with an authorization code.
7. CLI exchanges the code for a refresh token + access token, persists them to `credentials-cam.json` chmod 0600.
8. CLI confirms success on stderr and exits 0.

In `--non-interactive` mode (e.g., headless CI), `auth setup` SHALL accept all required inputs via flags or stdin and skip the browser-open prompt — Google's OAuth still requires a human to consent, but the CLI SHALL print the consent URL and accept the code via `--auth-code <code>` post-consent.

### 6.3 Wifi master-token setup (manual bootstrap)

Foyer's auth model has no OAuth flow. The master token is bootstrapped via the Android `Auth` library impersonation, which requires the operator to first extract an Android master token from a Google-account-paired Android device. The community standard is to use a one-time Android tool (such as the `aiohomekit-google-companion` bootstrap script, or the `gpsoauth` tool) to extract the token. **The CLI does NOT perform the Android-side extraction.** It accepts a pre-extracted token.

The flow:

1. Operator has already (out of band) extracted an Android master token from a paired device. The CLI's `auth wifi-setup --help` text names the community references and links to the up-to-date extraction guide.
2. Operator runs `nest-cli auth wifi-setup --experimental-wifi`.
3. CLI prompts for Google account email (required) and the Android master token (via stdin, `--master-token-file <path>`, or `GOOGLE_ANDROID_MASTER_TOKEN` env var; getpass on stdin to avoid shell history).
4. CLI calls `glocaltokens` to derive a Foyer-usable master token from the Android master token + email pair.
5. CLI persists the result to `credentials-wifi.json` chmod 0600.
6. CLI confirms success and exits 0.

`auth wifi-revoke --experimental-wifi` scrubs the file and exits 0 with a stderr reminder of the §6.4 revocation reality.

### 6.4 Revocation

**Cam (OAuth) revocation is clean.** `auth revoke` calls `https://oauth2.googleapis.com/revoke?token=<refresh_token>`, which Google honors immediately. The credentials file is then scrubbed.

**Wifi (Foyer) revocation is NOT programmatic.** Foyer does not expose a token-invalidate endpoint. The only ways to revoke a Foyer master token are: (a) revoke the entire Google account session via `myaccount.google.com/permissions`, which logs out every paired Android device too (high blast radius); (b) change the Google account password, which invalidates all derived tokens (also high blast radius). The CLI's `auth wifi-revoke` only scrubs the local file and emits a stderr reminder of (a) and (b). Operators are expected to consider this carefully.

### 6.5 Token caching and refresh

Cam access-token cache lives at `credentials-cam.json` itself — the access token is rewritten on every refresh. The CLI honors the `expires_at` field; refreshes proactively when within 60s of expiry; falls through to a fresh refresh on a 401 mid-request (single retry, then exit 2).

Wifi master tokens are long-lived (no documented expiry from Google). The CLI caches per-device local tokens derived from the master token in `~/.config/nest-cli/.tokens/wifi-<device-id>.json` chmod 0600, with a directory mode of 0700. These local tokens have shorter (typically 1-week) TTLs; expiry triggers a fresh derivation from the master token with no operator interaction.

### 6.6 Per-credential locking

Concurrent CLI invocations writing the same credentials file (e.g., two parallel cam refreshes) SHALL serialize via `flock` on a per-file lock token. Atomic rename used for all writes. Lock acquisition timeout `--timeout` seconds; default 30 for OAuth (longer than the standard 5 because Google's token endpoint has occasional 5+ second latency).

### 6.7 `auth status` redaction

`auth status --json` output SHALL include `expires_at`, `issued_at`, `google_account_email`, `last_refresh_ts`, `family`, `configured`, `probe_status`, `probe_error`. It SHALL NOT include the refresh token, the master token, the access token, or the OAuth client secret. There is no `--show-tokens` flag.

---

## 7. Non-Functional Requirements

### 7.1 Performance

Targets assume a wired internet connection or 5GHz Wi-Fi to the operator's host. Google's APIs vary in latency; targets are p95 floors, not contracts on degraded connections.

| Metric | Target |
|--------|--------|
| `cam list` (cached refresh token, fresh access token) | < 1500ms p95 |
| `cam list` (cached refresh token, expired access token, refresh required) | < 3000ms p95 |
| `cam info <target>` (cached) | < 1500ms p95 |
| `cam snapshot` via `CameraImage` | < 5000ms p95 |
| `cam snapshot` via `CameraEventImage` (event in window) | < 4000ms p95 |
| `cam stream` (RTSP, URL emission) | < 2000ms p95 |
| `cam stream` (WebRTC, offer-SDP exchange) | < 3500ms p95 |
| `cam events` (one-shot, drains <=100 messages) | < 4000ms p95 |
| `wifi list groups --experimental-wifi` (cached) | < 2500ms p95 |
| `wifi pause <client> --experimental-wifi` (cached) | < 2000ms p95 |
| `wifi speedtest run --experimental-wifi` | < 90s typical, < 180s ceiling |
| Cold CLI startup (`--help`) | < 250ms |

### 7.2 Determinism

- All emitted timestamps RFC 3339 UTC `Z`. No local time, no offsets other than `Z`.
- Multi-record output sorted by `target` ascending in resolved-config order with secondary timestamp tiebreak.
- Numeric fields are JSON numbers, never strings.
- Identical input produces identical output structure.
- No interactive prompts in non-tty mode (except `auth setup` whose interactivity is the entire point — and even there, `--non-interactive` exists).

### 7.3 Retry and rate-limit posture

Both Google APIs ratelimit. SDM's documented quota is 60 requests per minute per developer project; Foyer is undocumented but observed to throttle aggressively at ~1 req/sec per master token.

- The CLI SHALL respect HTTP 429 (Too Many Requests) on SDM responses by sleeping `Retry-After` seconds (or 30 if header absent), retrying up to 3 times, then exiting 3.
- For Foyer 429-equivalents (gRPC `RESOURCE_EXHAUSTED`), same posture.
- The CLI SHALL NOT preemptively rate-limit — it SHALL issue requests as fast as the operator scripts them, and respect server-side throttling on response. Exception: in batch and group fan-out mode, the per-family concurrency cap (default 3) is the only client-side throttle.
- Retries are silent at default verbosity; `-v` logs each retry attempt as a single INFO line.

### 7.4 Timeouts

| Verb / Phase | Default timeout |
|--------------|-----------------|
| Most cam verbs | 10s |
| `cam events --follow` per pull | 30s (long-poll) |
| `cam snapshot` total | 10s |
| `cam stream` total | 10s |
| `wifi speedtest run` | 180s |
| Most wifi verbs | 10s |
| OAuth token refresh | 30s (Google occasionally slow) |
| Foyer master-token derivation | 15s |

`--timeout <seconds>` overrides per-invocation. Verbs with sub-budgets (e.g., `cam snapshot` tiering across `CameraImage` and `CameraEventImage`) document their split in `--help`.

### 7.5 Network model

- All operations require outbound HTTPS to Google's APIs. Documented endpoints: `smartdevicemanagement.googleapis.com`, `oauth2.googleapis.com`, `accounts.google.com`, `pubsub.googleapis.com`, `googlehomefoyer-pa.googleapis.com` (wifi, gRPC).
- DNS resolution failure for any of the above SHALL exit 3 (network) with the failed hostname in the error.
- The CLI SHALL NOT make outbound connections to any non-Google endpoint. No telemetry, no analytics, no version-check pings.
- Corporate proxy support: the CLI SHALL honor `HTTPS_PROXY` / `https_proxy` environment variables for cam-side HTTPS calls. Foyer gRPC honors `grpc_proxy` per the gRPC library's standard behavior.

### 7.6 Portability

- macOS 13+ (Apple Silicon and Intel).
- Linux x86_64 and arm64.
- Python 3.11+ required (matches `google-cloud-pubsub` minimum).
- No Windows support in v1.
- ffmpeg NOT required in v1 (held in reserve for Phase 2+ snapshot fallback).

---

## 8. CLI Surface

### 8.1 Verb summary

| Verb | Purpose |
|------|---------|
| `discover` | Live API call to enumerate everything the credentials grant access to |
| `list` | Print configured aliases and groups (config-resolved unless `--probe`) |
| `info` | Show full state of one target (auto-detects family) |
| `cam list` | Synonym for `list --family cam` |
| `cam info <t>` | Camera detail |
| `cam snapshot <t>` | Pull JPEG to file |
| `cam stream <t>` | Emit Stream record (RTSP URL or WebRTC session metadata) |
| `cam stream-extend <t>` | Extend an active stream session |
| `cam stream-stop <t>` | Stop an active stream session |
| `cam chime <t>` | Fire doorbell chime (doorbells only) |
| `cam events [<t>]` | Pull pending events; `--follow` for streaming |
| `cam battery <t>` | Battery state for battery-powered cams |
| `cam signal <t>` | Signal strength |
| `cam capabilities <t>` | Traits + supported_verbs |
| `wifi list groups` | Enumerate Wi-Fi mesh groups (experimental) |
| `wifi list points <g>` | Enumerate points in a group |
| `wifi list clients <g>` | Enumerate connected clients |
| `wifi pause <c>` | Pause client |
| `wifi unpause <c>` | Unpause client |
| `wifi prioritize <c>` | Boost client for N minutes |
| `wifi group-assign <c>` | Assign client to family/parental/guest |
| `wifi speedtest run` | Trigger fresh speedtest |
| `wifi speedtest history` | List recent results |
| `wifi reboot point <p>` | Reboot single point |
| `wifi reboot group <g>` | Reboot whole mesh |
| `wifi network <g>` | Network info (SSID, guest, IPv4/IPv6) |
| `wifi guest enable\|disable <g>` | Toggle guest network |
| `wifi point-health <p>` | Per-point health snapshot |
| `groups` | List local group definitions |
| `batch` | Execute commands from file or stdin |
| `config` | `show` (effective config), `validate` (lint) |
| `auth setup` | Interactive cam OAuth setup |
| `auth refresh` | Force-refresh cam access token |
| `auth revoke` | Revoke cam OAuth at Google + scrub local |
| `auth wifi-setup` | Interactive wifi master-token setup (experimental) |
| `auth wifi-revoke` | Scrub wifi credentials (experimental) |
| `auth status` | Both credential states side-by-side |

### 8.2 Target syntax

A target is one of:

- An **alias** defined in config (e.g., `front-door`)
- An **SDM device id** (e.g., `enterprises/<proj>/devices/<id>`) — full path; the CLI accepts the bare id portion for convenience
- A **Google Wi-Fi ap id** (e.g., `<group-id>` or `<point-id>` from Foyer)
- A **group name** prefixed with `@` (e.g., `@perimeter-cams`)
- The literal `all-cam` to target every cam alias
- The literal `all-wifi` to target every wifi alias

Group targets are forbidden for `cam stream`, `cam stream-extend`, `cam stream-stop`, and `cam events --follow` (FR-8c, FR-8d).

### 8.3 Common flags

| Flag | Meaning |
|------|---------|
| `--json` | Pretty JSON output |
| `--jsonl` | Newline-delimited JSON output |
| `--quiet` | Suppress stdout |
| `--timeout <s>` | Per-operation timeout, default per §7.4 |
| `--config <path>` | Use a non-default config file |
| `--credential-source <env\|file\|none>` | Constrain credential sources (FR-CRED-14) |
| `--concurrency N` | Override `[defaults] concurrency` for this invocation |
| `--probe` | On `list`, additionally probe each device |
| `--online-only` | On `list`, imply `--probe` and filter to online |
| `--family <cam\|wifi>` | On `list` / `discover`, filter |
| `--experimental-wifi` | Required on every `wifi` sub-verb |
| `-v`, `-vv` | Verbose / very verbose stderr logging |
| `--yes` | Bypass interactive confirmation |

### 8.4 Worked examples

```text
# First-time cam setup
$ nest-cli auth setup
[interactive prompts; opens browser; persists credentials-cam.json]

# What can I see?
$ nest-cli discover --family cam --json
[
  { "alias": null, "device_id": "...", "name": "Front Door", "type": "DOORBELL", ... },
  { "alias": null, "device_id": "...", "name": "Backyard", "type": "CAMERA", ... }
]

# Configure aliases by editing ~/.config/nest-cli/config.toml, then:
$ nest-cli list
front-door     DOORBELL    online
backyard       CAMERA      online

$ nest-cli cam info front-door --json
{
  "alias": "front-door",
  "device_id": "enterprises/.../devices/...",
  "name": "Front Door",
  "type": "DOORBELL",
  "online": true,
  "battery_pct": 87,
  "signal_strength": -54,
  "traits": ["...", "...DoorbellChime", "...CameraEventImage", "..."],
  "room_name": "Entryway",
  "structure_name": "Home",
  "last_event_ts": "2026-04-30T11:42:05Z"
}

# Snapshot from a battery doorbell (CameraImage absent; fallback to CameraEventImage)
$ nest-cli cam snapshot front-door --output /tmp/door.jpg --json
{"target":"front-door","output":"/tmp/door.jpg","mechanism":"camera_event_image","bytes":52380}

# Stream from a 1st-gen RTSP camera (Nest IQ in office)
$ nest-cli cam stream office --json
{
  "target": "office",
  "protocol": "rtsp",
  "url": "rtsps://stream.example.com:443/...?auth=...",
  "expires_at": "2026-04-30T12:30:00Z",
  "stream_token": "...",
  "extension_token": "..."
}

# Stream from a 2nd-gen WebRTC camera — operator must supply offer SDP
$ ./generate-offer-sdp.sh > /tmp/offer.sdp
$ nest-cli cam stream backyard --offer-sdp /tmp/offer.sdp --json
{
  "target": "backyard",
  "protocol": "webrtc",
  "answer_sdp": "v=0\\r\\no=- ...",
  "media_session_id": "...",
  "expires_at": "2026-04-30T12:35:00Z"
}

# Fire the doorbell chime
$ nest-cli cam chime front-door
{"target":"front-door","chimed":true}

# Watch events live
$ nest-cli cam events --follow --types motion,doorbell-press --jsonl
{"ts":"2026-04-30T11:50:01Z","target":"front-door","event_type":"doorbell-press","has_image":true,"image_eligibility_window_s":30,"room":"Entryway","structure":"Home","source":"pubsub"}
{"ts":"2026-04-30T11:51:14Z","target":"backyard","event_type":"motion","has_image":true,"image_eligibility_window_s":30,"room":"Patio","structure":"Home","source":"pubsub"}
^C
{"event":"interrupted","received":2}

# Wifi (experimental — every invocation requires the flag)
$ nest-cli auth wifi-setup --experimental-wifi
[interactive bootstrap; persists credentials-wifi.json]

$ nest-cli wifi list groups --experimental-wifi --json
[{"id":"...","name":"Home","points":3,"clients":24,"online":true}]

$ nest-cli wifi list clients <group-id> --experimental-wifi --jsonl
{"id":"...","friendly_name":"kid-tablet","mac":"...","ip":"192.168.86.42","connected_to_point_id":"...","band":"5","tx_rate_mbps":288.5,"paused":false,"group_assignment":"family"}

$ nest-cli wifi pause kid-tablet --experimental-wifi
{"target":"kid-tablet","paused":true}

$ nest-cli wifi speedtest run --experimental-wifi --json
{"ts":"2026-04-30T11:55:42Z","group_id":"...","point_id":"...","download_mbps":487.3,"upload_mbps":42.1,"ping_ms":11.4,"source":"router"}

$ nest-cli wifi reboot point office-point --experimental-wifi
[stderr: Reboot 'office-point' [y/N]? ] y
{"target":"office-point","rebooting":true}

# Inspect both credential states
$ nest-cli auth status --json
[
  {"family":"cam","configured":true,"google_account_email":"dan@example.com","expires_at":"2026-04-30T12:30:00Z","last_refresh_ts":"2026-04-30T11:30:00Z","probe_status":"skipped"},
  {"family":"wifi","configured":true,"google_account_email":"dan@example.com","issued_at":"2026-04-15T08:14:00Z","probe_status":"skipped"}
]

# Run a list of commands from a file, fan out across all cameras
$ cat night.batch
cam chime front-door
cam events --types doorbell-press
$ nest-cli batch --file night.batch --jsonl
```

---

## 9. Configuration File

### 9.1 Location and format

Default path: `~/.config/nest-cli/config.toml` (override via `--config` or `NEST_CLI_CONFIG`).

Format: TOML.

### 9.2 Schema

| Section | Field | Type | Default | Purpose |
|---------|-------|------|---------|---------|
| `[defaults]` | `timeout_seconds` | int | 10 | Per-operation timeout |
| `[defaults]` | `concurrency` | int | 3 | Max parallel device ops |
| `[defaults]` | `output_format` | string | `auto` | `auto`/`text`/`json`/`jsonl` |
| `[credentials.cam]` | `file_path` | string | `~/.config/nest-cli/credentials-cam.json` | Cam OAuth credentials file |
| `[credentials.wifi]` | `file_path` | string | `~/.config/nest-cli/credentials-wifi.json` | Wifi master-token credentials file |
| `[oauth]` | `callback_port` | int | 8765 | Local OAuth callback listener port |
| `[pubsub]` | `subscription_path` | string | — | Full Pub/Sub subscription path for events |
| `[logging]` | `file` | string | — | Optional file path for JSON log tee |
| `[devices.<alias>]` | `family` | string | — | `cam` or `wifi` |
| `[devices.<alias>]` | `device_id` | string | — | SDM device id (cam) or Foyer ap id (wifi) |
| `[devices.<alias>]` | `model` | string | — | Optional; informational only |
| `[groups]` | `<name>` | string[] | — | Array of alias names (cross-family allowed) |

### 9.3 Complete example

```toml
# ~/.config/nest-cli/config.toml

[defaults]
timeout_seconds = 10
concurrency = 3
output_format = "auto"

[credentials.cam]
file_path = "~/.config/nest-cli/credentials-cam.json"

[credentials.wifi]
file_path = "~/.config/nest-cli/credentials-wifi.json"

[oauth]
callback_port = 8765

[pubsub]
# Required for `cam events`. Must be a Pub/Sub subscription on the operator's
# GCP project, subscribed to the SDM-published topic.
subscription_path = "projects/dan-nest-1234/subscriptions/nest-events"

[logging]
# Optional.
# file = "~/.local/state/nest-cli/log"

[devices.front-door]
family = "cam"
device_id = "enterprises/dan-nest-1234/devices/AVPHwEsK..."
model = "battery_doorbell"

[devices.backyard]
family = "cam"
device_id = "enterprises/dan-nest-1234/devices/AVPHwEsM..."
model = "battery_cam"

[devices.office]
family = "cam"
device_id = "enterprises/dan-nest-1234/devices/AVPHwEsN..."
model = "nest_iq_indoor"

[devices.home-mesh]
family = "wifi"
device_id = "<foyer-group-id>"
model = "nest_wifi"

[devices.office-point]
family = "wifi"
device_id = "<foyer-point-id>"

[groups]
perimeter-cams = ["front-door", "backyard"]
all-cams       = ["front-door", "backyard", "office"]
kids-devices   = []  # populate by client id once known
```

### 9.4 Config validation

`nest-cli config validate` SHALL parse the file, resolve every alias-to-device reference, resolve every group-to-alias reference, verify referenced credential files exist with chmod 0600, and exit 0 only if all checks pass. Cross-family group memberships are NOT a validation error — they're explicitly allowed (FR-5).

---

## 10. Data Model

### 10.1 Camera

```text
Camera {
  alias              : string?         # null if surfaced via discover only
  device_id          : string          # SDM device id, full path
  name              : string          # user-set name from Home app
  type              : "CAMERA" | "DOORBELL" | "DISPLAY" | string  # SDM device type
  online             : bool
  battery_pct        : int?            # 0-100; null if not battery-powered
  signal_strength    : int?            # RSSI dBm; null if not exposed
  firmware_version   : string?
  traits             : string[]        # full SDM traits array
  room_name          : string?
  structure_name     : string?
  last_event_ts      : RFC3339 string? # UTC, 'Z' suffix; null if none in cache
}
```

### 10.2 Stream

```text
Stream {
  target           : string
  protocol         : "rtsp" | "webrtc"
  expires_at       : RFC3339 string

  # When protocol == "rtsp":
  url              : string?           # rtsps://... full URL with token
  stream_token     : string?
  extension_token  : string?

  # When protocol == "webrtc":
  answer_sdp       : string?
  media_session_id : string?
}
```

### 10.3 Event

```text
Event {
  ts                          : RFC3339 string  # UTC 'Z'
  target                      : string          # alias
  event_type                  : "motion" | "person" | "package" | "sound" | "doorbell-press" | "unknown"
  has_image                   : bool            # eventId present + within window
  image_eligibility_window_s  : int             # seconds remaining (0 if has_image false)
  room                        : string?
  structure                   : string?
  source                      : "pubsub"
}
```

### 10.4 SnapshotResult

```text
SnapshotResult {
  target     : string
  output     : string          # file path or "-" (stdout)
  mechanism  : "camera_image" | "camera_event_image"
  bytes      : int
}
```

### 10.5 Wifi (umbrella info record)

`info <wifi-target>` returns one of: WifiGroup (when target is a group), WifiPoint (when target is a point), WifiClient (when target is a client).

### 10.6 WifiGroup

```text
WifiGroup {
  id                 : string
  name               : string
  points             : int             # count
  clients            : int             # count
  online             : bool
  master_point_id    : string
  ssid               : string
  guest_enabled      : bool
}
```

### 10.7 WifiPoint

```text
WifiPoint {
  id                              : string
  name                           : string
  is_master                       : bool
  model                           : string?
  firmware_version                : string?
  mesh_role                       : "master" | "satellite"
  signal_strength_to_upstream_dbm : int?
  connected_clients_count         : int
  online                          : bool
  uptime_s                        : int
}
```

### 10.8 WifiClient

```text
WifiClient {
  id                       : string
  friendly_name            : string
  mac                      : string?
  ip                       : string?
  connected_to_point_id    : string
  connection_type          : "wifi" | "ethernet"
  band                     : "2.4" | "5" | "6" | null
  tx_rate_mbps             : float?
  rx_rate_mbps             : float?
  paused                   : bool
  priority_until           : RFC3339 string?
  group_assignment         : "family" | "parental" | "guest" | null
}
```

### 10.9 SpeedTest

```text
SpeedTest {
  ts             : RFC3339 string
  group_id       : string
  point_id       : string         # which point ran the test (typically master)
  download_mbps  : float
  upload_mbps    : float
  ping_ms        : float
  source         : "router"
}
```

### 10.10 WifiNetwork

```text
WifiNetwork {
  group_id        : string
  ssid            : string
  guest_ssid      : string?       # null if guest disabled
  guest_enabled   : bool
  ipv4 {
    wan              : string
    lan_subnet       : string     # CIDR
    dhcp_range_start : string
    dhcp_range_end   : string
  }
  ipv6 {
    enabled    : bool
    wan        : string?
    prefix_len : int?
  }
  dns_servers : string[]
}
```

### 10.11 WifiPointHealth

```text
WifiPointHealth {
  id                              : string
  online                          : bool
  uptime_s                        : int
  signal_to_upstream_dbm          : int?
  connected_clients_count         : int
  mesh_role                       : "master" | "satellite"
}
```

### 10.12 AuthRecord (per-family in `auth status` output)

```text
AuthRecord {
  family                 : "cam" | "wifi"
  configured             : bool
  google_account_email   : string?
  expires_at             : RFC3339 string?  # cam only
  issued_at              : RFC3339 string?  # wifi only
  last_refresh_ts        : RFC3339 string?  # cam only
  probe_status           : "ok" | "failed" | "skipped"
  probe_error            : string?
}
```

---

## 11. Error Model and Exit Codes

### 11.1 Exit code table

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Operation completed; for batch/group, every sub-op succeeded |
| 1 | Device error | Google returned a device-level error (e.g., camera offline, command not honored); Foyer reports an upstream device failure; library breaking-change indicator on wifi side |
| 2 | Authentication error | Refresh token revoked, OAuth client invalid, master token rejected, missing credentials when no other source configured, credentials file chmod-mode too permissive |
| 3 | Network error | DNS, TLS, 5xx from Google, gRPC unavailable, Foyer rotation, Pub/Sub pull failed after retry budget; concurrent-lock acquisition timeout |
| 4 | Device not found | Alias unknown in config, SDM device id not in user's device list, Foyer point id not in mesh, unknown client id |
| 5 | Unsupported feature | Verb not supported by target hardware (CameraImage trait absent and no event in window; chime on a non-doorbell; talkback on any device; quiet-hours on any device; speedtest on a wifi point that is not master); WiFi verb invoked without `--experimental-wifi` |
| 6 | Config error | Config file missing when `--config`/`NEST_CLI_CONFIG` was set; invalid TOML; unresolvable references; unknown keys; Pub/Sub subscription path missing for `cam events` |
| 7 | Partial batch/group failure | ≥1 sub-op succeeded AND ≥1 sub-op failed |
| 64 | Usage error | Invalid CLI invocation: missing required arg, mutually-exclusive flags, group target on `cam stream`, missing `--experimental-wifi` on a wifi verb, missing `--offer-sdp` on a WebRTC stream call, `--quiet` paired in a way that loses the only output channel, `reboot` non-tty without `--yes` |
| 130 | SIGINT | Ctrl-C during execution; partial JSONL stream emitted with trailing `{"event":"interrupted",...}` line |
| 143 | SIGTERM | Same partial-result + interrupted-line behavior as 130 |

### 11.2 Disambiguation notes

- **Missing credentials** (no source configured) → exit **2** (auth, not config). The user has not configured how to authenticate.
- **Credentials file chmod violation** → exit **2** (auth). Credential-source integrity failure.
- **Missing `--experimental-wifi`** → exit **5** (unsupported feature) with hint, NOT 64. Rationale: the verb exists; the operator is asking for a surface that requires opt-in. Mirrors `tapo-cli` posture for `--experimental-clips`.
- **Missing `--offer-sdp` on WebRTC stream** → exit **64** (usage error). The operator omitted a required argument.
- **WebRTC camera with malformed offer SDP** → exit **1** (device error). Google rejected the SDP; not the CLI's fault.
- **Pub/Sub subscription path missing in config for `cam events`** → exit **6** (config). The CLI cannot complete the request because a documented config field is absent.
- **Foyer endpoint shape changed (library returns "unknown response shape")** → exit **1** (device error) with hint pointing at upstream library version and the §3.2.3 risk caveat. Distinguishes "Google rotated the API" (1) from "we can't reach Google" (3).

### 11.3 Structured error object (stderr)

```json
{
  "error": "auth_failed",
  "exit_code": 2,
  "family": "cam",
  "target": "front-door",
  "credential": "oauth_refresh_token",
  "message": "OAuth refresh token rejected by Google",
  "hint": "Run `nest-cli auth setup` to re-authorize. The previous refresh token may have been revoked at https://myaccount.google.com/permissions"
}
```

The `error` enum is closed and stable: `device_error`, `auth_failed`, `network_error`, `not_found`, `unsupported_feature`, `config_error`, `partial_failure`, `usage_error`, `interrupted`. Tooling MAY pattern-match on it. The `family` field is present on every error and is one of `cam`, `wifi`, `shared`. The `credential` field appears only on auth errors and names which credential failed (`oauth_refresh_token` | `oauth_access_token` | `foyer_master_token` | `foyer_local_token`).

### 11.4 Verbose log redaction

In `-vv` mode, the CLI emits raw protocol envelopes. The following SHALL be redacted to `***` in all log output:

- OAuth refresh token, access token, client secret.
- Foyer master token, derived local tokens.
- Pub/Sub subscription path's project component (full path is logged but project id is redacted by default; `--no-redact-project` opt-in).
- Stream URLs' embedded session tokens (the URL is shown, the `?auth=` segment is redacted).
- Camera event ids in event-image fetches.

---

## 12. Testing Strategy

### 12.1 Unit tests

- **Mock SDM client.** Hand-rolled test double matching `google-nest-sdm`'s public surface; per-camera-generation fixtures for: 1st-gen Nest IQ (RTSP + CameraImage), 2nd-gen Battery Cam (WebRTC + CameraEventImage only), Battery Doorbell (WebRTC + CameraEventImage + DoorbellChime), wired Nest Hello (RTSP + CameraImage + DoorbellChime).
- **Mock Pub/Sub subscriber.** Test double for `google-cloud-pubsub`'s subscriber client; emits canned event payloads for motion / person / sound / doorbell-press scenarios.
- **Mock `googlewifi`.** Test double matching `googlewifi`'s public surface; fixtures for one master + two satellite point mesh, mixed-band clients, paused-and-active clients, fresh-and-stale speedtests.
- **Mock `glocaltokens`.** Test double for the master-token derivation flow; success path, expired-master-token rejection, invalid-email rejection, network-failure during derivation.
- **OAuth flow tests.** Local-callback HTTP listener; canned authorization-code response; refresh token rotation; access-token expiry-edge-case (refresh fires at 60s before expiry).
- **Config parser tests.** Valid configs, invalid TOML, dangling alias refs, dangling group refs, missing credentials file path, cross-family group memberships, unknown keys.
- **Output formatter tests.** JSON key stability across mock cameras and points, including: full Camera record, Stream record (RTSP and WebRTC variants), Event records, WifiGroup, WifiPoint, WifiClient, SpeedTest, AuthRecord.
- **Exit-code matrix tests.** Every exit code 0/1/2/3/4/5/6/7/64/130/143 SHALL be reachable by at least one test.
- **Snapshot fallback test.** Mock CameraImage failure → mock CameraEventImage success → assert `mechanism: "camera_event_image"`.
- **Snapshot fallback test.** CameraImage trait absent + no event in window → exit 5 with structured error.
- **Auth-rejection short-circuit test.** Mock SDM 401 on snapshot → exit 2 immediately, no fallback attempted.
- **Concurrency lock test.** Two concurrent `nest-cli` invocations both refreshing the cam access token serialize on `flock`.
- **Signal handling test.** SIGINT during a 10-element batch SHALL produce ≤10 result lines plus the `{"event":"interrupted",...}` line and exit 130.
- **Signal handling test.** SIGINT during `cam events --follow` SHALL trigger clean ack-and-exit within 2s budget.
- **Group target rejected by `cam stream`** SHALL exit 64.
- **WiFi verb without `--experimental-wifi`** SHALL exit 5.
- **Config-show secret redaction test.** OAuth client secret, refresh token, and master token SHALL be `***` in `config show` output. There is no `--show-secrets` flag.

### 12.2 Integration tests

**CI MUST NOT hit Google's APIs.** Integration tests are gated on environment variables and skipped by default:

- `NEST_CLI_TEST_OAUTH_CREDENTIALS=<path>` — when set, runs the cam integration suite against a real (operator-provided) test Google account.
- `NEST_CLI_TEST_FOYER_MASTER_TOKEN=<token>` and `NEST_CLI_TEST_GOOGLE_EMAIL=<email>` — when set, runs the wifi integration suite (against a test mesh).

CI SHALL never set either. Local development workstations are the only place these run. The CI guard SHALL refuse to start a job that has either variable set in the environment, exiting with a clear "remove this env var; tests forbidden in CI" message — defense-in-depth against accidental Google-quota burn or credential leaks in CI logs.

### 12.3 Fixture corpus

Capture sanitized real-API responses (project id, device ids, MAC addresses, IPs, account email all redacted) in `tests/fixtures/` for replay. Specifically:

- SDM `devices.list` responses for 1+ doorbell, 1+ battery cam, 1+ wired cam, 1+ display.
- SDM `devices.executeCommand` responses for: `GenerateRtspStream`, `GenerateWebRtcStream` (with both success and `INVALID_ARGUMENT` rejection on offer SDP), `GenerateImage` (success + `NOT_FOUND` on no-event-in-window), `Chime`, `ExtendStream`, `StopStream`.
- Pub/Sub message envelopes for each event type (motion, person, package, sound, doorbell-press) plus an `unknown` event with a topic the CLI doesn't recognize.
- Foyer `googlewifi` responses for: `get_groups`, `get_access_points`, `get_devices`, `pause_device`, `unpause_device`, `prioritize_device`, `run_speedtest`, `reboot_access_point`.
- `glocaltokens` derivation success/failure pairs.

### 12.4 End-to-end smoke harness

A `scripts/smoke.py` (committed to the repo, gated on operator-supplied credentials, mirroring `tapo-cli`'s Phase 0 approach) walks every cam verb against every camera the operator owns, plus every wifi verb against the operator's mesh. Output is captured for fixture-corpus refresh.

---

## 13. Distribution and Install

### 13.1 Recommended

```text
uv tool install git+ssh://git@github.com/agileguy/nest-cli
```

Or with `pipx`:

```text
pipx install git+ssh://git@github.com/agileguy/nest-cli
```

Both produce an isolated venv with the entry point `nest-cli` on PATH. Updates are `uv tool upgrade nest-cli` or `pipx upgrade nest-cli`.

### 13.2 Optional install extras

The wifi side has its own dependency footprint (`googlewifi`, `glocaltokens`, plus their transitive deps including some platform-specific protobuf). To keep the cam-only install lean, the wifi deps are an optional extra:

- `pipx install 'nest-cli[wifi]'` — installs cam + wifi deps.
- `pipx install 'nest-cli'` — installs cam deps only. Invoking any `wifi` sub-verb in this install mode SHALL exit 5 (unsupported feature) with a hint to reinstall with `[wifi]`.
- `pyproject.toml` defines `[project.optional-dependencies] wifi = ["googlewifi>=...", "glocaltokens>=..."]` accordingly.

### 13.3 Alternatives considered

- **Publish to PyPI** — out of scope for v1. Personal-tool scope.
- **`brew install nest-cli`** — would require maintaining a Homebrew tap; not warranted for a single-user tool.
- **Single-file binary via PyInstaller** — increases binary size dramatically and complicates the optional `[wifi]` extras pattern; not recommended.

### 13.4 Versioning

Tag releases as `vX.Y.Z` in git. `uv tool install git+ssh://...@vX.Y.Z` pins to a tag.

`pyproject.toml` SHALL pin upstream library versions to known-good ranges, NOT floating `>=`. Specifically: `google-nest-sdm` to a tested minor version; `google-cloud-pubsub` to a tested minor; `googlewifi` and `glocaltokens` to specific git SHAs (mirroring `tapo-cli`'s pytapo posture — these are single-maintainer libraries with a history of upstream rotations, and a floating range invites silent breakage). Rolling forward requires an explicit `nest-cli` release.

---

## 14. Out of Scope

The following are **explicitly excluded** from v1 and from all phases of this SRD:

- **Live video display in the terminal.** Use `mpv`, `ffplay`, or `vlc` downstream of `cam stream`.
- **WebRTC client implementation.** Operator supplies the offer SDP via `--offer-sdp`; the CLI does NOT generate it. A future SRD revision may revisit if `aiortc` becomes a credible CLI-embeddable WebRTC stack.
- **Two-way realtime audio (talkback).** SDM does not expose it. Out of scope at all phases.
- **Camera siren control.** Not in SDM trait set. Out of scope.
- **Camera quiet-hours / arming / disarming.** Not in SDM as of v1.0.0 of this SRD. §17 tracks the open question.
- **Event-clip download.** SDM does not expose stored video clips. Out of scope at all phases.
- **NVR / DVR functionality.** Wrong tool. Use Frigate, Shinobi, Synology Surveillance Station downstream of `cam stream`.
- **Automation rules engine.** Use Home Assistant or a cron job that pipes `cam events` into `kasa-cli` / `hue-cli`.
- **GUI dashboard.** This is a CLI.
- **Camera firmware updates.** Use the Home/Nest mobile app.
- **Wi-Fi mesh provisioning.** Use the Home app.
- **Wi-Fi guest SSID password configuration.** Out of scope in v1; setting the guest password from CLI requires a write surface Foyer exposes inconsistently (Decision 9). Use the Home app.
- **Multi-account support.** One Google account at a time per credential file. Multi-account is §17 deferred.
- **Cross-family fused verbs.** "Pause the kid-tablet wifi client AND mute the doorbell" requires two invocations. Wrapping it into a single verb is rules-engine territory.
- **Apple Home / HomeKit translation.** Use `homebridge`.
- **Matter or Thread.** Different stack.
- **Linux/macOS Python consumption of Google's 2024 Home APIs.** Those SDKs are iOS/Android only as of 2026. §17 tracks if a desktop-Python equivalent ships.
- **Camera-side schedule editing.** Read-only forever. Mirrors `kasa-cli` Decision 3 and `tapo-cli` posture.
- **Pub/Sub topic provisioning from the CLI.** Operator creates the subscription in their GCP project; the CLI consumes it. `auth setup --pubsub` (Phase 2+) is a guided walkthrough with stderr links, not an automated provisioner.
- **Group config mutation from the CLI.** v1 `groups` sub-verb is `list` only. Add/remove are deferred.
- **Comment-preserving TOML round-trip on config writes.** v1 does not write user config files.

---

## 15. Resolved Decisions

The architectural decisions surfaced during research and design are recorded here for traceability.

| # | Decision area | Outcome |
|---|---------------|---------|
| 1 | **Two top-level command groups** | `cam` and `wifi` surface the cloud-vs-cloud asymmetry honestly. Each has its own auth flow, its own credential file, and its own stability promise. Reject the unified-facade temptation. |
| 2 | **Library strategy (cam)** | Wrap `google-nest-sdm` for the SDM REST surface. Wrap `google-cloud-pubsub` for events. No reimplementation of OAuth, REST, or Pub/Sub protocol. |
| 3 | **Library strategy (wifi)** | Wrap `googlewifi` for Foyer mesh control. Wrap `glocaltokens` for master-token bootstrap. Both gated behind `--experimental-wifi` runtime flag. |
| 4 | **Implementation language** | Python 3.11+ with `uv`. Matches sibling toolchain. |
| 5 | **Single binary, optional `[wifi]` extras** | One binary, `pip install 'nest-cli[wifi]'` adds the wifi deps. Keeps cam-only installs lean. Reject the separate-binary alternative. |
| 6 | **WebRTC offer-SDP responsibility** | Operator generates the offer SDP and supplies via `--offer-sdp`. CLI does NOT embed a WebRTC stack. Revisit if `aiortc` becomes credible for CLI-embeddable WebRTC. |
| 7 | **Wifi library — `googlewifi` over `python-google-wifi`** | More active upstream as of 2026. Both are single-maintainer and break in the same windows; pick the one with fewer stale issues. Documented escape hatch in `pyproject.toml` to swap if maintenance flips. |
| 8 | **Two credential files, not one** | OAuth tokens and Foyer master tokens have different blast radii, different lifetimes, different revocation semantics. Separate files match separate threat profiles (§4.7). |
| 9 | **Wifi guest-network password setting — out of v1** | Foyer exposes guest password setting inconsistently across mesh-firmware revisions. v1 ships guest-on / guest-off only. Defer SSID/password setting to a future phase if operator pressure materializes. |
| 10 | **Pub/Sub for events, not REST polling** | SDM does not expose events via REST. Pub/Sub is the only path. Operator owns the GCP subscription; CLI consumes it. `auth setup --pubsub` deferred to Phase 2+. |
| 11 | **OAuth refresh-token storage in plaintext on disk** | chmod 0600 + `~/.config/nest-cli/` is the established sibling-toolchain bar. OS keyring integration is §17 deferred — adds platform-specific transitive deps and an inconsistent UX between macOS Keychain, Linux Secret Service, and Windows DPAPI (and we don't ship Windows). |
| 12 | **Single Google account per credential file in v1** | Multi-account is real but uncommon. The file format already admits a `bridges`-style multi-account dict (mirroring `hue-cli`'s schema) but the v1 codepath assumes one account. Schema-future-proof, code-v1-simple. |
| 13 | **WiFi behind `--experimental-wifi` runtime flag, not config-settable** | Per-invocation friction is the feature. Allowing `[defaults] experimental_wifi = true` would let an operator opt in once and forget; the resulting silent-breakage on Foyer rotation would be worse than the friction. |
| 14 | **Concurrency default 3** | Lower than `tapo-cli`'s 5 because Google's APIs ratelimit harder. Override per-command with `--concurrency N`. |
| 15 | **Token cache location** | `~/.config/nest-cli/.tokens/` (mirrors siblings). chmod 0700 directory, chmod 0600 per-file. |
| 16 | **Windows support** | Out of scope. macOS 13+ and Linux x86_64/arm64 only. WSL is the answer for Windows users. |
| 17 | **Camera siren / arming / quiet-hours / talkback** | Not in SDM. Out of scope at all phases. §17 tracks the open question if Google ships the trait. |
| 18 | **Stream URL emission posture (`cam stream`)** | RTSP cameras: emit URL on stdout, exit 0 — same shape as `tapo-cli stream`. WebRTC cameras: emit JSON session metadata (answer SDP, media-session-id) and require `--offer-sdp` from operator. The CLI does not pretend WebRTC is a URL. |
| 19 | **Snapshot fallback** | Two tiers: `CameraImage` then `CameraEventImage` keyed off recent event id. ffmpeg-from-RTSP tier deferred to Phase 2+. Deliberate scope keep-out: WebRTC-only cameras between events truly have no snapshot path; exit 5 is honest. |
| 20 | **Group fan-out concurrency cap** | Default 3 (FR-7). Camera commands plus Foyer calls are heavier than LAN-direct ops. |
| 21 | **`auth setup` interactivity** | Inherently interactive (browser-open + consent). `--non-interactive` mode supports headless CI by accepting `--auth-code` post-consent. |
| 22 | **`auth status` shows both credential states** | Even though cam and wifi are otherwise on parallel tracks, the operator wants one place to see "what is this CLI authorized to do." |
| 23 | **Pin upstream libraries to git SHAs (wifi side)** | `googlewifi` and `glocaltokens` are single-maintainer and break unpredictably. SHA pin protects scripts; rolling forward is an explicit `nest-cli` release. Cam side may pin to minor versions because Google's libraries are more stable. |

---

## 16. Phase Plan

### 16.0 Phase 0 — Onboarding gate + smoke test (1 week)

**Deliverable:** Empirical proof that the operator can complete the SDM onboarding (Google Cloud project, Device Access Console $5 fee, OAuth client) AND that `glocaltokens` + `googlewifi` work against the operator's mesh, plus a captured fixture corpus to feed §12.3.

This phase ships **no** CLI code — only smoke-test scripts and confirmed library SHAs. Phases 1+ do not start until Phase 0 passes.

Tasks:

- Operator creates Google Cloud project, enables SDM API, registers an application in Device Access Console (paying $5), downloads OAuth Desktop client JSON.
- Operator confirms each owned Nest camera's `traits` array via `scripts/smoke-cam.py` (manual OAuth via browser; calls `devices.list`, `devices.get` for each).
- Operator extracts Android master token via documented community method (out-of-band).
- Operator runs `scripts/smoke-wifi.py` to derive a Foyer master token via `glocaltokens` and call `googlewifi` for `get_groups`, `get_access_points`, `get_devices`.
- Pin `google-nest-sdm`, `google-cloud-pubsub`, `googlewifi`, `glocaltokens` to specific known-good versions (or git SHAs for the wifi side).
- Capture sanitized fixtures into `tests/fixtures/` for every endpoint touched.
- Document any model that fails a probe — that camera is flagged in the ARCHITECTURE doc's per-generation matrix.

**Exit criteria** (none of Phase 1+ starts before all met):

- [ ] OAuth flow succeeds end-to-end against the operator's Google account.
- [ ] Every owned Nest camera returns a non-empty `traits` array via `devices.get`.
- [ ] `glocaltokens` derives a valid Foyer master token from the operator's account.
- [ ] `googlewifi` lists the operator's mesh group, points, and clients.
- [ ] Smoke scripts committed to repo; fixture corpus captured.

### 16.1 Phase 1 — v0.1.0: Cam-only auth + list + info (2-3 weeks)

**Deliverable:** A binary that can authenticate against SDM, list cameras, and emit detailed info on each. No streaming, no events, no snapshots yet — first-cycle ship is "is the auth flow reliable and does our config-resolution model survive contact with reality."

- Project skeleton, `uv` packaging, entry point.
- Click-based CLI with `auth setup`, `auth refresh`, `auth revoke`, `auth status`, `list`, `discover`, `cam list`, `cam info`, `cam capabilities`, `config show`, `config validate`.
- OAuth flow with local-callback listener (FR-CRED-1..6).
- `~/.config/nest-cli/credentials-cam.json` chmod 0600.
- Token-cache directory `~/.config/nest-cli/.tokens/` chmod 0700.
- Per-credential-file `flock` serialization (FR-CRED-13).
- Output: text (default), `--json`, `--jsonl`, `--quiet`.
- Structured-error contract on stderr (§11.2).
- Exit codes 0, 1, 2, 3, 4, 5, 6, 64.
- Unit tests with mock SDM client; CI never hits Google.
- README-equivalent prose in repo `docs/` plus `--help` text on every verb.

### 16.2 Phase 2 — v0.2.0: Streams, snapshots, chime, events (3 weeks)

**Deliverable:** Full cam control surface beyond Phase 1's read-only verbs.

- Verb: `cam snapshot <target>` with two-tier fallback (FR-CAM-3..5).
- Verb: `cam stream <target>` with RTSP and WebRTC variants (FR-CAM-6..12).
- Verb: `cam stream-extend`, `cam stream-stop` (FR-CAM-13..14).
- Verb: `cam chime <target>` (FR-CAM-15..16).
- Verb: `cam battery <target>`, `cam signal <target>` (FR-CAM-26..27).
- Verb: `cam events [<target>]` (one-shot pull mode only — `--follow` slips to v0.2.1 or v0.3 depending on Pub/Sub stability).
- `auth setup --pubsub` (Phase 2 stretch — enables Pub/Sub topic-and-subscription provisioning for the operator's GCP project).
- Exit code 5 paths exercised (CameraImage absent on WebRTC-only cam; chime on non-doorbell; talkback NOT registered).
- Integration test harness against operator's real cameras (gated on `NEST_CLI_TEST_OAUTH_CREDENTIALS`); CI guard refuses if env var present.

### 16.3 Phase 2.1 — v0.2.1: Cam events --follow (1 week)

**Deliverable:** Long-running event subscription.

- `cam events --follow` with FR-CAM-21 cadence and FR-CAM-23 backoff.
- `--types <comma-list>` filter (FR-CAM-22).
- SIGINT / SIGTERM clean-ack-and-exit within 2s.
- Hardware-acceptance gate: trigger physical doorbell-press, see event arrive in `--follow` JSONL within 5s.

### 16.4 Phase 3 — v0.3.0: WiFi list + per-client controls (experimental, 2 weeks)

**Deliverable:** WiFi side gated behind `--experimental-wifi`. List + pause/unpause/prioritize.

- `auth wifi-setup --experimental-wifi`, `auth wifi-revoke --experimental-wifi`.
- `wifi list groups`, `wifi list points`, `wifi list clients` (FR-WIFI-1..3).
- `wifi pause`, `wifi unpause`, `wifi prioritize`, `wifi group-assign` (FR-WIFI-4..7).
- Mock `googlewifi` test corpus.
- Hardware-acceptance gate against operator's mesh: pause a known client, verify it loses connectivity; unpause, verify restoration.
- Optional install extras (§13.2) wired up.

### 16.5 Phase 3.1 — v0.3.1: WiFi speedtest + reboot + network info (1 week)

- `wifi speedtest run`, `wifi speedtest history` (FR-WIFI-8..9).
- `wifi reboot point`, `wifi reboot group` with confirmation (FR-WIFI-10..12).
- `wifi network`, `wifi guest enable|disable` (FR-WIFI-13..14).
- `wifi point-health` (FR-WIFI-15).

### 16.6 Phase 4 — v0.4.0: Batch + groups (1 week)

**Deliverable:** Bulk operation primitives.

- Verb: `groups list` (FR-8b — mutations remain manual TOML edits).
- `@group` and `--group` target syntax resolution (FR-6).
- Parallel execution with concurrency cap (FR-7) + per-command `--concurrency`.
- `batch` verb reading from `--file` and `--stdin` (FR-9..10c).
- Cross-family group memberships allowed (FR-5).
- Per-target JSONL envelope (FR-9a).
- `cam stream` and `cam events --follow` group-target rejection (FR-8c, FR-8d) verified.
- SIGINT/SIGTERM handling with `{"event":"interrupted",...}` summary line.

### 16.7 Phase 5+ — Deferred

- `cam events` Pub/Sub topic-and-subscription provisioning (`auth setup --pubsub` automation).
- ffmpeg-from-RTSP snapshot tier 3 (FR-CAM-4 fallback).
- Multi-account credential files.
- OS keyring integration for credential storage (macOS Keychain / Linux Secret Service).
- Wifi guest SSID + password setting (Decision 9).
- Cross-family fused verbs (rules-engine territory; probably never).

---

## 17. Open Questions and Decisions Deferred

These items are NOT in v1 scope but are tracked here so future SRD revisions can revisit if circumstances change.

1. **SDM siren / arming / disarming / quiet-hours.** Currently absent from SDM trait set. If Google ships these traits, add corresponding verbs. Tracking: review SDM changelog quarterly.
2. **SDM talkback (two-way audio).** Currently absent. Same tracking cadence as #1.
3. **Google Home APIs (announced 2024) on Linux/macOS Python.** Currently iOS/Android only. If Google ships a desktop-Python SDK, evaluate whether the wifi side should migrate from Foyer to the official surface.
4. **WebRTC client embedded in CLI.** `aiortc` is a candidate but heavy and platform-specific; revisit if a stable async-friendly Python WebRTC stack emerges.
5. **Multi-account support.** Schema admits it; v1 codepath assumes one account. Activate when operator pressure materializes.
6. **OS keyring integration for credential storage.** macOS Keychain / Linux Secret Service / Windows DPAPI; cross-platform abstraction is messy. Revisit if a clean library emerges.
7. **`googlewifi` vs `python-google-wifi` swap.** If `googlewifi` becomes unmaintained at any phase, swap to `python-google-wifi` via `pyproject.toml`. Decision logged in #15 row 7.
8. **Pub/Sub topic-and-subscription auto-provisioning in `auth setup`.** Currently a documented manual step. Auto-provisioning requires additional GCP scopes (`pubsub.admin` or similar) which inflates the OAuth ask. Trade-off: operator UX vs scope-minimization principle.
9. **Wifi guest password / SSID setting.** Foyer exposes inconsistently across mesh firmware. Revisit when reliable.
10. **`cam stream` to a downstream WebRTC-relay subprocess.** A future verb `cam stream-relay <target> --port <p>` could spawn a subprocess that completes the WebRTC handshake and re-serves as RTSP locally. Probably belongs in a separate tool.
11. **Event-clip download via Nest Aware.** SDM doesn't expose this; the mobile app does. If Google adds a public clip-fetch endpoint, add it. Otherwise out of scope forever.
12. **Apple Home / HomeKit translation.** `homebridge` exists. The CLI does not encroach.
13. **Live state monitoring beyond events.** SDM doesn't expose telemetry beyond the Pub/Sub stream. Foyer might (Wi-Fi mesh has live throughput stats); if a `cam metrics --follow` or `wifi metrics --follow` verb earns its keep, add it post-v1.
14. **Cross-family fused verbs.** "Pause kid-tablet wifi client AND mute doorbell after 9pm" — that's an automation rules engine. Probably belongs in Home Assistant, not this CLI.
15. **Cam config-from-CLI (write paths to room/structure/name).** SDM exposes very limited write surface beyond the per-trait commands. Revisit if write-path traits land.
16. **Refresh-token rotation policy.** Google's refresh tokens don't expire on their own but can be invalidated by user-side consent revocation, security-incident response, or a Google-internal rotation. The CLI handles the auth-failure path (exit 2 with hint to re-`auth setup`); if Google announces a forced-rotation cadence, formalize it here.

---

## §17 Phase B implementation note (2026-05-03 — wifi side)

This section documents how the wifi-side implementation diverged from the v1 SRD plan (§3.2.1, §3.2.2, §15 Decision 7, §15 Decision 23) when the spec collided with empirical reality during Phase 3 hardening.

### What the SRD originally planned

The SRD called for wrapping `googlewifi` (PyPI) for the Foyer mesh control surface and `glocaltokens` (PyPI) for the master-token bootstrap (§3.2.2, §15 Decision 7). Both libraries were to be pinned tight (§15 Decision 23) and gated behind `--experimental-wifi` (§16.4). Phase 3 (v0.3.x) shipped this design.

### What broke

Empirical validation against a live Active T1 + Nest Wifi Pro mesh on 2026-05-03 proved the layered design fundamentally cannot work on AAS-bootstrapped tokens:

1. **Token-type mismatch.** `googlewifi.GoogleWifi.get_access_token()` calls `https://www.googleapis.com/oauth2/v4/token` with `grant_type=refresh_token`, expecting a standard OAuth2 refresh token (`1//09xxx...`). The token operators extract from a paired Android device via the `glocaltokens` / `gpsoauth` bootstrap is an AAS master token (`aas_et/...`). Google's auth backend rejects the AAS token at the `oauth2/v4/token` endpoint with "Authorization Error". The two flows are not interchangeable — `refresh_token` is for OAuth user-consent flows; AAS is for Android device-pair flows. The SRD did not catch this because both tokens are colloquially called "refresh tokens" in community documentation.

2. **`glocaltokens` 0.7.x bug.** `GLocalAuthenticationTokens.get_access_token()` calls `get_master_token()`, which has an early-return `if username is None or password is None` even when `self.master_token` is already populated by the constructor. Workaround: bypass `glocaltokens` entirely and call `gpsoauth.perform_oauth()` directly.

### What Phase B (post-v0.4.0) ships instead

- **Direct `gpsoauth.perform_oauth(email, master_token, android_id, ...)`** to mint a 1-hour Foyer access token (signed against `com.google.android.apps.chromecast.app`).
- **Direct gRPC** to `googlehomefoyer-pa.googleapis.com:443` via the protobuf stubs from `ghome-foyer-api` (PyPI). `StructuresServiceStub.GetHomeGraph()` returns the inventory the CLI projects onto the existing `WifiGroup` / `WifiPoint` models.
- **`googlewifi` and `glocaltokens` dropped** from `[wifi]` extras. Replaced with `gpsoauth`, `grpcio`, `googleapis-common-protos`, `ghome-foyer-api`.
- **`WifiCredentials` schema bumped 1 → 2** with required `android_id` (16-char hex from the rooted Android device's `gservices.db`). v1 files fail load-time validation.

### What the FRs now mean

- **FR-WIFI-1, FR-WIFI-2, FR-WIFI-15** (list groups, list points, point-health) ship implemented in Phase B — the data is derivable from `GetHomeGraph` alone.
- **FR-WIFI-3, FR-WIFI-4..14** (list clients, action verbs, network info, guest, speedtest, reboot) ship as exit-5 (`unsupported_feature`, `family="wifi"`) stubs in Phase B. The CLI surface is fully wired so operator scripts work; the FoyerClient method body raises a structured error pointing at the Phase-C deferral. Phase C will map each Foyer RPC and replace the stubs.
- **FR-WIFI-13** (network info) is specifically deferred because `GetHomeGraph` carries no SSID, IPv4/IPv6, DHCP, or DNS data — the verb would have to return placeholder `"<unknown>"` records, which the simplify-pass review flagged as actively misleading.

### Source updates

- §3.2.1 / §3.2.2 / §15 Decision 7 / §15 Decision 23 / §16.4 are accurate as a record of v0.3.x implementation. They are superseded by this §17 for the post-v0.4.0 wifi side.
- §13.2 wifi optional-extra deps are updated in `pyproject.toml`; the v1 list (`googlewifi`, `glocaltokens`) is replaced by `gpsoauth`, `grpcio`, `googleapis-common-protos`, `ghome-foyer-api`.
- §11 fixture corpus references (`mock googlewifi`, `mock glocaltokens`) are now `mock _fetch_systems` (the gRPC seam) — see `tests/wifi/conftest.py` Phase B rewrite.

---

**End of document.**
