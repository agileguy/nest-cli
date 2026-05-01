"""Tests for ``nest_cli.cli.list_cmd`` — list and discover commands.

Covers FR-1..1d (list) and FR-2/2a/3 (discover). All HTTP mocked via
``responses``; credentials path mocked via fixture-temp dirs and
monkeypatched ``default_credentials_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

# Import submodules explicitly so monkeypatch can resolve string paths
# like "nest_cli.cli.list_cmd.default_config_path". The package init
# re-exports the Click command object under the same name, which would
# otherwise shadow the submodule.
import nest_cli.cli.cam_cmd  # noqa: F401
import nest_cli.cli.config_cmd  # noqa: F401
import nest_cli.cli.list_cmd  # noqa: F401
from nest_cli.auth.types import CamCredentials
from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_creds(path: Path) -> CamCredentials:
    """Write a fresh-expiry CamCredentials JSON to ``path`` with mode 0600."""
    creds = CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj",
        oauth_client_id="client-id-12345678",
        oauth_client_secret="client-secret",  # noqa: S106
        refresh_token="refresh-tok",  # noqa: S106
        access_token="access-tok",  # noqa: S106
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(creds.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    return creds


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect both config and credentials to the test tmp dir.

    Patches ``default_config_path`` everywhere it's imported AND
    ``default_credentials_path`` everywhere it's imported. Also mocks
    out ``refresh_access_token_if_needed`` and ``save_credentials`` so
    no live OAuth calls happen.
    """
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"

    def _fake_config_path() -> Path:
        return config_path

    def _fake_creds_path() -> Path:
        return credentials_path

    # Patch every import-time binding of ``default_config_path`` and
    # ``default_credentials_path`` so each verb module sees the tmp paths.
    # The canonical home for the credentials path is now
    # ``nest_cli.cli._shared`` (the shared helper used by every verb).
    monkeypatch.setattr("nest_cli.config.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.list_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.cam_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.config_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", _fake_creds_path)

    # Stub refresh + save so no OAuth roundtrip and no real disk write
    # outside our tmp_path.
    def _fake_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", lambda p, c: None)

    return {"config": config_path, "credentials": credentials_path}


# ---------------------------------------------------------------------------
# `list`
# ---------------------------------------------------------------------------


class TestListAliases:
    def test_empty_config_emits_empty_list_in_json(self, fake_paths: dict[str, Path]) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == []

    def test_quiet_suppresses_stdout(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--quiet"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_emits_aliases_in_json(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            'kitchen-cam = "enterprises/proj/devices/def"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload) == 2
        names = {entry["name"] for entry in payload}
        assert names == {"front-door", "kitchen-cam"}
        assert all(entry["family"] == "cam" for entry in payload)

    def test_groups_flag_emits_groups_dict(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\na = "enterprises/p/devices/1"\nb = "enterprises/p/devices/2"\n'
            '[groups]\nall = ["a", "b"]\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--groups", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == {"all": ["a", "b"]}

    def test_family_filter_excludes_other_family(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            'office-mesh = "wifi:groups/g1"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--family", "cam", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "front-door"

    def test_jsonl_emits_one_per_line(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\na = "enterprises/p/devices/1"\nb = "enterprises/p/devices/2"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["list", "--jsonl"])
        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line]
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must not raise


# ---------------------------------------------------------------------------
# `discover`
# ---------------------------------------------------------------------------


class TestDiscover:
    @responses.activate
    def test_lists_devices_from_sdm(self, fake_paths: dict[str, Path]) -> None:
        _write_creds(fake_paths["credentials"])
        sample = json.loads(
            (
                Path(__file__).parent / "fixtures" / "sdm" / "samples" / "sample_indoor_cam.json"
            ).read_text()
        )
        sample["name"] = "enterprises/proj/devices/d1"
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices",
            json={"devices": [sample]},
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["discover", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == 1
        assert payload[0]["target_id"] == "enterprises/proj/devices/d1"

    @responses.activate
    def test_empty_inventory_exits_zero_with_info_log(self, fake_paths: dict[str, Path]) -> None:
        # FR-3: zero result is exit 0, [] on stdout, INFO line on stderr.
        _write_creds(fake_paths["credentials"])
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices",
            json={"devices": []},
            status=200,
        )
        # In Click 8.3+, stdout and stderr are separate by default.
        runner = CliRunner()
        result = runner.invoke(cli_root, ["discover", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        # The "no devices found" INFO line lands on stderr (FR-3).
        assert "no devices found" in result.stderr

    def test_wifi_family_exits_5(self, fake_paths: dict[str, Path]) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_root, ["discover", "--family", "wifi", "--json"])
        assert result.exit_code == 5
