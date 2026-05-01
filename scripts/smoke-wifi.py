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

Redaction
---------
Every string field whose key matches one of the registered PII classes
(``id``, ``mac``, ``serialNumber``, ``displayName``, ``friendlyName``,
``email``, ``psk``, ``passphrase``, ``wanIpAddress``) is replaced with a
deterministic placeholder of the form ``{{REDACTED_<KEY>_<N>}}``. The same
input value always maps to the same placeholder within a run, so fixture
diffs across runs are stable and per-fixture cross-references survive.

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
- 1  upstream library error (Foyer rotation, etc.)
- 2  ImportError on ``glocaltokens`` / ``googlewifi`` (operator must install
     the optional ``[wifi]`` extra)
- 64 Usage error (bad args)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# Substring matches against the dict KEY (case-insensitive). Order is
# significant only for the placeholder class name in the output: the FIRST
# match wins. Put the more-specific matches first.
_REDACTION_KEYS: tuple[str, ...] = (
    "wanIpAddress",
    "passphrase",
    "psk",
    "serialNumber",
    "displayName",
    "friendlyName",
    "email",
    "mac",
    "id",
)


class _Redactor:
    """Deterministic key-driven redactor for Foyer-style payloads.

    ``redact(obj)`` returns a deep-copied ``obj`` with PII replaced by
    ``{{REDACTED_<KEY>_<N>}}`` placeholders. Same input always maps to
    the same placeholder within a run.
    """

    def __init__(self) -> None:
        self._mapping: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    def _classify(self, key: str) -> str | None:
        lowered = key.lower()
        for needle in _REDACTION_KEYS:
            if needle.lower() in lowered:
                return needle.upper()
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
        if cls is not None and isinstance(value, str):
            return self._placeholder(cls, value)
        return self.redact(value)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
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
    return str(args.master_token)


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

    GLocalAuthenticationTokens, GoogleWifi = _import_wifi_libraries()

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
    except Exception as exc:  # noqa: BLE001 — operator-facing, want full surface
        print(
            f"error: upstream wifi library raised: {type(exc).__name__}: {exc}\n"
            "       this is the documented Foyer rotation risk (SRD §3.2.3).\n"
            "       check googlewifi / glocaltokens issue trackers for current state.",
            file=sys.stderr,
        )
        return 1

    redactor = _Redactor()
    redacted_groups = redactor.redact(_to_jsonable(groups))
    redacted_aps = redactor.redact(_to_jsonable(access_points))
    redacted_devices = redactor.redact(_to_jsonable(devices))

    _write_fixture(args.fixtures_dir / "groups.json", redacted_groups)
    _write_fixture(args.fixtures_dir / "access_points.json", redacted_aps)
    _write_fixture(args.fixtures_dir / "devices.json", redacted_devices)

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
