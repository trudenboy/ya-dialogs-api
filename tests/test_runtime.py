"""Tests for ``ya_dialogs_api.runtime`` — declarative slot dispatcher."""

from __future__ import annotations

import pytest

from ya_dialogs_api import (
    IntentMatch,
    ManifestMapping,
    ManifestMultiplyWhen,
    ManifestRuntime,
    apply_runtime_mapping,
)


def _match(slots: dict[str, dict[str, object]] | None = None) -> IntentMatch:
    """Build an IntentMatch with a synthetic ``slots`` payload."""
    payload: dict[str, object] = {"slots": slots} if slots is not None else {}
    return IntentMatch(form_name="control.x", payload=payload)


class TestNoMappingRuntime:
    """Runtime without any mapping rules — empty fields dict."""

    def test_returns_empty_dict(self) -> None:
        runtime = ManifestRuntime(kind="control", action="pause")
        assert apply_runtime_mapping(_match(), runtime) == {}


class TestIdentityTransform:
    """``transform="identity"`` — pass-through (no clamp / abs)."""

    def test_int_value_passes_through(self) -> None:
        runtime = ManifestRuntime(
            kind="control",
            action="x",
            mapping=(ManifestMapping(field="value", from_slot="n"),),
        )
        result = apply_runtime_mapping(_match({"n": {"value": 42}}), runtime)
        assert result == {"value": 42}

    def test_negative_int_passes_through(self) -> None:
        runtime = ManifestRuntime(
            kind="control",
            action="x",
            mapping=(ManifestMapping(field="value", from_slot="n"),),
        )
        result = apply_runtime_mapping(_match({"n": {"value": -7}}), runtime)
        assert result == {"value": -7}


class TestClampTransform:
    """``transform="clamp"`` — bound to ``[min, max]``."""

    @pytest.fixture
    def rule(self) -> ManifestMapping:
        return ManifestMapping(field="value", from_slot="n", transform="clamp", min=0, max=100)

    def test_value_within_range_unchanged(self, rule: ManifestMapping) -> None:
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        result = apply_runtime_mapping(_match({"n": {"value": 50}}), runtime)
        assert result == {"value": 50}

    def test_above_max_clamped(self, rule: ManifestMapping) -> None:
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        result = apply_runtime_mapping(_match({"n": {"value": 150}}), runtime)
        assert result == {"value": 100}

    def test_below_min_clamped(self, rule: ManifestMapping) -> None:
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        result = apply_runtime_mapping(_match({"n": {"value": -5}}), runtime)
        assert result == {"value": 0}

    def test_open_lower_bound(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="clamp", max=10)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        # min=None → no lower bound.
        result = apply_runtime_mapping(_match({"n": {"value": -100}}), runtime)
        assert result == {"value": -100}


class TestAbsClampTransform:
    """``transform="abs_clamp"`` — clamp(abs(value)). Used with sign override."""

    def test_abs_then_clamp(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="abs_clamp", min=0, max=100)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        # |-50| = 50 → within [0, 100] → 50.
        assert apply_runtime_mapping(_match({"n": {"value": -50}}), runtime) == {"value": 50}

    def test_abs_above_max_clamped(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="abs_clamp", min=0, max=100)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        # |-999| = 999 → clamped to 100.
        assert apply_runtime_mapping(_match({"n": {"value": -999}}), runtime) == {"value": 100}


class TestSign:
    """``sign="negative"`` negates the value (post-clamp)."""

    def test_positive_sign_default(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="abs_clamp", min=0, max=100)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({"n": {"value": 20}}), runtime) == {"value": 20}

    def test_negative_sign_negates(self) -> None:
        rule = ManifestMapping(
            field="value",
            from_slot="n",
            transform="abs_clamp",
            min=0,
            max=100,
            sign="negative",
        )
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        # |20| = 20 → sign=negative → -20.
        assert apply_runtime_mapping(_match({"n": {"value": 20}}), runtime) == {"value": -20}


class TestDefault:
    """Missing slot + ``default`` — uses default."""

    def test_default_when_slot_missing(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="identity", default=10)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({}), runtime) == {"value": 10}

    def test_default_with_clamp(self) -> None:
        rule = ManifestMapping(
            field="value",
            from_slot="n",
            transform="clamp",
            min=0,
            max=100,
            default=42,
        )
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({}), runtime) == {"value": 42}

    def test_no_default_missing_slot_skips_intent(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="identity")
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({}), runtime) is None


class TestRejectIfBelow:
    """``reject_if_below`` — skip intent when value below threshold."""

    def test_skip_when_below(self) -> None:
        rule = ManifestMapping(
            field="value", from_slot="n", transform="identity", reject_if_below=1
        )
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({"n": {"value": 0}}), runtime) is None
        assert apply_runtime_mapping(_match({"n": {"value": -10}}), runtime) is None

    def test_passes_at_threshold(self) -> None:
        rule = ManifestMapping(
            field="value", from_slot="n", transform="identity", reject_if_below=1
        )
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({"n": {"value": 1}}), runtime) == {"value": 1}


class TestCap:
    """``cap`` — skip intent when value exceeds threshold (misclassification)."""

    def test_skip_when_above_cap(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="identity", cap=86400)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({"n": {"value": 100000}}), runtime) is None

    def test_passes_at_cap(self) -> None:
        rule = ManifestMapping(field="value", from_slot="n", transform="identity", cap=86400)
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({"n": {"value": 86400}}), runtime) == {"value": 86400}


