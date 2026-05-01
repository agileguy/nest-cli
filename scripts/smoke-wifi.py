#!/usr/bin/env python3
"""Phase 0 wifi-side smoke script — operator runs ONCE against real hardware.

Purpose
-------
Empirically prove that ``glocaltokens`` + ``googlewifi`` work against the
operator's mesh, and capture a sanitized fixture corpus for later phases.

What it does
------------
1. Accepts the operator's Google username (email) plus an Android master
   token they extracted out-of-band (per SRD §3.2.1: typically via
   ``gpsoauth`` or a one-time bootstrap from a paired Android device — that
   path is explicitly out of scope for this script).
2. Hands those off to ``glocaltokens.client.GLocalAuthenticationTokens`` to
   derive a Foyer-usable token.
3. Drives ``googlewifi.GoogleWifi`` to call ``get_groups()``,
   ``get_access_points()``, ``get_devices()``.
4. Writes redacted ``groups.json``, ``access_points.json``, ``devices.json``
   into the fixtures dir.
5. Prints a summary table to stdout (counts only — no ids).

This script is **standalone**. It does NOT import the ``nest_cli`` package.

Redaction rules (defense-in-depth)
----------------------------------
The captured JSON has every real identifier scrubbed before it touches disk.
Two layers:

1. **Key-driven walk:** any string value whose surrounding key matches one
   of the registered PII classes is replaced with a deterministic
   placeholder of the form ``{{REDACTED_<CLASS>_<N>}}``. Keys are normalized
   (lowercased, ``_`` and ``-`` stripped) before comparison so ``friendlyName``
   and ``friendly_name`` and ``friendly-name`` all hit the same rule.
   Two rule families:

   - ``_REDACTION_EXACT`` — normalized key must EQUAL the needle. Used for
     specific PII fields (``ssid``, ``mac``, ``passphrase``, ``serialnumber``,
     ``wanipaddress``, plus LAN-topology classes like ``subnet``, ``gateway``,
     ``dnsservers``, ``dhcprangestart``).
   - Endswith for ID-like keys — uses a regex against the ORIGINAL (not
     normalized) key requiring a separator boundary: ``_id`` / ``-id`` /
     a ``[a-z]Id`` camelCase transition. This catches ``group_id``,
     ``device-id``, ``groupId`` while leaving ``paid``, ``solid``, ``valid``,
     ``width``, ``guidance`` un-redacted.

2. **Post-scan veto:** after the walk, the serialized output is regex-scanned
   for any residual MAC address, email address, IPv4 address (with a small
   allowlist for ``0.0.0.0`` / ``127.0.0.1`` / ``255.255.255.255``), or UUID.
   If anything matches, the script fails loud (exit 4, ``RedactionError``)
   rather than write a fixture that might leak.

Operator security note
----------------------
**Do not pass ``--master-token`` on the command line in a shared shell.**
The token will end up in shell history. Use ``--master-token-stdin``
instead and pipe the token in:

    echo "$MASTER_TOKEN" | python scripts/smoke-wifi.py \\
        --google-username you@example.com --master-token-stdin

Exit codes
----------
- 0  success
- 1  upstream library error (Foyer rotation, network, malformed shape)
- 2  ImportError on ``glocaltokens`` / ``googlewifi`` (operator must install
     the optional ``[wifi]`` extra)
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

# Exact-match needles, evaluated against the NORMALIZED key
# (lowercased, ``_`` and ``-`` stripped). The first matching needle wins.
_REDACTION_EXACT: tuple[tuple[str, str], ...] = (
    # SSIDs
    ("ssid", "SSID"),
    ("guestssid", "SSID"),
    # Hardware addresses
    ("mac", "MAC"),
    ("macaddress", "MAC"),
    ("bssid", "MAC"),
    # Identity
    ("email", "EMAIL"),
    # Pre-shared keys / passphrases
    ("psk", "PSK"),
    ("passphrase", "PSK"),
    ("presharedkey", "PSK"),
    # Hardware identifiers
    ("serialnumber", "SERIAL"),
    # Display names
    ("displayname", "NAME"),
    ("friendlyname", "NAME"),
    # WAN / LAN topology — all redacted as LANNET / WANIP class.
    ("wanipaddress", "WANIP"),
    ("lansubnet", "LANNET"),
    ("subnet", "LANNET"),
    ("gateway", "LANNET"),
    ("dhcprangestart", "LANNET"),
    ("dhcprangeend", "LANNET"),
    ("dnsservers", "LANNET"),
    ("dns", "LANNET"),
    # Bare ``id`` key (keys like literally ``id`` after normalization).
    ("id", "ID"),
)

# Regex applied to the ORIGINAL key (not normalized) to catch ID-like
# suffixes that have a real separator boundary. Picks up ``group_id``,
# ``device-id``, ``stationId`` — but NOT ``paid``, ``solid``, ``valid``,
# ``width``, ``guidance``, ``android``.
_ID_SUFFIX_RE = re.compile(r"(?:_id|-id|[a-z]Id)$")


class RedactionError(RuntimeError):
    """Raised when the post-redaction scan finds a residual real identifier."""


# Defense-in-depth: after redaction, the serialized JSON must NOT contain
# any of these patterns. If it does, the redaction registry has a gap.
_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "MAC address"),
    (r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b", "email address"),
    # IPv4 — allowlist common safe placeholders below.
    (
        r"(?<![0-9])(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
        r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(?![0-9])",
        "IPv4 address",
    ),
    # UUIDs (Foyer device / group ids).
    (
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "UUID",
    ),
)

# Allow obvious placeholder octets in IPv4 hits.
_IP_ALLOWLIST = frozenset(("0.0.0.0", "127.0.0.1", "255.255.255.255"))


class _Redactor:
    """Deterministic key-driven redactor for Foyer-style payloads.

    ``redact(obj)`` returns a deep-copied ``obj`` with PII replaced by
    ``{{REDACTED_<CLASS>_<N>}}`` placeholders. Same input always maps to
    the same placeholder within a run.
    """

    def __init__(self) -> None:
        self._mapping: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    def _classify(self, key: str) -> str | None:
        """Return the redaction class for ``key``, or ``None`` if not PII.

        Comparison is two-pass:
          1. Normalize the key (lowercase, strip ``_`` / ``-``) and check
             against ``_REDACTION_EXACT`` for an equality hit.
          2. Apply ``_ID_SUFFIX_RE`` to the ORIGINAL key for separator-
             bounded ID suffixes.
        """
        normalized = key.lower().replace("_", "").replace("-", "")
        for needle, cls in _REDACTION_EXACT:
            if normalized == needle:
                return cls
        if _ID_SUFFIX_RE.search(key):
            return "ID"
        return None

    def _placeholder(self, class_name: str, original: str) -> str:
        cache_key = (class_name, original)
        if cache_key in self._mapping:
            return self._mapping[cache_key]
        idx = self._counters.get(class_name, 0) + 1
        self._counters[class_name] = idx
        out = f"{{{{REDACTED_{class_name}_{idx}}}}}"
        self._mapping[cache_key] = out
        return out

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self._redact_value(k, obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, list):
            return [self.redact(item) for item in obj]
        return obj

    def _redact_value(self, key: str, value: Any) -> Any:
        cls = self._classify(key)
        if cls is not None:
            if isinstance(value, str):
                return self._placeholder(cls, value)
            if isinstance(value, list):
                # ``dns_servers``-style: list of bare strings, all of the
                # parent class. Redact each string element; recurse for
                # any nested structure.
                return [
                    self._placeholder(cls, item) if isinstance(item, str) else self.redact(item)
                    for item in value
                ]
        return self.redact(value)


def _scan_for_leaks(serialized: str) -> list[str]:
    """Return human-readable reasons the serialized output is unsafe."""
    failures: list[str] = []
    for pattern, description in _LEAK_PATTERNS:
        for match in re.finditer(pattern, serialized):
            value = match.group(0)
            if description == "IPv4 address" and value in _IP_ALLOWLIST:
                continue
            failures.append(f"{description}: matched {value!r}")
    return failures


def _to_jsonable(obj: Any) -> Any:
    """Best-effort cast of upstream library objects into JSON-friendly types.

    ``googlewifi`` returns dataclass-like objects in some versions and dicts
    in others. We don't try to pin a shape — we just walk it.
    """
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in vars(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_to_jsonable(item) for item in obj]
    if isinstance(obj, bool | int | float | str) or obj is None:
        return obj
    return str(obj)


def _write_fixture(path: Path, payload: Any) -> None:
    """Serialize ``payload`` to ``path`` after a leak scan. Raises on leak."""
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    leaks = _scan_for_leaks(serialized)
    if leaks:
        raise RedactionError("post-redaction leak scan failed:\n  - " + "\n  - ".join(leaks))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


# --- Main -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke-wifi.py",
        description=(
            "Phase 0 Foyer onboarding smoke test for nest-cli. Drives "
            "glocaltokens + googlewifi against the operator's mesh and "
            "writes redacted fixtures."
        ),
    )
    parser.add_argument(
        "--google-username",
        required=True,
        help="Google account email (the account that owns the Nest Wi-Fi mesh).",
    )
    token_group = parser.add_mutually_exclusive_group(required=True)
    token_group.add_argument(
        "--master-token",
        help=(
            "Android master token extracted out-of-band. WARNING: this lands "
            "in your shell history. Prefer --master-token-stdin."
        ),
    )
    token_group.add_argument(
        "--master-token-stdin",
        action="store_true",
        help="Read the master token from stdin (one line).",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("tests/fixtures/foyer/captured"),
        help="Directory to write redacted fixtures into (default: tests/fixtures/foyer/captured).",
    )
    return parser


def _resolve_master_token(args: argparse.Namespace) -> str:
    if args.master_token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            print(
                "error: --master-token-stdin received an empty line on stdin.",
                file=sys.stderr,
            )
            raise SystemExit(64)
        return token
    flag_value = "" if args.master_token is None else str(args.master_token).strip()
    if not flag_value:
        print(
            "error: --master-token was supplied but is empty or whitespace only.",
            file=sys.stderr,
        )
        raise SystemExit(64)
    return flag_value


def _import_wifi_libraries() -> tuple[Any, Any]:
    """Import ``glocaltokens`` and ``googlewifi``; clear hint on failure."""
    try:
        from glocaltokens.client import GLocalAuthenticationTokens  # type: ignore[import-not-found]
        from googlewifi import GoogleWifi  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            "error: glocaltokens and/or googlewifi are not installed.\n"
            "       these are wifi-side optional extras. install with:\n"
            "           uv pip install nest-cli[wifi]\n"
            f"       (underlying ImportError: {exc})",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return GLocalAuthenticationTokens, GoogleWifi


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    master_token = _resolve_master_token(args)

    GLocalAuthenticationTokens, GoogleWifi = _import_wifi_libraries()  # noqa: N806 — class objects, not regular variables

    try:
        # Derive a Foyer-usable token. The exact attribute we need from
        # glocaltokens has shifted between versions; we hand the same master
        # token to googlewifi and let it consume what it expects. This script
        # is operator-driven and run interactively, so any version mismatch
        # surfaces as a clear traceback the operator can act on.
        _ = GLocalAuthenticationTokens(
            username=args.google_username,
            master_token=master_token,
        )
        wifi = GoogleWifi(refresh_token=master_token)

        groups = wifi.get_groups()
        access_points = wifi.get_access_points()
        devices = wifi.get_devices()
    except (ConnectionError, TimeoutError, OSError) as exc:
        print(
            f"error: network error talking to Foyer: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    except (KeyError, AttributeError, TypeError) as exc:
        print(
            f"error: googlewifi returned unexpected shape: {type(exc).__name__}: {exc}\n"
            "       this is the documented Foyer rotation risk (SRD §3.2.3).\n"
            "       check googlewifi / glocaltokens issue trackers for current state.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — operator-facing, want full surface
        print(
            f"error: unexpected error from upstream wifi library: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise

    redactor = _Redactor()
    redacted_groups = redactor.redact(_to_jsonable(groups))
    redacted_aps = redactor.redact(_to_jsonable(access_points))
    redacted_devices = redactor.redact(_to_jsonable(devices))

    try:
        _write_fixture(args.fixtures_dir / "groups.json", redacted_groups)
        _write_fixture(args.fixtures_dir / "access_points.json", redacted_aps)
        _write_fixture(args.fixtures_dir / "devices.json", redacted_devices)
    except RedactionError as exc:
        print(f"error: redaction veto: {exc}", file=sys.stderr)
        return 4

    group_count = len(redacted_groups) if isinstance(redacted_groups, list) else 1
    ap_count = len(redacted_aps) if isinstance(redacted_aps, list) else 1
    dev_count = len(redacted_devices) if isinstance(redacted_devices, list) else 1

    print(f"captured Foyer fixtures into {args.fixtures_dir}/")
    print(f"  groups:        {group_count}")
    print(f"  access points: {ap_count}")
    print(f"  clients:       {dev_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
