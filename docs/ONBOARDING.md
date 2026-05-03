# Operator onboarding

This is the runbook for getting `nest-cli` working against your own Google account and Nest hardware. Going through it once unlocks every cam-side verb. The wifi side is gated behind `--experimental-wifi` and not in v0.1.0 — its onboarding is documented separately when that release ships.

**Time required:** ~15-30 minutes for the cam side, plus 5-10 minutes per smoke-test cycle if you want to ratify against real hardware.

**Cost:** **\$5 USD one-time, non-refundable**, paid to Google for Device Access registration. There is no way to bypass this and there is no free tier — every developer using SDM pays it.

---

## Prerequisites checklist

Before you start, confirm:

- [ ] You have a Google account that owns Nest cameras / doorbells you want to control.
- [ ] You can reach `console.cloud.google.com` and `console.nest.google.com/device-access` from your network.
- [ ] You have a credit card or payment method on the Google account for the \$5 fee.
- [ ] Python 3.11+ is installed (`python3 --version`).
- [ ] `uv` is installed (`uv --version`). If not: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

---

## Step 1 — Create a Google Cloud project + enable SDM

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it whatever you like (e.g. `nest-cli-personal`). Note the **project ID** — you'll need it later. (The ID is auto-generated from the name; it's the lowercase-with-hyphens form.)
3. Once the project is created, switch to it (top-left project picker).
4. Enable the Smart Device Management API: <https://console.cloud.google.com/apis/library/smartdevicemanagement.googleapis.com> → click **Enable**.

---

## Step 2 — Register on the Device Access Console (\$5 USD)

1. Go to <https://console.nest.google.com/device-access>.
2. Click **Get Started**, accept the terms.
3. Pay the **\$5 USD non-refundable** registration fee (this is a per-developer fee, not per project).
4. Click **Create project**. When prompted for an OAuth client ID, leave blank for now — you'll fill this in after Step 3.
5. After creation, you'll see a **Project ID** specifically for Device Access (this is **different** from your Google Cloud project ID — both are needed). Note it down; this is what `nest-cli auth setup` calls `google_cloud_project_id`.

---

## Step 3 — Create a Desktop OAuth client

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. Click **Create credentials** → **OAuth client ID**.
3. If prompted to configure the consent screen first: choose **External**, fill in your email + product name (e.g. `nest-cli`), add your Google account email as a test user, save. Don't bother publishing — you only need it in test mode for personal use.
4. Application type: **Desktop app**.
5. Name: anything (e.g. `nest-cli desktop`).
6. After creation, click **Download JSON** — save the file somewhere you control (e.g. `~/.config/nest-cli/oauth-client.json`). This contains the `client_id` and `client_secret` you'll feed `nest-cli auth setup`.

---

## Step 4 — Link the OAuth client to Device Access

1. Back at <https://console.nest.google.com/device-access>, edit your project.
2. Paste the **OAuth Client ID** from the JSON you just downloaded.
3. Save.

---

## Step 5 — Run `nest-cli auth setup`

```bash
nest-cli auth setup
```

This will:

1. Prompt for your **Device Access project ID** (Step 2).
2. Prompt for your **OAuth client ID** and **client secret** (from the JSON in Step 3).
3. Open a browser to Google's consent screen.
4. Wait for you to log in with the Google account that owns the Nest devices and authorize `nest-cli`.
5. Receive the OAuth callback on `127.0.0.1:8765` (override with `--callback-port` if that port is busy).
6. Persist credentials to `~/.config/nest-cli/credentials-cam.json` chmod 0600.

If consent fails or the port is busy, the setup verb exits with a structured error and a hint. Re-running after a typo is fine — pass `--overwrite` to replace existing credentials.

**Verify:**

```bash
nest-cli auth status --json
```

Should print a JSON array with one element showing your project ID, redacted client ID, and a non-zero `time_until_expiry_seconds`.

---

## Step 6 — Discover your devices

```bash
nest-cli discover --json
```

Should emit one JSON object per device the credentials grant access to. Cameras show their full SDM `enterprises/.../devices/...` path, type, traits, and online state. Confirm every camera you own appears. If anything is missing, double-check Step 2's project (the device must be on the same Google account that authorized the OAuth flow).

---

## Step 7 — Save device aliases

`nest-cli` reads `~/.config/nest-cli/config.toml` if it exists. Add aliases so you don't have to type the full SDM path every time:

```toml
[aliases]
front-door  = "enterprises/abc-1234-5678-90ef/devices/AVPHwH...REDACTED..."
kitchen-cam = "enterprises/abc-1234-5678-90ef/devices/AVPHxx...REDACTED..."

[groups]
all-cams = ["front-door", "kitchen-cam"]
```

Then:

```bash
nest-cli cam info front-door --json
nest-cli cam capabilities front-door
nest-cli list --groups
```

Validate the config any time:

```bash
nest-cli config validate
```

Exits 0 if OK; 6 with a structured error pointing at the offending line if not.

---

## Step 8 (optional) — Run the smoke scripts to capture sanitized fixtures

The repo ships two operator-run smoke scripts at `scripts/smoke-cam.py` and `scripts/smoke-wifi.py`. They do **not** depend on the installed `nest-cli` package — they're standalone and use `argparse`. Their purpose is to:

- Empirically prove your Google Cloud / Device Access onboarding works against real hardware.
- Capture sanitized JSON fixtures for unit tests in later phases (Phase 2+).

You only need this if you want to contribute fixtures back to the repo or feed the captures into your own fork's tests. For day-to-day operator use, skip.

```bash
cd /path/to/nest-cli   # the repo, not the installed package
python scripts/smoke-cam.py \
  --client-secret-json ~/.config/nest-cli/oauth-client.json \
  --google-cloud-project-id YOUR_DEVICE_ACCESS_PROJECT_ID
```

The script writes redacted fixtures to `tests/fixtures/sdm/captured/` (gitignored). Review each file before promoting any to `tests/fixtures/sdm/samples/` (committed). The redactor scrubs identifiers, but human review is the final defense.

The wifi smoke script (`scripts/smoke-wifi.py`) requires you to extract an Android master token via the documented community method (`gpsoauth` against a paired Android device). That path is out of scope here — see SRD §3.2.1 for the bootstrap reference.

---

## Where everything lives

| Artifact                                         | Path                                                           |
|--------------------------------------------------|----------------------------------------------------------------|
| OAuth client JSON (you download this)            | wherever you saved it (recommended: `~/.config/nest-cli/oauth-client.json`) |
| Persisted credentials (created by `auth setup`)  | `~/.config/nest-cli/credentials-cam.json` chmod **0600**       |
| Token cache                                      | `~/.config/nest-cli/.tokens/` chmod **0700**                   |
| Local config (you create this by hand)           | `~/.config/nest-cli/config.toml`                               |
| Smoke-script captured fixtures (gitignored)      | `<repo>/tests/fixtures/sdm/captured/`, `<repo>/tests/fixtures/foyer/captured/` |

Set `XDG_CONFIG_HOME` to override the `~/.config` parent.

---

## Re-running, rotating, revoking

- **Refresh access token manually:** `nest-cli auth refresh`. Normally automatic when `expires_at` is within 60s of now.
- **Re-run setup with new credentials:** `nest-cli auth setup --overwrite`.
- **Revoke and start over:** `nest-cli auth revoke` (calls Google's revoke endpoint, then scrubs the local file). After this, every cam verb exits 2 until you re-run `auth setup`.

Google's refresh tokens don't expire on their own but can be invalidated by:
1. You revoking consent at <https://myaccount.google.com/permissions>.
2. A Google security event auto-revoking it.
3. The `auth revoke` verb above.

If Google invalidates your token out-of-band, the next cam verb returns exit 2 with a hint pointing at `nest-cli auth setup`.

---

## Troubleshooting

**"Address already in use" during `auth setup`** — port 8765 is busy. Pick another with `--callback-port 8766`. Make sure you also update the redirect URI in your OAuth client config if you persistently change it.

**Browser doesn't open during `auth setup`** — pass `--no-browser` to print the consent URL on stderr instead of trying to launch a browser. Useful on headless boxes (SSH sessions, CI runners). You then open the URL on a workstation, complete consent, and the local callback listener still receives the redirect.

**"credentials file mode is too permissive"** — `~/.config/nest-cli/credentials-cam.json` is not chmod 0600. Fix: `chmod 600 ~/.config/nest-cli/credentials-cam.json`. The chmod-enforce check is intentional (SRD §FR-CRED-2 / threat model §4.7).

**"SDM API rejected access token"** after a successful setup — refresh-token-revoked by Google, or your Device Access registration lapsed. Re-run `auth setup --overwrite`.

**`discover` returns an empty list** — the OAuth flow used a different Google account than the one that owns your Nest devices. Run `auth revoke` then `auth setup` again, paying attention to which account you authorize.

**`cam info <alias>` exits 4** — the alias resolves but the SDM device path no longer exists (e.g. you removed the camera from the Google Home app). Check `discover` for the current device list, update `~/.config/nest-cli/config.toml`.

---

## Reference

- SRD §6.2 — full OAuth flow specification
- SRD §3.1 — SDM API background
- SRD §4.7 — threat model
- SRD §16.0 — Phase 0 onboarding gate (where the \$5 fee + smoke-test corpus live)
