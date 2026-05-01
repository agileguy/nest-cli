# Changelog

All notable changes to `nest-cli` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
