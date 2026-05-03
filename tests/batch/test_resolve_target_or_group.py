"""Tests for ``resolve_target_or_group`` (FR-5, FR-6).

The helper translates an operator-supplied target argument
(``alias-name`` / ``@group-name`` / a literal device path) into an
ordered list of ``ResolvedTarget`` records ready for fan-out
emission. Cross-family group memberships are NOT a resolution error
(FR-5) — wrong-family members are flagged so the per-target executor
can emit an exit-5 record per FR-9a / FR-8e.

Test coverage focus:

- Plain alias  → single resolved target, family from target prefix.
- ``@group``   → multi-element list in config-file order.
- Unknown ``@group`` → ``StructuredError(code=4)``.
- Cross-family membership → wrong-family items get
  ``family_match=False``; the resolver does NOT mutate the order.
- Bare device path (no alias) passes through unchanged.
"""

from __future__ import annotations

import pytest

from nest_cli.cli._shared import (
    ResolvedTarget,
    resolve_target_or_group,
)
from nest_cli.config import Config
from nest_cli.errors import EXIT_NOT_FOUND, StructuredError


def _config(
    *, aliases: dict[str, str] | None = None, groups: dict[str, list[str]] | None = None
) -> Config:
    return Config(aliases=aliases or {}, groups=groups or {})


class TestPlainAlias:
    def test_known_alias_returns_single_resolved_target(self) -> None:
        config = _config(aliases={"front-door": "enterprises/proj/devices/d1"})
        out = resolve_target_or_group(config, "front-door", expected_family="cam")
        assert out == [
            ResolvedTarget(
                name="front-door",
                target="enterprises/proj/devices/d1",
                family="cam",
                family_match=True,
            )
        ]

    def test_literal_target_passes_through(self) -> None:
        """A non-alias target string is returned verbatim with family inferred."""
        config = _config()
        out = resolve_target_or_group(config, "enterprises/proj/devices/d99", expected_family="cam")
        assert out == [
            ResolvedTarget(
                name="enterprises/proj/devices/d99",
                target="enterprises/proj/devices/d99",
                family="cam",
                family_match=True,
            )
        ]

    def test_wifi_alias_resolves_to_wifi_family(self) -> None:
        config = _config(aliases={"office-mesh": "wifi:groups/g1"})
        out = resolve_target_or_group(config, "office-mesh", expected_family="wifi")
        assert out == [
            ResolvedTarget(
                name="office-mesh",
                target="wifi:groups/g1",
                family="wifi",
                family_match=True,
            )
        ]

    def test_wrong_family_for_single_alias_marks_not_match(self) -> None:
        """A cam alias passed to a wifi verb resolves but with family_match=False.

        Per FR-5, the cross-family case is reported as a per-target
        exit-5 record at fan-out time. For a single-alias call (not a
        group), this means the verb still gets a single-element list
        but with ``family_match=False`` — the verb's fan-out wrapper
        can refuse the call cleanly with an unsupported_feature error.
        """
        config = _config(aliases={"front-door": "enterprises/proj/devices/d1"})
        out = resolve_target_or_group(config, "front-door", expected_family="wifi")
        assert len(out) == 1
        assert out[0].family == "cam"
        assert out[0].family_match is False


class TestAtPrefixGroup:
    def test_at_prefix_resolves_group_members_in_config_order(self) -> None:
        """``@home-cams`` → length-N list in config's member-list order."""
        config = _config(
            aliases={
                "front": "enterprises/proj/devices/dF",
                "back": "enterprises/proj/devices/dB",
                "side": "enterprises/proj/devices/dS",
            },
            groups={"home-cams": ["front", "back", "side"]},
        )
        out = resolve_target_or_group(config, "@home-cams", expected_family="cam")
        names = [r.name for r in out]
        assert names == ["front", "back", "side"]
        for record in out:
            assert record.family == "cam"
            assert record.family_match is True

    def test_unknown_at_group_exits_4(self) -> None:
        config = _config(groups={"home-cams": []})
        with pytest.raises(StructuredError) as exc_info:
            resolve_target_or_group(config, "@unknown-group", expected_family="cam")
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_at_group_with_unknown_member_alias_exits_4(self) -> None:
        """A group naming a missing alias surfaces as exit 4 with a hint.

        Operators may forget to define an alias they reference in
        ``[groups]``. That's a config-time error caught at resolution.
        """
        config = _config(
            aliases={"front": "enterprises/proj/devices/dF"},
            groups={"home-cams": ["front", "missing"]},
        )
        with pytest.raises(StructuredError) as exc_info:
            resolve_target_or_group(config, "@home-cams", expected_family="cam")
        assert exc_info.value.code == EXIT_NOT_FOUND


class TestCrossFamilyGroup:
    def test_cam_verb_against_mixed_group_marks_wifi_member_not_match(self) -> None:
        """Mixed group + cam verb: wifi member surfaces with family_match=False.

        Resolution does not silently drop the wifi member. The fan-out
        executor emits an exit-5 record for it (FR-5).
        """
        config = _config(
            aliases={
                "front-door": "enterprises/proj/devices/dF",
                "office-mesh": "wifi:groups/g1",
            },
            groups={"all-stuff": ["front-door", "office-mesh"]},
        )
        out = resolve_target_or_group(config, "@all-stuff", expected_family="cam")
        assert len(out) == 2
        assert [r.name for r in out] == ["front-door", "office-mesh"]
        assert out[0].family == "cam"
        assert out[0].family_match is True
        assert out[1].family == "wifi"
        assert out[1].family_match is False

    def test_wifi_verb_against_mixed_group_marks_cam_member_not_match(self) -> None:
        """Same shape as above, mirrored for wifi side."""
        config = _config(
            aliases={
                "front-door": "enterprises/proj/devices/dF",
                "office-mesh": "wifi:groups/g1",
            },
            groups={"all-stuff": ["front-door", "office-mesh"]},
        )
        out = resolve_target_or_group(config, "@all-stuff", expected_family="wifi")
        assert [r.name for r in out] == ["front-door", "office-mesh"]
        assert out[0].family_match is False
        assert out[1].family_match is True
