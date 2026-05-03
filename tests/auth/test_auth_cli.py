"""Click ``CliRunner`` tests for the ``auth`` subgroup.

Coverage map (FR → test):

- FR-CRED-1 (interactive setup): test_setup_first_invocation_succeeds.
- FR-CRED-2 (refuse-to-overwrite): test_setup_refuses_to_overwrite,
  test_setup_overwrite_flag_succeeds.
- FR-CRED-4 (force refresh): test_refresh_rotates_token.
- FR-CRED-5 (revoke): test_revoke_calls_google_and_scrubs,
  test_revoke_requires_yes_in_non_tty.
- FR-CRED-10 (status redacts secrets): test_status_json_redacts_client_id,
  test_status_text_renders_human_readable, test_status_no_credentials,
  test_status_after_revoke_reads_empty_stub.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth import credentials as cred_mod
from nest_cli.auth.credentials import (
    GOOGLE_OAUTH_REVOKE_URL,
    GOOGLE_OAUTH_TOKEN_URL,
    default_credentials_path,
    save_credentials,
)
from nest_cli.auth.types import CamCredentials
from nest_cli.cli import auth_cmd as auth_cmd_mod
from nest_cli.cli.auth_cmd import auth_group

# ---------------------------------------------------------------------------
# Shared fixtures + helpers
# ---------------------------------------------------------------------------


def _make_creds(
    *,
    expires_at: datetime | None = None,
    access_token: str = "access-token-on-disk",
    client_id: str = "abcdefgh12345678.apps.googleusercontent.com",
) -> CamCredentials:
    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj-id",
        oauth_client_id=client_id,
        oauth_client_secret="secret-on-disk",
        refresh_token="refresh-on-disk",
        access_token=access_token,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)),
    )


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``default_credentials_path`` at a writable tmp dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return default_credentials_path()


def _stub_post_form(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, bytes]]) -> None:
    """Replace ``credentials._post_form`` with a deterministic stub."""

    def fake_post(url: str, params: dict[str, str]) -> tuple[int, bytes]:
        if url not in mapping:
            raise AssertionError(f"unexpected POST to {url}")
        return mapping[url]

    monkeypatch.setattr(cred_mod, "_post_form", fake_post)


def _stub_run_oauth_flow(monkeypatch: pytest.MonkeyPatch, creds: CamCredentials) -> None:
    """Replace the OAuth flow with a deterministic factory."""

    def fake_flow(
        client_id: str,
        client_secret: str,
        project_id: str,
        *,
        callback_port: int,
        open_browser: bool,
    ) -> CamCredentials:
        # Honor the prompted client_id / client_secret / project_id so the
        # post-flow on-disk state reflects what the operator typed.
        return CamCredentials(
            version=1,
            type="oauth",
            google_cloud_project_id=project_id,
            oauth_client_id=client_id,
            oauth_client_secret=client_secret,
            refresh_token=creds.refresh_token,
            access_token=creds.access_token,
            expires_at=creds.expires_at,
        )

    monkeypatch.setattr(auth_cmd_mod, "run_oauth_flow", fake_flow)


# ---------------------------------------------------------------------------
# auth setup
# ---------------------------------------------------------------------------


def test_setup_first_invocation_succeeds(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh setup persists credentials with mode 0o600."""
    _stub_run_oauth_flow(monkeypatch, _make_creds())
    runner = CliRunner()
    result = runner.invoke(
        auth_group,
        ["setup"],
        # Three lines of input: project_id, client_id, client_secret (hidden).
        input="my-project\nmy-client-id\nmy-secret\n",
    )
    assert result.exit_code == 0, result.output
    assert isolated_xdg.exists()
    mode = stat.S_IMODE(isolated_xdg.stat().st_mode)
    assert mode == 0o600
    payload = json.loads(isolated_xdg.read_text())
    assert payload["google_cloud_project_id"] == "my-project"
    assert payload["oauth_client_id"] == "my-client-id"


