"""FR-WIFI-0 — every wifi sub-verb requires --experimental-wifi.

Without the flag, the verb exits 64 with a hint that mentions SRD §3.2.3
(``--help`` + the FR-WIFI-0 contract).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nest_cli.cli.auth_cmd import auth_group
from nest_cli.cli.wifi_cmd import wifi_group


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "groups"],
        ["list", "points", "group-home-001"],
        ["list", "clients", "group-home-001"],
    ],
)
def test_wifi_list_verb_without_experimental_flag_exits_64(argv: list[str]) -> None:
    runner = CliRunner()
    result = runner.invoke(wifi_group, argv)
    assert result.exit_code == 64, f"expected 64, got {result.exit_code}: {result.output}"
    err = result.stderr or result.output
    # The hint MUST name the experimental rationale (SRD §3.2.3 referenced
    # via the constant string the verb attaches to the StructuredError).
    assert "experimental" in err.lower()


def test_wifi_list_groups_with_experimental_flag_proceeds(
    fake_googlewifi: type, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """With --experimental-wifi, the verb proceeds to the credential-load path.

    Even on a fresh tmp dir with no wifi credentials, the verb makes it
    past the gate. The next failure point is "missing wifi credentials"
    (exit 2). The point of THIS test is to prove the gate doesn't
    short-circuit when the flag IS present — not to assert the
    happy-path output (covered by test_list_groups.py).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[arg-type]
    runner = CliRunner()
    result = runner.invoke(wifi_group, ["list", "groups", "--experimental-wifi"])
    # Without credentials, exit 2 — we're past the FR-WIFI-0 gate.
    assert result.exit_code == 2, result.output


def test_wifi_error_envelope_carries_family_wifi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """A wifi-side StructuredError emits ``family: wifi`` in the JSON envelope.

    Trigger the missing-credentials path (exit 2) and inspect the
    structured-error JSON on stderr. ``family`` MUST be present and
    equal ``"wifi"`` (audit recommendation; SRD §11.3 alignment).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[arg-type]
    runner = CliRunner()
    result = runner.invoke(
        wifi_group, ["list", "groups", "--experimental-wifi", "--output", "json"]
    )
    assert result.exit_code == 2
    # Stderr error envelope is JSON in --output json mode.
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_cam_error_envelope_omits_family_back_compat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """Cam-side errors stay without the ``family`` field for v0.1.0/v0.2.x back-compat.

    Trigger the cam-side missing-credentials path (``auth refresh``
    against an empty XDG dir) and confirm the JSON envelope omits
    ``family``. Documented deviation from SRD §11.3 — the cam-side
    retrofit is a follow-up.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[arg-type]
    runner = CliRunner()
    result = runner.invoke(auth_group, ["refresh", "--output", "json"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr or result.output)
    assert "family" not in payload
