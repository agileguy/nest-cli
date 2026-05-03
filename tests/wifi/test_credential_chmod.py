"""FR-CRED-12 — wifi credentials with loose mode raise exit 2.

Mirrors the cam-side chmod check; the only difference is the family
discriminator on the structured-error envelope.
"""

from __future__ import annotations

import json
import os
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


def test_loose_mode_exits_2(isolated_xdg: Path, fake_googlewifi: type) -> None:
    path = default_wifi_credentials_path()
    save_wifi_credentials(
        path,
        WifiCredentials(
            version=2,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )
    os.chmod(path, 0o644)  # group + other readable

    runner = CliRunner()
    result = runner.invoke(
        wifi_group, ["list", "groups", "--experimental-wifi", "--output", "json"]
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
