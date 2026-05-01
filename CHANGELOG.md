# Changelog

All notable changes to `nest-cli` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.1] - 2026-05-01

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
