# Changelog

All notable changes to `nest-cli` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- `README.md` — status block updated to v0.1.0-shipped reality; quick-start
  examples now reflect verbs that actually work; added a link to the
  operator runbook.
- `docs/ONBOARDING.md` (new) — operator runbook: Google Cloud + Device
  Access setup, OAuth client creation, `auth setup` walkthrough,
  smoke-test flow, troubleshooting, where credentials live.
- `docs/ARCHITECTURE.md` — added a Phase 1 implementation map: module
  layout, request flow for cam verbs, output/error contract, threat
  model excerpt, what v0.1.0 deliberately does NOT include.
- `docs/SECURITY.md` — replaced the pre-release contact-email TODO with
  a GitHub Security Advisory link; updated supported-versions table.

## [0.1.0] - 2026-05-01

### Added

- `nest_cli/errors.py` — SRD §11.1 `EXIT_*` constants and the `StructuredError`
  dataclass with a stderr emitter that honors text vs JSON output mode.
- `nest_cli/output.py` — `add_output_options` decorator (`--json`, `--jsonl`,
  `--quiet`, `--output`) plus a mode-aware `emit()` function. Mutually
  exclusive flag combinations exit 64 with a structured error.
- `nest_cli/config.py` — `tomllib`-based TOML parser for the local config
  with `[aliases]` and `[groups]` sections (extra="forbid"), plus
  `default_config_path()` honoring `XDG_CONFIG_HOME`.
- `nest_cli/sdm/types.py` — Pydantic `Camera` and `CameraTrait` records per
  SRD §10.1, normalizing the SDM API's trait-dict into a list of name-keyed
  records.
- `nest_cli/sdm/client.py` — `SdmClient` thin wrapper around `requests`
  with auto-refresh on 401 and structured-error mapping for 4xx/5xx and
  network failures.
- `nest_cli/cli/list_cmd.py` — `list` (FR-1, FR-1a, FR-1b, FR-1c, FR-1d) and
  `discover` (FR-2, FR-2a) commands.
- `nest_cli/cli/cam_cmd.py` — `cam` subgroup with `list`/`info`/`capabilities`
  (FR-CAM-1, FR-CAM-2, FR-CAM-28).
- `nest_cli/cli/config_cmd.py` — `config` subgroup with `show`/`validate`
  (FR-16c).
- `nest_cli/cli/__init__.py` — root Click group `cli` wiring all subgroups.
- `requests>=2.31,<3` as an explicit dependency (was previously transitive
  via `google-auth-oauthlib`).
- Comprehensive mocked test coverage: `tests/test_errors.py`,
  `tests/test_output.py`, `tests/test_config.py`, `tests/sdm/test_client.py`,
  `tests/test_cli_list.py`, `tests/test_cli_cam.py`, `tests/test_cli_config.py`.

### Changed

- `nest_cli/__main__.py` — replaced the Phase 0 stub with a thin
  `from nest_cli.cli import cli as main` entry point.
- `nest_cli/__init__.py` — `__version__` bumped to `0.1.0`.
- `pyproject.toml` — `version` bumped to `0.1.0`.
- `tests/test_skeleton.py` — version assertions updated to `0.1.0`.
- `nest_cli/cli/auth_cmd.py` — rebased onto the shared `add_output_options`
  decorator and `emit()`/`exit_on_structured_error` infrastructure. Every
  `auth` verb now honors `--json`, `--jsonl`, `--quiet`, and `--output`
  uniformly with the rest of the CLI. The local `_emit`,
  `_exit_with_credential_error`, and `_OUTPUT_*` helpers were removed.
  Error envelopes no longer carry a `family` discriminator (not in SRD
  §11.3); the discriminator surfaces in the `auth status` payload only.
- `nest_cli/cli/auth_cmd.py` — `auth status` emits a JSON array per
  FR-CRED-10 (one element for the cam family; Phase 3 will add the wifi
  element).
- `nest_cli/cli/config_cmd.py` — `config show` text mode emits TOML per
  FR-16c (round-trips through `tomllib`); JSON modes continue to emit
  the structured-record dict.
- `nest_cli/auth/types.py`, `nest_cli/sdm/types.py`, `nest_cli/output.py`
  — datetime fields now serialize as RFC 3339 UTC with the literal `Z`
  suffix per FR-22, both via Pydantic `field_serializer` and through the
  shared `_to_jsonable` / `_pydantic_default` paths.
- `nest_cli/auth/credentials.py` — `EXIT_*` constants now imported from
  `nest_cli.errors` (single source of truth, SRD §11.1). The lock-file
  open path was hardened against a symlink-substitution race
  (`O_CREAT|O_EXCL|O_NOFOLLOW` first, `O_NOFOLLOW` fallback); a
  pre-existing symlink at `<creds>.lock` is rejected with a structured
  auth error.

