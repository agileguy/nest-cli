# Phase D — OAuth Reverse-Engineering Plan

**Status:** Open / not yet executed
**Author:** Dan Elliott
**Created:** 2026-05-03
**Related:** SRD §17 Phase C addendum; CHANGELOG `[Unreleased]` notes; PR #9 (`8409e2a`)

---

## Goal

Discover the OAuth flow the **live Google Home Android app uses today (2026)** to obtain credentials for Foyer REST endpoints at `googlehomefoyer-pa.googleapis.com/v2/...`, then replicate it from a Mac so `nest-cli`'s Phase C action verbs can be live-verified against real Nest Wifi mesh hardware.

**Why this is the only remaining path**: every public OAuth method for the `936475272427.apps.googleusercontent.com` client ID is closed by Google as of 2026 — see SRD §17 Phase C addendum for the exhaustive probe results. The live Google Home app is, by definition, still successfully authenticating against Foyer (the operator can use the app today). Whatever flow it uses *must* still work; we just don't know what it is.

---

## Target

- **Device:** Active T1, rooted via Magisk v30.7
- **Account:** `the.daddy.magoo@gmail.com` — already has Google Home installed and consented for Nest Wifi mesh control
- **Mesh under test:** "Home" group, id `036f7d70-a247-4dd0-a81e-24abb460f209`, 6 points, currently reachable via Phase B reads
- **App under inspection:** Google Home (`com.google.android.apps.chromecast.app`) — current Play Store version as of investigation date

---

## Approach

Two-layer interception:

1. **Network capture** with mitmproxy — see every HTTPS request the Google Home app sends, including all auth flows (token mints, refresh paths, scope upgrades).
2. **TLS interception** via Frida-based certificate pinning bypass — the Google Home app pins its TLS certificates, so mitmproxy's MITM cert won't be trusted unless we patch the app's pin-validation logic at runtime.

Once we can read the traffic, the answer is one of:

- A new OAuth flow rooted in a different endpoint we haven't tried (most likely)
- A different client_id the app uses internally (possible — the public `936475272427` may be a legacy fallback)
- A completely different auth mechanism (e.g., Google Play Services account token API → mint via system-level call rather than user-space OAuth)

---

## Pre-requisites checklist

Before starting the actual capture session, confirm each:

- [ ] Active T1 is rooted (Magisk v30.7 confirmed alive — `adb shell su -c id` returns `uid=0`)
- [ ] T1 connected to Mac via USB; `adb devices` shows the device authorized
- [ ] Mac has `mitmproxy` installed (`brew install mitmproxy`); minimum version 10.x
- [ ] Mac has `frida-tools` installed (`pip install frida-tools` or `uv tool install frida-tools`); minimum 16.x
- [ ] Mac has the matching `frida-server-*-android-arm64` binary downloaded from `https://github.com/frida/frida/releases` (must match the `frida-tools` major version)
- [ ] Operator has the Google Home app installed on the T1 with the.daddy.magoo@gmail.com signed in and the Home mesh visible
- [ ] Operator has 2-4 hours of focused time (cert pinning bypass is the unpredictable step; capture itself is fast)
- [ ] Mac and T1 are on the same wifi network (the T1 will route HTTP traffic through the Mac via proxy settings)

---

## Step-by-step plan

### Phase 0 — Tooling setup (~30 min)

1. **Install Frida server on T1**:
   ```bash
   adb push frida-server-*-android-arm64 /data/local/tmp/frida-server
   adb shell "su -c 'chmod 755 /data/local/tmp/frida-server'"
   adb shell "su -c '/data/local/tmp/frida-server &'"
   # Verify from Mac:
   frida-ps -U | head
   ```

2. **Configure mitmproxy on Mac**:
   ```bash
   # Run mitmproxy in interactive mode on a known port:
   mitmproxy --listen-port 8080 --set block_global=false
   # Note Mac's LAN IP (e.g., 192.168.1.50)
   ```

3. **Install mitmproxy CA cert on T1 as a system root**:
   ```bash
   # Cert lives at ~/.mitmproxy/mitmproxy-ca-cert.cer after first mitmproxy run
   # Convert to Android system cert format:
   openssl x509 -in ~/.mitmproxy/mitmproxy-ca-cert.cer -inform PEM \
     -subject_hash_old | head -1
   # Returns hash like c8750f0d
   cp ~/.mitmproxy/mitmproxy-ca-cert.cer /tmp/c8750f0d.0
   adb push /tmp/c8750f0d.0 /sdcard/c8750f0d.0
   adb shell "su -c 'mount -o remount,rw /system'"
   adb shell "su -c 'cp /sdcard/c8750f0d.0 /system/etc/security/cacerts/'"
   adb shell "su -c 'chmod 644 /system/etc/security/cacerts/c8750f0d.0'"
   adb shell "su -c 'mount -o remount,ro /system'"
   adb reboot
   # After reboot, verify in Settings → Security → Trusted credentials → System
   ```

   **Alternative if `/system` is read-only even with root**: use the `MagiskTrustUserCerts` Magisk module, which makes user-installed certs trusted at system level. Install via Magisk Manager → Modules → search.