def test_setup_refuses_to_overwrite(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-CRED-2: a second invocation without --overwrite exits 2."""
    save_credentials(isolated_xdg, _make_creds())
    _stub_run_oauth_flow(monkeypatch, _make_creds())
    runner = CliRunner()
    result = runner.invoke(auth_group, ["setup"])
    assert result.exit_code == 2, result.output
    # Stderr error record names the remediation.
    assert "overwrite" in (result.stderr or "")


def test_setup_overwrite_flag_succeeds(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--overwrite` clobbers an existing file."""
    save_credentials(isolated_xdg, _make_creds())
    new_creds = _make_creds(access_token="rotated-during-overwrite")
    _stub_run_oauth_flow(monkeypatch, new_creds)
    runner = CliRunner()
    result = runner.invoke(
        auth_group,
        ["setup", "--overwrite"],
        input="proj2\nclient2\nsecret2\n",
    )
    assert result.exit_code == 0, result.output
    on_disk = json.loads(isolated_xdg.read_text())
    assert on_disk["access_token"] == "rotated-during-overwrite"
    assert on_disk["oauth_client_id"] == "client2"


def test_setup_help_names_fr_credentials() -> None:
    """``--help`` text MUST name FR-CRED-1 / FR-CRED-2 per task brief."""
    runner = CliRunner()
    result = runner.invoke(auth_group, ["setup", "--help"])
    assert result.exit_code == 0
    # The verb's docstring references both FRs.
    assert "FR-CRED-1" in result.output
    assert "FR-CRED-2" in result.output


# ---------------------------------------------------------------------------
# auth refresh
# ---------------------------------------------------------------------------


def test_refresh_rotates_token(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh verb replaces the access token in-place."""
    save_credentials(isolated_xdg, _make_creds(access_token="OLD"))
    body = json.dumps({"access_token": "NEW", "expires_in": 1800, "token_type": "Bearer"}).encode()
    _stub_post_form(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (200, body)})
    runner = CliRunner()
    result = runner.invoke(auth_group, ["refresh"])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(isolated_xdg.read_text())
    assert on_disk["access_token"] == "NEW"


def test_refresh_propagates_exit_2_on_4xx(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked refresh token surfaces as exit 2 with a structured stderr.

    In ``--json`` mode the SRD §11.3 envelope carries the ``auth_failed``
    enum string. Text mode emits a human-readable line with no enum
    keyword (reserved for the JSON envelope).
    """
    save_credentials(isolated_xdg, _make_creds())
    _stub_post_form(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (400, b'{"error":"invalid_grant"}')})
    runner = CliRunner()
    result = runner.invoke(auth_group, ["refresh", "--json"])
    assert result.exit_code == 2, result.output
    stderr = result.stderr or ""
    assert "auth_failed" in stderr
    envelope = json.loads(stderr.strip().splitlines()[-1])
    assert envelope["error"] == "auth_failed"
    assert envelope["exit_code"] == 2
    # FR-CRED-10 cleanup: the ``family`` discriminator does NOT belong in
    # the §11.3 error envelope (reserved for the auth status payload).
    assert "family" not in envelope


# ---------------------------------------------------------------------------
# auth revoke
# ---------------------------------------------------------------------------


def test_revoke_calls_google_and_scrubs(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful revoke posts to Google and replaces the file with ``{}``."""
    save_credentials(isolated_xdg, _make_creds())
    _stub_post_form(monkeypatch, {GOOGLE_OAUTH_REVOKE_URL: (200, b"")})
    runner = CliRunner()
    result = runner.invoke(auth_group, ["revoke", "--yes"])
    assert result.exit_code == 0, result.output
    # File still exists, but contents are the empty stub.
    assert isolated_xdg.read_text().strip() == "{}"
    mode = stat.S_IMODE(isolated_xdg.stat().st_mode)
    assert mode == 0o600


def test_revoke_interactive_confirmation(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In tty contexts (CliRunner default), the prompt accepts ``y\\n``."""
    save_credentials(isolated_xdg, _make_creds())
    _stub_post_form(monkeypatch, {GOOGLE_OAUTH_REVOKE_URL: (200, b"")})
    runner = CliRunner()
    result = runner.invoke(auth_group, ["revoke"], input="y\n")
    assert result.exit_code == 0, result.output
    assert isolated_xdg.read_text().strip() == "{}"


def test_revoke_aborts_on_confirmation_no(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator answering ``n`` leaves credentials intact and exits 0."""
    save_credentials(isolated_xdg, _make_creds())
    runner = CliRunner()
    result = runner.invoke(auth_group, ["revoke"], input="n\n")
    assert result.exit_code == 0, result.output
    on_disk = json.loads(isolated_xdg.read_text())
    assert on_disk.get("refresh_token") == "refresh-on-disk"  # untouched


# ---------------------------------------------------------------------------
# auth status
# ---------------------------------------------------------------------------


def test_status_json_redacts_client_id(
    isolated_xdg: Path,
) -> None:
    """JSON output emits redacted client_id; never the secret/refresh/access tokens.

    FR-CRED-10: ``auth status --json`` emits a JSON array of two records
    (one per family) so the operator has a single place to see "what is
    this CLI authorized to do" (Decision 22). The wifi entry reports
    ``configured=false`` when no wifi credentials file exists.
    """
    save_credentials(
        isolated_xdg,
        _make_creds(client_id="abcdefgh12345678.apps.googleusercontent.com"),
    )
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list), "FR-CRED-10: --json output must be an array"
    assert len(payload) == 2
    cam_record = next(r for r in payload if r["family"] == "cam")
    wifi_record = next(r for r in payload if r["family"] == "wifi")
    assert cam_record["configured"] is True
    assert wifi_record["configured"] is False  # never set up in this test
    # Client id ends in the trailing 8 chars of the input.
    assert cam_record["oauth_client_id_redacted"].endswith(
        "abcdefgh12345678.apps.googleusercontent.com"[-8:]
    )
    # Critical: never leak the real secrets in the rendered output.
    assert "secret-on-disk" not in result.output
    assert "refresh-on-disk" not in result.output
    assert "access-token-on-disk" not in result.output


def test_status_text_renders_human_readable(isolated_xdg: Path) -> None:
    """Text mode prints labeled lines, not JSON."""
    save_credentials(isolated_xdg, _make_creds())
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status"])
    assert result.exit_code == 0, result.output
    assert "family: cam" in result.output
    assert "configured: true" in result.output
    assert "expires_at:" in result.output
    # Should not be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_status_no_credentials(isolated_xdg: Path) -> None:
    """Status when no file exists reports configured=false (does NOT exit 2)."""
    assert not isolated_xdg.exists()
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    cam_record = next(r for r in payload if r["family"] == "cam")
    wifi_record = next(r for r in payload if r["family"] == "wifi")
    assert cam_record["configured"] is False
    assert wifi_record["configured"] is False


def test_status_after_revoke_reads_empty_stub(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``{}`` stub (post-revoke) shows configured=false with a note."""
    isolated_xdg.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    isolated_xdg.write_text("{}")
    os.chmod(isolated_xdg, 0o600)
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    cam_record = next(r for r in payload if r["family"] == "cam")
    assert cam_record["configured"] is False
    assert "revoked" in cam_record.get("note", "")


def test_status_jsonl_emits_one_object_per_line(isolated_xdg: Path) -> None:
    """``--jsonl`` mode emits the array elements one per line.

    Phase 3 emits two records (cam + wifi). Each lands on its own line.
    """
    save_credentials(isolated_xdg, _make_creds())
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--jsonl"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 2
    families = {json.loads(line)["family"] for line in lines}
    assert families == {"cam", "wifi"}


def test_status_quiet_suppresses_stdout(isolated_xdg: Path) -> None:
    """``--quiet`` mode emits no stdout; exit code is the only signal.

    Newly enabled by the migration to ``add_output_options``.
    """
    save_credentials(isolated_xdg, _make_creds())
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--quiet"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_status_loose_chmod_exits_2(isolated_xdg: Path) -> None:
    """A 0644 credentials file refuses to load; status emits exit 2."""
    save_credentials(isolated_xdg, _make_creds())
    os.chmod(isolated_xdg, 0o644)
    runner = CliRunner()
    result = runner.invoke(auth_group, ["status"])
    assert result.exit_code == 2, result.output
