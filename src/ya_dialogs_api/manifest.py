"""Declarative skill manifest — TOML format with copy-paste-from-console DSL.

This module defines a portable, format-stable representation of a Yandex
Dialogs custom skill (intents + custom entities) that any client of
``ya-dialogs-api`` can author, version, ship to users, and edit live
without depending on Python source code changes.

The manifest is intentionally a thin wrapper around the dev-console
artefacts:

* ``intents[*].grammar`` carries the Granet ``sourceText`` byte-for-byte
  (including any inline ``slots:`` sub-block) — copy-pasteable both
  ways with the dev-console editor at
  ``https://dialogs.yandex.ru/developer/skills/<id>/draft/settings/intents``.
* ``entities.text`` carries the customEntities Granet blob byte-for-byte.
* All metadata field names mirror the Yandex API field names in
  snake_case (``form_name`` ↔ ``formName``, ``human_readable_name`` ↔
  ``humanReadableName``, ``is_activation`` ↔ ``isActivation``,
  ``positive_tests`` ↔ ``positiveTests``, ``negative_tests`` ↔
  ``negativeTests``).

The module is intentionally **agnostic** about runtime semantics —
i.e. how a client maps a matched intent to its own command dataclass.
That belongs to the calling application, not to a shared library
component.

TOML schema (current ``schema_version = 1``)::

    schema_version = 1

    [entities]
    text = \"\"\"<full Granet customEntities sourceText>\"\"\"

    [[intents]]
    form_name           = "control.pause"
    human_readable_name = "Пауза"
    is_activation       = false
    positive_tests      = "<newline-separated>"
    negative_tests      = "<newline-separated>"
    grammar             = \"\"\"<full Granet sourceText>\"\"\"

The loader rejects ``schema_version`` newer than this module knows about
so a forward-compatible user override doesn't get silently parsed as v1.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from collections.abc import Mapping
from typing import Any

from .api_client import EntityDraft, EntityValue, IntentDraft

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "ManifestEntities",
    "ManifestIntent",
    "SkillManifest",
    "SkillManifestError",
    "entities_to_drafts",
    "intent_to_draft",
    "parse_manifest",
    "parse_manifest_text",
]


SUPPORTED_SCHEMA_VERSION = 1


class SkillManifestError(ValueError):
    """Raised when the skill manifest TOML is malformed or unsupported."""


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestEntities:
    """Single Granet ``customEntities`` source text shipped to Yandex."""

    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestIntent:
    """Single Yandex Dialogs custom-intent definition.

    Field naming mirrors Yandex API field names (``form_name`` ↔
    ``formName``, ``human_readable_name`` ↔ ``humanReadableName``,
    ``is_activation`` ↔ ``isActivation``, ``positive_tests`` ↔
    ``positiveTests``, ``negative_tests`` ↔ ``negativeTests``) so the
    cognitive load matches the dev-console UI.

    ``grammar`` carries the Granet ``sourceText`` verbatim, including
    the inline ``slots:`` sub-block when the intent is slot-bearing.
    """

    form_name: str
    human_readable_name: str = ""
    is_activation: bool = False
    positive_tests: str = ""
    negative_tests: str = ""
    grammar: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class SkillManifest:
    """Parsed skill manifest — immutable view of the TOML source.

    Use :meth:`to_intent_drafts` and :meth:`to_entity_drafts` to feed
    the result into :func:`auto_create_skill` /
    :func:`auto_update_skill` directly.
    """

    schema_version: int
    entities: ManifestEntities
    intents: tuple[ManifestIntent, ...]

    def to_intent_drafts(self) -> list[IntentDraft]:
        """Convert all intents to ``IntentDraft`` for ``set_intents``.

        Each ``grammar`` is shipped as ``source_text`` verbatim;
        because the manifest carries any ``slots:`` sub-block inline,
        ``IntentDraft.slots`` is left empty (the
        :attr:`IntentDraft.rendered_source_text` short-circuit detects
        the existing block and avoids double-embedding).
        """
        return [intent_to_draft(intent) for intent in self.intents]

    def to_entity_drafts(self) -> list[EntityDraft]:
        """Parse the entities Granet blob into structured drafts."""
        return entities_to_drafts(self.entities.text)


def parse_manifest_text(text: str) -> SkillManifest:
    """Parse a TOML manifest source string.

    :raises SkillManifestError: on TOML syntax errors, missing required
        fields, duplicate ``form_name`` values, or unsupported
        ``schema_version``.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SkillManifestError(f"manifest TOML is malformed: {exc}") from exc
    return parse_manifest(data)


