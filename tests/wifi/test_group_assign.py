"""CliRunner tests for ``nest-cli wifi group-assign`` (FR-WIFI-7).

Coverage:

- Each of the four group choices (family / parental / guest / none) is accepted.
- Case-insensitive `--group FAMILY` works.
- Missing `--group` flag → Click usage error.
- Phase B status: the action verb has not yet been mapped onto the
  Foyer gRPC surface, so the verb exits 5 (unsupported_feature,
  family=wifi) with a hint pointing at the Phase-C deferral.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.wifi_cmd import wifi_group


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=2,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


# ---------------------------------------------------------------------------
# Click validation: --group choice + case-insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_value", ["family", "parental", "guest", "none"])
def test_group_assign_accepts_each_choice_value(
    group_value: str, isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """All four `--group` choices reach the FoyerClient layer.

    Each one currently exits 5 (upstream googlewifi gap) but only AFTER
    Click successfully parses the choice. Exit-5 with family=wifi proves
    the choice was accepted by Click and rejected by the FoyerClient
    deliberately, not silently coerced.
    """
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            group_value,
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    # Phase B status (2026-05-03): the FoyerClient action verb has not
    # yet been mapped onto a Foyer gRPC RPC; exit 5
    # (EXIT_UNSUPPORTED_FEATURE) is the documented posture, deferred to
    # Phase C.
    assert result.exit_code == 5, f"got {result.exit_code}: {result.output}"
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    # Hint should reference the Phase-C deferral (was: "googlewifi" gap).
    hint = (payload.get("hint") or "").lower()
    assert "phase" in hint or "foyer" in hint or "deferred" in hint


def test_group_assign_case_insensitive(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`--group FAMILY` is accepted just like `--group family`."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            "FAMILY",
            "--experimental-wifi",
        ],
    )
    # Same exit-5 path as the lowercase form proves Click accepted the
    # uppercase variant.
    assert result.exit_code == 5, result.output


def test_group_assign_invalid_choice_rejected(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`--group enterprise` (not in the choice set) → Click usage error."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            "enterprise",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "enterprise" in err.lower() or "invalid" in err.lower() or "choice" in err.lower()


def test_group_assign_missing_group_flag_rejected(isolated_xdg: Path) -> None:
    """Omitting `--group` → Click usage error (the option is required)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "group" in err.lower() or "missing" in err.lower() or "required" in err.lower()
