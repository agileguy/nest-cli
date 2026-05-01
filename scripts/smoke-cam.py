#!/usr/bin/env python3
"""Phase 0 cam-side smoke script — operator runs ONCE against real hardware.

Purpose
-------
Empirically prove that the operator's Google Cloud / Device Access onboarding
works end-to-end and capture a sanitized fixture corpus for later phases.

What it does
------------
1. Drives the OAuth Desktop installed-app flow against the SDM scope using
   the operator-supplied ``client_secret_*.json`` they downloaded from the
   Google Cloud Console.
2. Calls the SDM REST endpoint
   ``GET /v1/enterprises/{project_id}/devices`` to enumerate the operator's
   devices.
3. Calls ``GET /v1/{device_name}`` for each returned device.
4. Writes one sanitized fixture per device to ``<fixtures-dir>/devices_get_<slug>.json``,
   plus an aggregate ``devices_list.json``.
5. Prints a summary table to stdout (slug-only — no real ids).

This script is **standalone**. It does NOT import the ``nest_cli`` package.
The ``nest_cli`` package itself ships no CLI verbs in Phase 0 (SRD §16.0);
this script is the operator's hand-driven onboarding gate.

Redaction rules (defense-in-depth)
----------------------------------
The captured JSON has every real identifier scrubbed before it touches disk.
Two layers:

1. **Key-driven walk:** any string value whose surrounding key matches one of
   the registered PII classes (``name`` at object root, ``displayName`` under
   ``parentRelations``, anything containing ``customName`` / ``customId`` /
   ``structureId``) is replaced with a deterministic placeholder of the form
   ``{{<CLASS>_<N>}}``. The same input value always produces the same
   placeholder within a run, so fixture diffs are stable.
2. **Post-scan veto:** after the walk, the serialized output is regex-scanned
   for any residual ``enterprises/...`` substring or any UUID-shaped string
   that looks like a structure / device id. If anything matches, the script
   fails loud (exit 4, ``RedactionError``) rather than write a fixture
   that might leak.

Both layers must pass for a fixture to land. If you hit a redaction failure,
the bug is in the registered key set — extend ``_REDACTION_KEYS`` and re-run.

Usage
-----
    python scripts/smoke-cam.py \\
        --client-secret-json ~/Downloads/client_secret_xxx.json \\
        --google-cloud-project-id my-nest-cli-project \\
        --fixtures-dir tests/fixtures/sdm/captured

The operator supplies the project id (from the Google Cloud / Device Access
Console). The OAuth flow opens a browser for consent on first run; subsequent
runs in a fresh dir will re-prompt (this script does NOT cache tokens — that
is the production CLI's job in Phase 1).

Exit codes
----------
- 0  success
- 2  OAuth / network failure
- 3  SDM API returned a non-2xx
- 4  Redaction veto (post-scan caught a residual real id)
- 64 Usage error (bad args)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# --- Redaction --------------------------------------------------------------

# Keys whose VALUE (a string) must be redacted. Match is done with
# case-sensitive substring containment against the dict key. Order matters
# only insofar as the placeholder class name is taken from the FIRST match.
_REDACTION_KEYS: tuple[tuple[str, str], ...] = (
    # SDM device.name is the full path "enterprises/{proj}/devices/{id}".
    # Redact wholesale at the top level. Sub-walkers also catch nested.
    ("name", "DEVICE_NAME"),
    ("parent", "PARENT_NAME"),  # parentRelations[].parent
    ("displayName", "ROOM_NAME"),  # parentRelations[].displayName, structures
    ("customName", "CUSTOM_NAME"),
    ("customId", "CUSTOM_ID"),
    ("structureId", "STRUCTURE_ID"),
    ("structureName", "STRUCTURE_NAME"),
)

# Defense-in-depth: after redaction, the serialized JSON must NOT contain
# any of these patterns. If it does, the redaction registry has a gap.
_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"enterprises/[A-Za-z0-9][A-Za-z0-9_-]+", "raw enterprises/{project_id} path"),
    # SDM device ids look like ULID-ish AWAA... strings; structure ids are
    # similarly opaque. Any uppercase-heavy 16+ char [A-Z0-9_-] run that
    # survived redaction is suspicious.
    (r"\bAWA[A-Z0-9_-]{12,}\b", "SDM-style opaque device id"),
)


class RedactionError(RuntimeError):
    """Raised when post-redaction scan finds a residual real identifier."""


class _Redactor:
    """Deterministic, key-driven redaction with stable per-class numbering.

    ``redact(obj)`` returns a deep-copied ``obj`` with PII replaced.
    The same input value always maps to the same placeholder within an
    instance lifetime, so calling ``redact`` on the list response and then
    on each ``get`` response yields stable cross-references (e.g. the
    device id used in ``devices_list.json`` matches the slug used to name
    the per-device fixture file).
    """

    def __init__(self) -> None:
        # (class_name, original_value) -> placeholder
        self._mapping: dict[tuple[str, str], str] = {}
        # class_name -> next index
        self._counters: dict[str, int] = {}

    def _placeholder_for(self, class_name: str, original: str) -> str:
        key = (class_name, original)
        if key in self._mapping:
            return self._mapping[key]
        idx = self._counters.get(class_name, 0) + 1
        self._counters[class_name] = idx
        placeholder = f"{{{{{class_name}_{idx}}}}}"
        self._mapping[key] = placeholder
        return placeholder

    def _classify(self, key: str) -> str | None:
        """Return the redaction class name for ``key``, or ``None`` if not PII."""
        for needle, class_name in _REDACTION_KEYS:
            if needle in key:
                return class_name
        return None

    def redact(self, obj: Any) -> Any:
        """Recursively redact ``obj`` (dict / list / scalar) and return a copy."""
        if isinstance(obj, dict):
            # Sort keys to make traversal — and therefore counter assignment —
            # deterministic across runs.
            return {k: self._redact_value(k, obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, list):
            return [self.redact(item) for item in obj]
        return obj

    def _redact_value(self, key: str, value: Any) -> Any:
        cls = self._classify(key)
        if cls is not None and isinstance(value, str):
            return self._placeholder_for(cls, value)
        # Even if this key isn't classified, recurse — the PII might be nested.
        return self.redact(value)

    def device_slug(self, device_name: str) -> str:
        """Stable slug for ``device.name`` suitable as a filename component.

        Reuses the same placeholder mapping as the in-fixture redaction so
        ``devices_get_DEVICE_NAME_1.json`` lines up with the placeholder
        used inside ``devices_list.json`` for that device.
        """
        placeholder = self._placeholder_for("DEVICE_NAME", device_name)
        # placeholder is "{{DEVICE_NAME_N}}" — strip braces for filenames.
        return placeholder.strip("{}").lower()


def _scan_for_leaks(serialized: str) -> list[str]:
    """Return a list of human-readable reasons the serialized output is unsafe."""
    failures: list[str] = []
    for pattern, description in _LEAK_PATTERNS:
        match = re.search(pattern, serialized)
        if match:
            failures.append(f"{description!s}: matched {match.group(0)!r}")
    return failures


def _write_fixture(path: Path, payload: Any) -> None:
    """Serialize ``payload`` to ``path`` after a leak scan. Raises on leak."""
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    leaks = _scan_for_leaks(serialized)
    if leaks:
        raise RedactionError(
            "post-redaction leak scan failed:\n  - " + "\n  - ".join(leaks)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


# --- SDM client -------------------------------------------------------------

_SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"
_SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"


def _run_oauth_flow(client_secret_path: Path, callback_port: int) -> str:
    """Run the OAuth Desktop installed-app flow and return an access token.

    Imports are deferred to here so ``--help`` works without
    ``google-auth-oauthlib`` installed.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            "error: google-auth-oauthlib is required for the cam smoke flow.\n"
            "       install it with:  uv pip install google-auth-oauthlib requests",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_path),
        scopes=[_SDM_SCOPE],
    )
    credentials = flow.run_local_server(port=callback_port)
    if not credentials.token:
        raise SystemExit(2)
    return str(credentials.token)