def parse_manifest(raw: Mapping[str, Any]) -> SkillManifest:
    """Validate and convert a raw TOML mapping into a :class:`SkillManifest`.

    :param raw: result of :func:`tomllib.loads` / :func:`tomllib.load`.
    :raises SkillManifestError: on missing required fields, duplicate
        ``form_name`` values, or unsupported ``schema_version``.
    """
    schema = raw.get("schema_version")
    if not isinstance(schema, int):
        raise SkillManifestError("manifest: 'schema_version' (int) is required")
    if schema > SUPPORTED_SCHEMA_VERSION:
        raise SkillManifestError(
            f"manifest: schema_version={schema} is newer than this library "
            f"supports ({SUPPORTED_SCHEMA_VERSION}). Upgrade ya-dialogs-api, "
            "or downgrade the manifest.",
        )

    entities_raw = raw.get("entities") or {}
    if not isinstance(entities_raw, Mapping):
        raise SkillManifestError("manifest: 'entities' must be a TOML table")
    entities_text = entities_raw.get("text", "")
    if not isinstance(entities_text, str):
        raise SkillManifestError("manifest: 'entities.text' must be a string")
    entities = ManifestEntities(text=entities_text)

    intents_raw = raw.get("intents") or []
    if not isinstance(intents_raw, list):
        raise SkillManifestError("manifest: 'intents' must be an array of tables")
    intents: list[ManifestIntent] = []
    seen_form_names: set[str] = set()
    for index, item in enumerate(intents_raw):
        if not isinstance(item, Mapping):
            raise SkillManifestError(
                f"manifest: intents[{index}] must be a TOML table",
            )
        form_name = item.get("form_name")
        if not isinstance(form_name, str) or not form_name:
            raise SkillManifestError(
                f"manifest: intents[{index}].form_name (non-empty str) is required",
            )
        if form_name in seen_form_names:
            raise SkillManifestError(
                f"manifest: duplicate form_name {form_name!r} at intents[{index}]",
            )
        seen_form_names.add(form_name)
        intents.append(
            ManifestIntent(
                form_name=form_name,
                human_readable_name=_require_str(
                    item, "human_readable_name", index, default=""
                ),
                is_activation=_require_bool(
                    item, "is_activation", index, default=False
                ),
                positive_tests=_strip_block(
                    _require_str(item, "positive_tests", index, default="")
                ),
                negative_tests=_strip_block(
                    _require_str(item, "negative_tests", index, default="")
                ),
                grammar=_require_str(item, "grammar", index, default=""),
            )
        )
    return SkillManifest(
        schema_version=schema,
        entities=entities,
        intents=tuple(intents),
    )


def intent_to_draft(intent: ManifestIntent) -> IntentDraft:
    """Convert a manifest intent to ``IntentDraft`` for ``set_intents``.

    ``slots`` is left empty because the manifest grammar carries any
    ``slots:`` sub-block inline; :attr:`IntentDraft.rendered_source_text`
    detects that and avoids double-composition.
    """
    return IntentDraft(
        form_name=intent.form_name,
        human_readable_name=intent.human_readable_name,
        source_text=intent.grammar,
        positive_tests=intent.positive_tests,
        negative_tests=intent.negative_tests,
        is_activation=intent.is_activation,
    )


def _require_str(
    item: Mapping[str, Any],
    key: str,
    index: int,
    *,
    default: str,
) -> str:
    """Read an optional string field from an intent entry, strict on type.

    Returns ``default`` when the key is absent. When the key is
    present, requires a string value — raises
    :class:`SkillManifestError` otherwise (silent string-coercion
    would let ``grammar = 123`` produce ``"123"`` and miss the actual
    authoring bug).
    """
    if key not in item:
        return default
    value = item[key]
    if not isinstance(value, str):
        raise SkillManifestError(
            f"manifest: intents[{index}].{key} must be a string, "
            f"got {type(value).__name__}",
        )
    return value