4. **Configure T1 wifi to use Mac as proxy**:
   - Settings → Wifi → long-press connected SSID → Modify → Advanced → Proxy: Manual
   - Hostname: `<Mac LAN IP>` Port: `8080`
   - Save

5. **Smoke-test the proxy**: open Chrome on T1 and visit `https://example.com`. Should appear in mitmproxy. If not, re-check proxy settings and Mac firewall.

### Phase 1 — Cert pinning bypass (~30-90 min, unpredictable)

The Google Home app pins. Without bypass, mitmproxy will see the connection but the app will refuse the response. Two approaches in order of preference:

1. **Frida CodeShare universal pin bypass**:
   ```bash
   frida -U -f com.google.android.apps.chromecast.app \
     --codeshare akabe1/frida-multiple-unpinning \
     --no-pause
   ```

   Open the Google Home app via `frida -f` (this auto-launches it). If the script logs "✅ unpinned ...", proceed to Phase 2. If you see traffic in mitmproxy and the app loads, you're good.

2. **If universal bypass fails** (Google Home may use proprietary pinning logic on top of standard Android APIs):
   - Use `objection`:
     ```bash
     objection -g com.google.android.apps.chromecast.app explore
     # In objection shell:
     android sslpinning disable
     ```
   - If THAT fails, decompile the APK with `jadx` and find the pinning class manually:
     ```bash
     adb shell pm path com.google.android.apps.chromecast.app
     adb pull /data/app/.../base.apk /tmp/google-home.apk
     jadx /tmp/google-home.apk -d /tmp/google-home-src/
     # Search for: CertificatePinner, X509TrustManager, checkServerTrusted
     # Write a custom Frida hook that no-ops the relevant method
     ```

3. **If pinning genuinely cannot be bypassed**: abort Phase D. Fall back to APK decompile (Phase 5 alternative below).

### Phase 2 — Targeted traffic capture (~15 min)

With pinning bypassed and traffic flowing through mitmproxy:

1. **Filter mitmproxy** to the relevant hosts:
   ```
   # In mitmproxy interactive view, press 'f' then enter:
   ~d googlehomefoyer-pa.googleapis.com|oauth2.googleapis.com|oauthaccountmanager.googleapis.com|accounts.google.com|android.clients.google.com
   ```

2. **Trigger each interesting code path** in the Google Home app while watching mitmproxy:
   - **List clients**: tap the mesh → "Devices" tab → see connected stations. Capture the `GET /v2/groups/{gid}/stations` request and walk back through the auth chain.
   - **Pause a device**: pick a non-critical station (e.g., a guest device) → tap → Pause. Capture the `PUT /v2/groups/{gid}/stationBlocking`.
   - **Speedtest**: tap mesh → Speedtest → Run. Capture the `POST /v2/groups/{gid}/wanSpeedTest` and the polling chain.
   - **Reboot a single point**: pick the LEAST critical mesh point → tap → Reboot point. Capture `POST /v2/accesspoints/{apId}/reboot`. **DO NOT reboot the master point during capture — losing the network drops the mitmproxy session.**

3. **For EACH captured request to a `/v2/...` endpoint, record**:
   - Full request headers (especially `Authorization`, `User-Agent`, `X-Goog-*`)
   - Full request body
   - Response status and body
   - The auth chain that produced the bearer (back-trace through prior requests in the session)

### Phase 3 — Auth chain reconstruction (~30-60 min)

Working backward from the captured Foyer REST request:

1. **Identify the bearer token**: copy the `Authorization: Bearer ya29...` value from a captured Foyer REST request.
2. **Find where that token came from**: scroll back in mitmproxy until you see a response that contains that token (look for it in `access_token`, `token`, or `Auth` fields of JSON responses).
3. **Identify that minting request's bearer/credential** — repeat backward.
4. **Continue until you reach** either:
   - A request signed by a system-level credential (master_token / AAS), OR
   - A request that uses no bearer at all (raw OAuth start)

Document the full chain in a sequence diagram. Compare against:
- The current `_refresh_onhub_access_token` implementation (`nest_cli/wifi/client.py` ~line 768)
- djtimca/googlewifi-api's chain
- The 2026 dead-end probes in the SRD §17 Phase C addendum

The chain WILL differ from djtimca's. The whole point of this exercise is to find HOW it differs.

### Phase 4 — Replay from Mac (~30 min)

Once the chain is documented:

1. **Write a Python script** (`scripts/foyer-auth-chain-prototype.py`) that replicates the captured chain step-by-step using `requests`, starting from the same inputs the operator already has (master_token, android_id) or whatever new entry point the chain reveals.
2. **Run the script** and verify it produces a valid Foyer REST bearer that successfully calls `GET /v2/groups/{gid}/stations`.
3. **If a step requires a credential we don't have a way to mint** (e.g., a Play Services attestation token), document that as the new blocker and pivot strategy.

### Phase 5 — Integrate into nest-cli (~1-2 hours)

Once a working chain exists in the prototype script:

1. Replace the body of `FoyerClient._refresh_onhub_access_token` with the new chain.
2. Update `WifiCredentials` schema if a new field is required (e.g., a captured device-bound token instead of a refresh_token).
3. Update `auth wifi-refresh-bootstrap` (or rename the verb) to bootstrap the new credential.
4. Update tests to mock the new chain.
5. Update `_REFRESH_TOKEN_HINT` and SRD §17 Phase C addendum with the working method.
6. Live-verify each Phase C action verb against the Home mesh.
7. Bump version to v0.6.0; ship.

---

## Risks and mitigations

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Cert pinning bypass fails outright | Medium | Hard block on Phase D | Fall back to APK decompile (Phase 5 alt below) |
| App uses Play Integrity attestation | Medium | Tokens may be device-bound and non-replayable from Mac | Capture from T1 only; nest-cli runs the Foyer call from Mac with a token that may have a TTL — verify TTL is operationally adequate |
| Captured token short-lived (~1 hour) | High | Frequent re-mints needed | Architect cache like Phase B/C already does; the captured token's mint endpoint is the long-lived credential |
| Google rotates the auth flow during capture | Low | Wasted session | Capture cleanly the first time; sessions are cheap to repeat |
| Reboot of master point during capture drops session | Medium | Lost capture | Avoid rebooting master; reboot leaf points only |
| `/system` is genuinely read-only even with root (verified-boot devices) | Low for Magisk | Cert install fails | Use `MagiskTrustUserCerts` module |
| App refuses to run under Frida (anti-debug) | Low | Need stealthier injection | Switch from `frida -f` (spawn) to `frida -p PID` (attach to running) |
| Operator account flagged for unusual auth pattern | Very Low | Account temporarily locked | Use throwaway Google account on T1 if paranoid; this device is already Dan's primary, so risk is minimal |

---

## Phase 5 alternative — APK decompile (if interception fails)

If pinning bypass cannot be made to work (Google's anti-tampering keeps escalating):

1. Pull the APK: `adb pull /data/app/.../base.apk /tmp/google-home.apk`
2. Decompile with `jadx`: `jadx -d /tmp/google-home-src /tmp/google-home.apk`
3. Search for Foyer endpoints in the decompiled source:
   ```bash
   grep -r "googlehomefoyer-pa" /tmp/google-home-src/ | head
   grep -r "stationBlocking\|wanSpeedTest\|/v2/groups" /tmp/google-home-src/ | head
   ```
4. Trace the call sites back to where the auth header is set. This gives the auth method without needing live capture.

This is harder than network capture (the source is obfuscated), but it works offline and doesn't need pinning bypass.

---

## Deliverables (when Phase D executes)

- `scripts/frida-pin-bypass.js` — known-working Frida hook for Google Home app
- `scripts/foyer-auth-chain-prototype.py` — Python replay of the captured auth chain
- Updated `nest_cli/wifi/client.py` with the new auth method
- Updated `tests/wifi/` with mocks for the new chain
- Updated `docs/SRD-nest-cli.md` §17 Phase C addendum with the working method
- Updated `CHANGELOG.md` with the Phase D entry and the v0.6.0 version bump
- `docs/PHASE-D-CAPTURE-NOTES.md` — sanitized capture session notes (no live tokens, just method + endpoint shape)

---

## Operational state at planning time (2026-05-03)

For the operator picking this up later:

- **Master token (live):** `/tmp/foyer-bootstrap/master_token_v2.txt` (224 bytes, prefix `aas_et/AKppINY...`) — minted via gpsoauth, valid as of capture session start
- **Android ID:** `3ed8458512f92bed` (from `gservices.db` on the T1)
- **v2 credentials file:** `~/.config/nest-cli/credentials-wifi.json` (working for Phase B reads)
- **Reachable via Phase B:** `wifi list groups` → `[{id:"036f7d70-...", name:"Home", points:6}]`
- **Validation scripts directory:** `/Users/dan/repos/active-t1-unlock/foyer-validation/` (gpsoauth path proven)
- **Known mesh point ids:** to be obtained at session start via `wifi list points 036f7d70-a247-4dd0-a81e-24abb460f209`

---

**This plan is the single canonical reference for unblocking Phase C action verbs. Update on each session whether the session succeeded or failed.**
