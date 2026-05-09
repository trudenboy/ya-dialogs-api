"""Tests for ``ya_dialogs_api.manifest`` — TOML loader + entity DSL parser."""

from __future__ import annotations

import textwrap
import tomllib

import pytest

from ya_dialogs_api import (
    SUPPORTED_SCHEMA_VERSION,
    EntityDraft,
    EntityValue,
    IntentDraft,
    SkillManifest,
    SkillManifestError,
    entities_to_drafts,
    intent_to_draft,
    parse_manifest,
    parse_manifest_text,
)
from ya_dialogs_api.manifest import ManifestIntent


def _toml_to_dict(text: str) -> dict:  # type: ignore[type-arg]
    """Inline-readable TOML helper."""
    return tomllib.loads(textwrap.dedent(text))


class TestParseManifest:
    """``parse_manifest`` validates structure and converts to dataclasses."""

    def test_minimal_valid_manifest(self) -> None:
        raw = _toml_to_dict(
            """
            schema_version = 1
            [entities]
            text = ""
            """
        )
        manifest = parse_manifest(raw)
        assert manifest.schema_version == 1
        assert manifest.entities.text == ""
        assert manifest.intents == ()

    def test_intent_fields_are_normalised(self) -> None:
        raw = _toml_to_dict(
            '''
            schema_version = 1
            [entities]
            text = ""
            [[intents]]
            form_name = "control.pause"
            human_readable_name = "Пауза"
            is_activation = false
            positive_tests = """
            пауза
            поставь на паузу
            """
            grammar = """
            root:
                пауза | поставь на паузу
            """
            '''
        )
        manifest = parse_manifest(raw)
        assert len(manifest.intents) == 1
        intent = manifest.intents[0]
        assert intent.form_name == "control.pause"
        assert intent.human_readable_name == "Пауза"
        assert intent.is_activation is False
        assert intent.positive_tests == "пауза\nпоставь на паузу"
        assert "root:" in intent.grammar
        assert "пауза | поставь на паузу" in intent.grammar

    def test_missing_schema_version_raises(self) -> None:
        with pytest.raises(SkillManifestError, match="schema_version"):
            parse_manifest({"entities": {"text": ""}})

    def test_unsupported_schema_version_raises(self) -> None:
        with pytest.raises(SkillManifestError, match="newer"):
            parse_manifest(
                {"schema_version": SUPPORTED_SCHEMA_VERSION + 1, "entities": {"text": ""}}
            )

    def test_missing_form_name_raises(self) -> None:
        raw = _toml_to_dict(
            """
            schema_version = 1
            [entities]
            text = ""
            [[intents]]
            human_readable_name = "X"
            grammar = "root: x"
            """
        )
        with pytest.raises(SkillManifestError, match="form_name"):
            parse_manifest(raw)

    def test_duplicate_form_name_raises(self) -> None:
        raw = _toml_to_dict(
            """
            schema_version = 1
            [entities]
            text = ""
            [[intents]]
            form_name = "control.pause"
            grammar = "root: a"
            [[intents]]
            form_name = "control.pause"
            grammar = "root: b"
            """
        )
        with pytest.raises(SkillManifestError, match="duplicate form_name"):
            parse_manifest(raw)

    def test_intent_order_is_preserved(self) -> None:
        raw = _toml_to_dict(
            """
            schema_version = 1
            [entities]
            text = ""
            [[intents]]
            form_name = "control.zeta"
            grammar = "root: z"
            [[intents]]
            form_name = "control.alpha"
            grammar = "root: a"
            """
        )
        manifest = parse_manifest(raw)
        assert [i.form_name for i in manifest.intents] == [
            "control.zeta",
            "control.alpha",
        ]


class TestParseManifestText:
    """``parse_manifest_text`` parses a TOML source string end-to-end."""

    def test_round_trip(self) -> None:
        text = textwrap.dedent(
            """
            schema_version = 1
            [entities]
            text = ""
            [[intents]]
            form_name = "control.pause"
            grammar = "root: пауза"
            """
        )
        manifest = parse_manifest_text(text)
        assert manifest.intents[0].form_name == "control.pause"

    def test_invalid_toml_raises(self) -> None:
        with pytest.raises(SkillManifestError, match="malformed"):
            parse_manifest_text("not = a = valid = toml = ===")


