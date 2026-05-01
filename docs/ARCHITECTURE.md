# Architecture (planning artifact)

This document is a Phase 0 planning artifact. It captures the per-camera
generation matrix from
[SRD-nest-cli.md §3.1.2](SRD-nest-cli.md#312-stream-protocol-per-generation-the-headline-asymmetry)
so that Phase 1+ implementation work can size verb behavior without scrolling
through the full SRD. The matrix is **not** a runtime authority — see "How
the CLI actually decides" below.

## Per-generation capability matrix

The columns are SDM traits the CLI cares about. A `+` means "generation
typically exposes this trait"; a `-` means "generation typically does not".
This is shaped by Google's hardware revisions, **not** by the SDM contract
itself — the SDM contract is "look at the device's `traits` array and gate
on what's actually there." Use this table to set expectations at design
time, not as runtime input.

| Generation                            | Stream protocol        | `CameraImage` | `CameraEventImage` | `DoorbellChime` |
|---------------------------------------|------------------------|---------------|--------------------|-----------------|
| 1st-gen Nest Cam (Indoor / Outdoor / IQ) | `RTSP` (`GenerateRtspStream`)   | +             | +                  | -               |
| Nest Hello (1st-gen wired doorbell)   | `RTSP` (`GenerateRtspStream`)   | +             | +                  | +               |
| 2nd-gen Battery Cam                   | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | -               |
| 2nd-gen Battery Doorbell              | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | +               |
| 2nd-gen Floodlight Cam                | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | -               |
| post-2021 generic                     | `WEB_RTC` (`GenerateWebRtcStream`) | -          | +                  | (model dependent) |

Source: [SRD §3.1.2](SRD-nest-cli.md#312-stream-protocol-per-generation-the-headline-asymmetry).
The headline asymmetry is the `RTSP` vs `WEB_RTC` split: 1st-gen hardware
exposes a stable RTSP URL the operator can pipe straight into `ffmpeg`/`mpv`,
whereas every 2nd-gen and later camera ships `WEB_RTC` only — a session-bound
SDP-exchange flow with a few-minute `expiresAt` and a downstream WebRTC peer
required to actually consume the stream. The same split drives the snapshot
fallback chain (1st-gen has `CameraImage`; 2nd-gen and later only expose
`CameraEventImage` keyed off the most recent qualifying `eventId`).

## How the CLI actually decides

The CLI **does not gate on model name**. At runtime, every per-device verb
calls `devices.get` (cached), inspects the returned `traits` array, and
routes based on which traits are present:

- `cam stream` → checks `CameraLiveStream.supportedProtocols`. If `RTSP`
  is present, calls `GenerateRtspStream` and emits the URL. If only
  `WEB_RTC` is present, it requires `--offer-sdp` and calls
  `GenerateWebRtcStream`.
- `cam snapshot` → checks for `CameraImage` first; falls back to
  `CameraEventImage` keyed off the most recent qualifying event.
- `cam chime` → requires `DoorbellChime` to be present in the device's
  traits; otherwise exits with the unsupported-feature code (5).

This is **explicitly per-SRD FR**: `nest-cli` does not lookup a hardcoded
model-to-capability table at runtime. The matrix above is for human
planning only. If Google ships a future generation that flips a column,
the CLI's runtime behavior tracks Google's `traits` payload directly with
no code change required — though this document will need a row added.

## Out of scope at Phase 0

This file does **not** cover:

- The wifi side per-mesh-firmware matrix (deferred until the wifi slice
  lands in Phase 3 — SRD §16.4).
- The Pub/Sub events surface (separate routing concern; deferred until
  Phase 2 — SRD §16.2 / §3.1.3).
- Auth credential file layout (covered in [SECURITY.md](SECURITY.md) and
  the SRD §FR-CRED section).

All of those are intentional Phase 0 omissions per SRD §16.0 — Phase 0's
deliverable is the smoke-test gate plus the dependency pin set, not the
architecture document for verbs that do not exist yet.
