"""Tests for ``ya_dialogs_api.nlu`` — NLU payload accessors."""

from __future__ import annotations

import pytest

from ya_dialogs_api import IntentMatch, iter_intent_matches


class TestIterIntentMatches:
    """``iter_intent_matches`` yields one IntentMatch per matched intent."""

    def test_yields_match_per_form_name(self) -> None:
        nlu = {
            "control.pause": {},
            "control.next": {"slots": {}},
        }
        matches = list(iter_intent_matches(nlu))
        form_names = [m.form_name for m in matches]
        assert form_names == ["control.pause", "control.next"]
        assert all(isinstance(m, IntentMatch) for m in matches)

    def test_none_input_yields_nothing(self) -> None:
        assert list(iter_intent_matches(None)) == []

    def test_non_mapping_input_yields_nothing(self) -> None:
        # Type ignore — defensive against malformed JSON loaders.
        assert list(iter_intent_matches("not a dict")) == []  # type: ignore[arg-type]

    def test_empty_mapping_yields_nothing(self) -> None:
        assert list(iter_intent_matches({})) == []

    def test_skips_invalid_form_names(self) -> None:
        nlu = {
            "": {},  # empty key
            "control.pause": {},
        }
        matches = list(iter_intent_matches(nlu))
        assert [m.form_name for m in matches] == ["control.pause"]

    def test_non_mapping_payload_wrapped_as_empty(self) -> None:
        """A weird payload (string/list) is wrapped — slot accessors return None."""
        nlu = {"control.pause": "garbage"}
        matches = list(iter_intent_matches(nlu))  # type: ignore[arg-type]
        assert len(matches) == 1
        assert matches[0].slot_int("level") is None


class TestIntentMatchSlotAccessors:
    """``IntentMatch`` slot_int / slot_str / slot_value / has_slot / raw_slot."""

    def _match(self, payload: dict) -> IntentMatch:  # type: ignore[type-arg]
        return IntentMatch(form_name="control.x", payload=payload)

    def test_slot_int_extracts_int(self) -> None:
        m = self._match({"slots": {"level": {"value": 50}}})
        assert m.slot_int("level") == 50

    def test_slot_int_coerces_float(self) -> None:
        m = self._match({"slots": {"level": {"value": 50.7}}})
        assert m.slot_int("level") == 50

    def test_slot_int_negative(self) -> None:
        m = self._match({"slots": {"level": {"value": -5}}})
        assert m.slot_int("level") == -5

    def test_slot_int_missing_slot(self) -> None:
        m = self._match({"slots": {}})
        assert m.slot_int("level") is None

    def test_slot_int_missing_slots_block(self) -> None:
        m = self._match({})
        assert m.slot_int("level") is None

    def test_slot_int_missing_value_key(self) -> None:
        m = self._match({"slots": {"level": {"type": "YANDEX.NUMBER"}}})
        assert m.slot_int("level") is None

    def test_slot_int_rejects_bool(self) -> None:
        m = self._match({"slots": {"level": {"value": True}}})
        assert m.slot_int("level") is None
        m_false = self._match({"slots": {"level": {"value": False}}})
        assert m_false.slot_int("level") is None

    def test_slot_int_rejects_string(self) -> None:
        m = self._match({"slots": {"level": {"value": "fifty"}}})
        assert m.slot_int("level") is None

    def test_slot_str_extracts_string(self) -> None:
        m = self._match({"slots": {"unit": {"value": "minutes"}}})
        assert m.slot_str("unit") == "minutes"

    def test_slot_str_rejects_int(self) -> None:
        m = self._match({"slots": {"unit": {"value": 30}}})
        assert m.slot_str("unit") is None

    def test_slot_value_returns_raw(self) -> None:
        m = self._match({"slots": {"x": {"value": [1, 2, 3]}}})
        assert m.slot_value("x") == [1, 2, 3]

    def test_slot_value_missing(self) -> None:
        m = self._match({"slots": {}})
        assert m.slot_value("x") is None

    def test_has_slot(self) -> None:
        m = self._match({"slots": {"x": {"value": 1}}})
        assert m.has_slot("x") is True
        assert m.has_slot("y") is False
        m_empty = self._match({})
        assert m_empty.has_slot("x") is False

    def test_raw_slot_returns_mapping(self) -> None:
        m = self._match({"slots": {"x": {"value": 1, "type": "YANDEX.NUMBER"}}})
        slot = m.raw_slot("x")
        assert slot == {"value": 1, "type": "YANDEX.NUMBER"}

    def test_raw_slot_missing_returns_none(self) -> None:
        m = self._match({"slots": {}})
        assert m.raw_slot("x") is None

    def test_slots_value_not_a_mapping_returns_none(self) -> None:
        # If slots is malformed (e.g. list), accessors gracefully return None.
        m = self._match({"slots": "garbage"})
        assert m.slot_int("x") is None
        assert m.slot_str("x") is None
        assert m.has_slot("x") is False

    def test_slot_payload_not_a_mapping_returns_none(self) -> None:
        m = self._match({"slots": {"x": "garbage"}})
        assert m.slot_int("x") is None
        assert m.raw_slot("x") is None


@pytest.mark.parametrize(
    ("payload", "name", "expected"),
    [
        # Real-world payloads from Yandex docs / probe.
        ({"slots": {"level": {"type": "YANDEX.NUMBER", "value": 75}}}, "level", 75),
        ({"slots": {"level": {"type": "YANDEX.NUMBER", "value": 75.0}}}, "level", 75),
        ({"slots": {"amount": {"value": 2}}}, "amount", 2),
        # Yandex sometimes wraps in `tokens` etc — only `value` is read.
        ({"slots": {"x": {"value": 10, "tokens": {"start": 0, "end": 2}}}}, "x", 10),
    ],
)
def test_slot_int_real_world_shapes(
    payload: dict,  # type: ignore[type-arg]
    name: str,
    expected: int,
) -> None:
    m = IntentMatch(form_name="control.x", payload=payload)
    assert m.slot_int(name) == expected