### Fixed

- Re-sort the stdlib import block in `nest_cli/auth/credentials.py` so
  ruff I001 stops failing CI (`random` and `time` had been inserted out
  of alphabetical order by the lock-jitter fix).

## [0.0.1] - 2026-05-01

### Fixed

- Commit `uv.lock` so GitHub Actions `setup-uv@v3` cache restoration succeeds
  (the default `cache-dependency-glob: **/uv.lock` was failing against an
  ignored lockfile and breaking CI on every push).
- Move the CI credentials guard to the FIRST workflow step so a malicious
  transitive dependency cannot read secret env vars during `uv sync` before
  the guard fires. Broaden it from one variable to three:
  `NEST_CLI_TEST_OAUTH_CREDENTIALS`, `GOOGLE_APPLICATION_CREDENTIALS`,
  `NEST_CLI_TEST_FOYER_TOKEN`.
- `scripts/smoke-cam.py`: register `assignee` (mapped to `ASSIGNEE_PATH`) in
  the redaction registry. Real SDM `devices.get` responses include
  `assignee: enterprises/{project_id}/structures/{structure_id}/rooms/{room_id}`,
  which the post-scan veto was hard-failing on — blocking every operator
  fixture-capture run.
- `scripts/smoke-wifi.py`: comprehensive redaction overhaul.
  - Snake_case / kebab-case / camelCase key normalization in `_classify`
    so `friendly_name`, `friendly-name`, and `friendlyName` all hit the
    same rule.
  - Replace bare `id` substring matcher with a curated exact-match
    registry plus a separator-bounded endswith regex
    (`(?:_id|-id|[a-z]Id)$`) — catches `group_id`, `device-id`, `groupId`
    without over-firing on `paid`, `solid`, `valid`, `width`, `guidance`.
  - Add LAN-topology classes (`subnet`, `gateway`, `dhcp_range_start`,
    `dhcp_range_end`, `dns_servers`, `dns`, `wan_ip_address`).
  - Mirror cam-script's post-scan veto: `_LEAK_PATTERNS` for MAC,
    email, IPv4 (with `0.0.0.0`/`127.0.0.1`/`255.255.255.255` allowlist),
    UUID. `_write_fixture` raises `RedactionError` on any hit; `main`
    returns exit code 4.
  - Distinguish exception categories in `main` — network errors,
    upstream-shape errors (Foyer rotation), and redaction errors each
    print a category-specific message instead of a single bare blanket.
  - Validate that `--master-token` flag value is non-empty / non-whitespace
    (previously only the stdin path checked).
  - List-of-strings under a classified key (e.g. `dns_servers: ["8.8.8.8",
    "1.1.1.1"]`) now redact every element instead of slipping through.
- README v0.0.1-honest scope: status block now reflects skeleton state,
  install command points at git+https, Quick start codeblock prefaced
  with a "Coming in v0.1.0" warning.

### Added

- Initial repo skeleton, dependency pinning, CI baseline.
- `pyproject.toml` with hatchling build backend, pinned cam-side dependencies
  (`google-nest-sdm>=7.1,<8`, `google-cloud-pubsub>=2.36,<3`, `click>=8.1,<9`,
  `pydantic>=2.5,<3`) and `[wifi]` optional extras (`googlewifi>=0.0.21,<0.1`,
  `glocaltokens>=0.7,<0.8`).
- `nest_cli/__main__.py` Click stub — `--version` and `--help` only; no verbs
  registered until Phase 1.
- GitHub Actions CI on Python 3.11 and 3.12: ruff lint + format check, mypy,
  pytest, plus a hard guard that fails the build if real OAuth credentials
  leak into the runner environment.
- Smoke tests (`tests/test_skeleton.py`) covering import, `--version`, and
  no-arg help output.
- `docs/SECURITY.md` stub pointing at the SRD threat model (§4.7).

### Notes

- SRD §15 Decision 23 prefers git-SHA pins for the wifi-side libraries
  (`googlewifi`, `glocaltokens`) because they are single-maintainer with a
  history of upstream rotations. Phase 0 ships tight semver pins as a
  pragmatic first cut; tightening to SHAs is tracked for a future release
  before Phase 3 (the wifi slice) lands.
- `google-nest-sdm` is pinned to the 7.1.x line (latest 7.x is 7.1.5) rather
  than the current 9.x release. The 8.0.0 cut raised the Python floor to
  3.13, which would conflict with SRD §11's stated `requires-python = ">=3.11"`.
  Phase 0 holds the 3.11 floor; bumping to 3.13 (and to `google-nest-sdm` 9.x)
  is a deliberate future decision, not a Phase 0 carry-along.
