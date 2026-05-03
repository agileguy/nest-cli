"""FR-WIFI-0 gate coverage for the action verbs (pause/unpause/prioritize/group-assign).

Engineer A's `test_experimental_gate.py` covers the gate for the read-only
``wifi list`` verbs. This module covers the four action verbs added in
Phase 3B. Each invocation without ``--experimental-wifi`` MUST exit 64
with a hint that mentions "experimental".

Also asserts ``family="wifi"`` on the gate's structured-error envelope —
the audit posture (SRD §11.3) that wifi-side errors carry the family
discriminator.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group


@pytest.mark.parametrize(
    "argv",
    [
        ["pause", "sta-laptop"],
        ["unpause", "sta-laptop"],
        ["prioritize", "sta-laptop"],
        ["prioritize", "sta-laptop", "--duration", "30"],
        ["group-assign", "sta-laptop", "--group", "family"],
    ],
)
def test_action_verb_without_experimental_flag_exits_64(argv: list[str]) -> None:
    """Each action verb without ``--experimental-wifi`` exits 64."""
    runner = CliRunner()
    result = runner.invoke(wifi_group, argv)
    assert result.exit_code == 64, f"expected 64, got {result.exit_code}: {result.output}"
    err = result.stderr or result.output
    assert "experimental" in err.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["pause", "sta-laptop"],
        ["unpause", "sta-laptop"],
        ["prioritize", "sta-laptop"],
        ["group-assign", "sta-laptop", "--group", "family"],
    ],
)
def test_gate_error_envelope_carries_family_wifi(argv: list[str]) -> None:
    """The FR-WIFI-0 gate's structured-error envelope carries family=wifi."""
    runner = CliRunner()
    result = runner.invoke(wifi_group, [*argv, "--output", "json"])
    assert result.exit_code == 64
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