def _http_get(url: str, access_token: str) -> dict[str, Any]:
    """GET ``url`` with the SDM bearer token; return parsed JSON.

    Imports are deferred so ``--help`` does not require ``requests``.
    """
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            "error: requests is required for the cam smoke flow.\n"
            "       install it with:  uv pip install requests",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code >= 400:
        print(
            f"error: SDM API returned HTTP {response.status_code} for {url}\n"
            f"       body: {response.text[:500]}",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return dict(response.json())


# --- Main -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke-cam.py",
        description=(
            "Phase 0 SDM onboarding smoke test for nest-cli. Drives the OAuth "
            "Desktop flow, calls devices.list + devices.get for every device "
            "the operator's account is authorized to see, and writes "
            "redacted fixtures."
        ),
    )
    parser.add_argument(
        "--client-secret-json",
        required=True,
        type=Path,
        help="Path to the OAuth client_secret JSON downloaded from Google Cloud Console.",
    )
    parser.add_argument(
        "--google-cloud-project-id",
        required=True,
        help="The Device Access Console project id (NOT the Google Cloud project number).",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("tests/fixtures/sdm/captured"),
        help="Directory to write redacted fixtures into (default: tests/fixtures/sdm/captured).",
    )
    parser.add_argument(
        "--callback-port",
        type=int,
        default=8765,
        help="Local TCP port for the OAuth callback listener (default: 8765).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.client_secret_json.is_file():
        print(
            f"error: --client-secret-json path does not exist: {args.client_secret_json}",
            file=sys.stderr,
        )
        return 64

    access_token = _run_oauth_flow(args.client_secret_json, args.callback_port)

    list_url = f"{_SDM_BASE}/enterprises/{args.google_cloud_project_id}/devices"
    list_response = _http_get(list_url, access_token)
    devices = list_response.get("devices", [])

    redactor = _Redactor()

    # Per-device GETs first — this populates the redactor's mapping so the
    # aggregate list fixture writes with the same placeholder identities.
    summary_rows: list[tuple[str, str, int]] = []
    try:
        for device in devices:
            device_name = device.get("name", "")
            if not isinstance(device_name, str) or not device_name:
                print(
                    f"error: device entry missing 'name' field: {device!r}",
                    file=sys.stderr,
                )
                return 3
            slug = redactor.device_slug(device_name)
            get_url = f"{_SDM_BASE}/{device_name}"
            get_response = _http_get(get_url, access_token)
            redacted_get = redactor.redact(get_response)
            _write_fixture(
                args.fixtures_dir / f"devices_get_{slug}.json",
                redacted_get,
            )
            summary_rows.append(
                (
                    slug,
                    str(device.get("type", "")),
                    len(get_response.get("traits", {})),
                )
            )

        # Now write the aggregate list with the same redactor instance.
        redacted_list = redactor.redact(list_response)
        _write_fixture(args.fixtures_dir / "devices_list.json", redacted_list)
    except RedactionError as exc:
        print(f"error: redaction veto: {exc}", file=sys.stderr)
        return 4

    # Stdout summary (slug only — no real ids).
    print(f"captured {len(devices)} device(s) into {args.fixtures_dir}/")
    print(f"{'slug':<32} {'type':<48} {'traits':>7}")
    print("-" * 90)
    for slug, dev_type, trait_count in summary_rows:
        print(f"{slug:<32} {dev_type:<48} {trait_count:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