def _require_bool(
    item: Mapping[str, Any],
    key: str,
    index: int,
    *,
    default: bool,
) -> bool:
    """Read an optional bool field from an intent entry, strict on type.

    TOML distinguishes booleans from strings, so a stray ``is_activation
    = "false"`` would otherwise be coerced via ``bool("false")`` →
    ``True`` (any non-empty string is truthy). Surface the type
    mismatch as a manifest error instead of silently flipping the flag.
    """
    if key not in item:
        return default
    value = item[key]
    if not isinstance(value, bool):
        raise SkillManifestError(
            f"manifest: intents[{index}].{key} must be a boolean, "
            f"got {type(value).__name__}",
        )
    return value


def _strip_block(value: str) -> str:
    r"""Normalise a triple-quoted TOML string to ``\n``-separated lines.

    Triple-quoted blocks in the manifest typically open with a newline
    immediately after ``\"\"\"`` (which TOML trims) and close with one
    before the final ``\"\"\"`` (which it does not). Strip leading /
    trailing whitespace so callers see a normalised value matching what
    Yandex stores in ``positiveTests`` / ``negativeTests``.
    """
    return value.strip()


# ---------------------------------------------------------------------------
# Granet ``customEntities`` DSL → list[EntityDraft]
# ---------------------------------------------------------------------------

# Entity block header — matches "entity <name>:" anchored to start of line.
# Used to split the customEntities sourceText into per-entity bodies.
_ENTITY_HEADER_RE = re.compile(r"^entity\s+(\S+)\s*:\s*$", re.MULTILINE)


def entities_to_drafts(entities_text: str) -> list[EntityDraft]:
    """Parse a Granet ``customEntities`` DSL blob into structured drafts.

    Lets a manifest carry the entities sourceText byte-for-byte (the
    same text the dev-console editor shows) while still feeding the
    structured ``set_entities()`` setter. Empty / whitespace input
    yields an empty list.

    Format expected (verbatim from the dev console)::

        entity time_unit:
            values:
                seconds:
                    секунда | секунды | сек
                minutes:
                    минута | минуты | мин

    :raises SkillManifestError: on syntactic surprises (unnamed entity,
        values block missing, value with no phrases).
    """
    if not entities_text or not entities_text.strip():
        return []
    parts = _ENTITY_HEADER_RE.split(entities_text)
    # Split layout: [leading_garbage, name1, body1, name2, body2, ...].
    # Length 1 means no header was matched; <3 means a malformed split
    # (regex group + body always come in pairs).
    leading = parts[0]
    if leading.strip():
        raise SkillManifestError(
            "entities: stray content before first 'entity <name>:' header: "
            f"{leading.strip()[:60]!r}",
        )
    if len(parts) < 3:  # noqa: PLR2004
        raise SkillManifestError(
            "entities: text is non-empty but no 'entity <name>:' header was found",
        )
    drafts: list[EntityDraft] = []
    for index in range(1, len(parts), 2):
        name = parts[index].strip()
        body = parts[index + 1]
        if not name:
            raise SkillManifestError("entities: empty entity name in header")
        values = _parse_entity_values(name, body)
        drafts.append(EntityDraft(name=name, values=tuple(values)))
    return drafts


def _parse_entity_values(entity_name: str, body: str) -> list[EntityValue]:
    """Parse the ``values:`` sub-block of one entity into EntityValue list."""
    seen_values_header = False
    current_name: str | None = None
    current_phrases: list[str] = []
    out: list[EntityValue] = []

    def _flush() -> None:
        nonlocal current_name, current_phrases
        if current_name is None:
            return
        if not current_phrases:
            raise SkillManifestError(
                f"entities[{entity_name}]: value {current_name!r} has no phrases",
            )
        out.append(EntityValue(name=current_name, phrases=tuple(current_phrases)))
        current_name = None
        current_phrases = []

    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped == "values:":
            seen_values_header = True
            continue
        if not seen_values_header:
            raise SkillManifestError(
                f"entities[{entity_name}]: expected 'values:' before {stripped!r}",
            )
        if stripped.endswith(":"):
            _flush()
            current_name = stripped[:-1].strip()
            if not current_name:
                raise SkillManifestError(
                    f"entities[{entity_name}]: empty value name",
                )
        else:
            phrases = [p.strip() for p in stripped.split("|") if p.strip()]
            if not phrases:
                continue
            current_phrases.extend(phrases)
    _flush()

    if not seen_values_header:
        raise SkillManifestError(
            f"entities[{entity_name}]: missing 'values:' sub-block",
        )
    if not out:
        raise SkillManifestError(f"entities[{entity_name}]: no values declared")
    return out