class TestEntitiesToDrafts:
    """``entities_to_drafts`` parses Granet customEntities DSL."""

    def test_empty_text_returns_empty_list(self) -> None:
        assert entities_to_drafts("") == []
        assert entities_to_drafts("   \n\n  ") == []

    def test_single_entity_with_two_values(self) -> None:
        text = textwrap.dedent(
            """\
            entity time_unit:
                values:
                    seconds:
                        секунда | секунды | сек
                    minutes:
                        минута | минуты | мин
            """
        )
        drafts = entities_to_drafts(text)
        assert len(drafts) == 1
        entity = drafts[0]
        assert entity.name == "time_unit"
        assert len(entity.values) == 2
        assert entity.values[0].name == "seconds"
        assert entity.values[0].phrases == ("секунда", "секунды", "сек")
        assert entity.values[1].name == "minutes"
        assert entity.values[1].phrases == ("минута", "минуты", "мин")

    def test_value_phrases_can_span_multiple_lines(self) -> None:
        text = textwrap.dedent(
            """\
            entity x:
                values:
                    a:
                        раз | два
                        три | четыре
                    b:
                        пять
            """
        )
        drafts = entities_to_drafts(text)
        assert drafts[0].values[0].phrases == ("раз", "два", "три", "четыре")
        assert drafts[0].values[1].phrases == ("пять",)

    def test_multiple_entities(self) -> None:
        text = textwrap.dedent(
            """\
            entity first:
                values:
                    a:
                        один
            entity second:
                values:
                    b:
                        два
            """
        )
        drafts = entities_to_drafts(text)
        assert [e.name for e in drafts] == ["first", "second"]

    def test_round_trip_with_to_dsl(self) -> None:
        """Parsing the DSL back through to_dsl() should yield equivalent text."""
        original = textwrap.dedent(
            """\
            entity time_unit:
                values:
                    seconds:
                        секунда | секунды | сек
            """
        )
        drafts = entities_to_drafts(original)
        re_rendered = drafts[0].to_dsl()
        # Re-rendered DSL has the same structural content (allowing
        # whitespace normalisation).
        re_parsed = entities_to_drafts(re_rendered)
        assert re_parsed[0].name == drafts[0].name
        assert re_parsed[0].values == drafts[0].values

    def test_missing_values_block_raises(self) -> None:
        text = "entity x:\n    a:\n        один\n"
        with pytest.raises(SkillManifestError, match="expected 'values:'"):
            entities_to_drafts(text)

    def test_value_with_no_phrases_raises(self) -> None:
        text = textwrap.dedent(
            """\
            entity x:
                values:
                    a:
                    b:
                        один
            """
        )
        with pytest.raises(SkillManifestError, match="no phrases"):
            entities_to_drafts(text)

    def test_stray_content_before_first_entity_raises(self) -> None:
        text = "garbage\nentity x:\n    values:\n        a:\n            один\n"
        with pytest.raises(SkillManifestError, match="stray content"):
            entities_to_drafts(text)


class TestIntentToDraft:
    """``intent_to_draft`` produces an IntentDraft suitable for set_intents."""

    def test_basic_conversion(self) -> None:
        intent = ManifestIntent(
            form_name="control.pause",
            human_readable_name="Пауза",
            grammar="root: пауза",
            positive_tests="пауза",
            is_activation=False,
        )
        draft = intent_to_draft(intent)
        assert isinstance(draft, IntentDraft)
        assert draft.form_name == "control.pause"
        assert draft.source_text == "root: пауза"
        # No structured slots — manifest carries them inline if any.
        assert draft.slots == ()

    def test_manifest_to_intent_drafts_method(self) -> None:
        manifest = SkillManifest(
            schema_version=1,
            entities=__import__("ya_dialogs_api").ManifestEntities(text=""),
            intents=(
                ManifestIntent(form_name="control.a", grammar="root: a"),
                ManifestIntent(form_name="control.b", grammar="root: b"),
            ),
        )
        drafts = manifest.to_intent_drafts()
        assert [d.form_name for d in drafts] == ["control.a", "control.b"]
        assert all(isinstance(d, IntentDraft) for d in drafts)

    def test_manifest_to_entity_drafts_method(self) -> None:
        manifest = parse_manifest_text(
            textwrap.dedent(
                '''
                schema_version = 1
                [entities]
                text = """
                entity time_unit:
                    values:
                        seconds:
                            секунда
                """
                '''
            )
        )
        drafts = manifest.to_entity_drafts()
        assert len(drafts) == 1
        assert isinstance(drafts[0], EntityDraft)
        assert drafts[0].name == "time_unit"
        assert drafts[0].values[0] == EntityValue(name="seconds", phrases=("секунда",))