class TestMultiplyWhen:
    """``multiply_when`` — conditional unit conversion."""

    @pytest.fixture
    def runtime(self) -> ManifestRuntime:
        rule = ManifestMapping(
            field="value",
            from_slot="amount",
            transform="identity",
            multiply_when=(ManifestMultiplyWhen(slot="unit", equals="minutes", factor=60),),
        )
        return ManifestRuntime(kind="control", action="seek", mapping=(rule,))

    def test_unit_minutes_multiplies(self, runtime: ManifestRuntime) -> None:
        result = apply_runtime_mapping(
            _match({"amount": {"value": 2}, "unit": {"value": "minutes"}}),
            runtime,
        )
        assert result == {"value": 120}

    def test_unit_seconds_no_multiply(self, runtime: ManifestRuntime) -> None:
        result = apply_runtime_mapping(
            _match({"amount": {"value": 30}, "unit": {"value": "seconds"}}),
            runtime,
        )
        assert result == {"value": 30}

    def test_unit_missing_no_multiply(self, runtime: ManifestRuntime) -> None:
        result = apply_runtime_mapping(_match({"amount": {"value": 45}}), runtime)
        assert result == {"value": 45}

    def test_cap_applied_after_multiply(self) -> None:
        """cap should reject after unit conversion (2000 min → 120000 s > cap)."""
        rule = ManifestMapping(
            field="value",
            from_slot="amount",
            transform="identity",
            cap=86400,
            multiply_when=(ManifestMultiplyWhen(slot="unit", equals="minutes", factor=60),),
        )
        runtime = ManifestRuntime(kind="x", action="x", mapping=(rule,))
        assert (
            apply_runtime_mapping(
                _match({"amount": {"value": 2000}, "unit": {"value": "minutes"}}),
                runtime,
            )
            is None
        )


class TestStringSlotPath:
    """``slot_type="str"`` reads via slot_str, no transforms apply."""

    def test_str_value(self) -> None:
        rule = ManifestMapping(field="play_kind", from_slot="kind", slot_type="str")
        runtime = ManifestRuntime(kind="play", action="x", mapping=(rule,))
        result = apply_runtime_mapping(
            _match({"kind": {"value": "my_wave"}}),
            runtime,
        )
        assert result == {"play_kind": "my_wave"}

    def test_missing_str_skips(self) -> None:
        rule = ManifestMapping(field="play_kind", from_slot="kind", slot_type="str")
        runtime = ManifestRuntime(kind="play", action="x", mapping=(rule,))
        assert apply_runtime_mapping(_match({}), runtime) is None


class TestMultipleMappings:
    """Several mapping rules in one runtime — all populate the result dict."""

    def test_two_int_fields(self) -> None:
        runtime = ManifestRuntime(
            kind="x",
            action="x",
            mapping=(
                ManifestMapping(field="a", from_slot="x"),
                ManifestMapping(field="b", from_slot="y"),
            ),
        )
        result = apply_runtime_mapping(
            _match({"x": {"value": 1}, "y": {"value": 2}}),
            runtime,
        )
        assert result == {"a": 1, "b": 2}

    def test_one_missing_skips_whole_intent(self) -> None:
        """If any rule signals skip, the whole intent is skipped."""
        runtime = ManifestRuntime(
            kind="x",
            action="x",
            mapping=(
                ManifestMapping(field="a", from_slot="x"),
                ManifestMapping(field="b", from_slot="y"),
            ),
        )
        result = apply_runtime_mapping(_match({"x": {"value": 1}}), runtime)
        assert result is None


class TestRealWorldPayloads:
    """Composite cases mirroring the v1.4.x ParsedControl-building logic."""

    def test_volume_set_clamped(self) -> None:
        """control.volume_set: level slot → ParsedControl(value=clamp 0..100)."""
        runtime = ManifestRuntime(
            kind="control",
            action="volume_set",
            mapping=(
                ManifestMapping(
                    field="value",
                    from_slot="level",
                    transform="clamp",
                    min=0,
                    max=100,
                ),
            ),
        )
        assert apply_runtime_mapping(_match({"level": {"value": 150}}), runtime) == {"value": 100}

    def test_volume_decrease_default_negate(self) -> None:
        """control.volume_decrease: optional delta, default 10, sign negative."""
        runtime = ManifestRuntime(
            kind="control",
            action="volume_relative",
            mapping=(
                ManifestMapping(
                    field="value",
                    from_slot="delta",
                    transform="abs_clamp",
                    min=0,
                    max=100,
                    default=10,
                    sign="negative",
                ),
            ),
        )
        # No slot → default=10 → abs(10)=10 → sign=- → -10.
        assert apply_runtime_mapping(_match({}), runtime) == {"value": -10}
        # delta=-5 → abs=5 → sign=- → -5.
        assert apply_runtime_mapping(_match({"delta": {"value": -5}}), runtime) == {"value": -5}

    def test_seek_forward_minutes_capped(self) -> None:
        """control.seek_forward: amount * 60 if unit=minutes; cap 86400."""
        runtime = ManifestRuntime(
            kind="control",
            action="seek_forward",
            mapping=(
                ManifestMapping(
                    field="value",
                    from_slot="amount",
                    transform="identity",
                    reject_if_below=1,
                    cap=86400,
                    multiply_when=(ManifestMultiplyWhen(slot="unit", equals="minutes", factor=60),),
                ),
            ),
        )
        assert apply_runtime_mapping(
            _match({"amount": {"value": 2}, "unit": {"value": "minutes"}}),
            runtime,
        ) == {"value": 120}
        # 0 below threshold.
        assert apply_runtime_mapping(_match({"amount": {"value": 0}}), runtime) is None
        # >24h after multiply.
        assert (
            apply_runtime_mapping(
                _match({"amount": {"value": 2000}, "unit": {"value": "minutes"}}),
                runtime,
            )
            is None
        )
