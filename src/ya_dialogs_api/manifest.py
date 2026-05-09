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
    "ManifestMapping",
    "ManifestMultiplyWhen",
    "ManifestRuntime",
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
class ManifestMultiplyWhen:
    """Conditional multiplier on a slot-derived numeric field.

    Multiplies the mapped field value by ``factor`` when the slot
    named ``slot`` carries the string value ``equals``. Used to
    convert unit-bearing numeric slots (e.g. seek with ``unit=minutes``
    multiplies amount by 60 to yield seconds).

    :param slot: slot name to inspect (string value).
    :param equals: slot value that triggers the multiplier.
    :param factor: integer factor to multiply the mapped field by.
    """

    slot: str
    equals: str
    factor: int


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestMapping:
    """Single rule mapping an NLU slot to a target dataclass field.

    Lives inside :class:`ManifestRuntime`. The companion runtime
    dispatcher (:func:`ya_dialogs_api.runtime.apply_runtime_mapping`)
    interprets the rule against an :class:`~ya_dialogs_api.IntentMatch`
    and yields a value (or signals a skip).

    :param field: name of the field on the consumer's target dataclass
        the mapping populates (used as a ``**kwargs`` key).
    :param from_slot: slot name in the NLU payload to read.
    :param slot_type: ``"int"`` to read via :meth:`IntentMatch.slot_int`
        or ``"str"`` for :meth:`IntentMatch.slot_str`.
    :param transform: ``"identity"`` (no-op), ``"clamp"`` (clamp to
        [min, max]), or ``"abs_clamp"`` (clamp ``abs(value)`` to
        [min, max] — useful when sign is forced separately).
    :param min: lower bound for clamp/abs_clamp transforms.
    :param max: upper bound for clamp/abs_clamp transforms.
    :param cap: skip the intent entirely if the post-multiplication
        value exceeds this. ``None`` disables. Used to reject obviously
        misclassified utterances ("seek 30000 seconds").
    :param default: value to use when the slot is missing. ``None``
        means "skip the intent on missing slot".
    :param sign: ``"positive"`` (default) or ``"negative"``. When
        ``"negative"``, the final value is negated — used to encode
        direction (volume_decrease) without a signed slot type.
    :param reject_if_below: skip the intent when the post-multiplication
        value is below this threshold. ``None`` disables.
    :param multiply_when: tuple of conditional multipliers applied
        before sign / clamp / cap.
    """

    field: str
    from_slot: str
    slot_type: str = "int"
    transform: str = "identity"
    min: int | None = None
    max: int | None = None
    cap: int | None = None
    default: int | None = None
    sign: str = "positive"
    reject_if_below: int | None = None
    multiply_when: tuple[ManifestMultiplyWhen, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestRuntime:
    """Runtime dispatch metadata for a matched intent.

    Carries the consumer-application-level semantics: which command
    family the intent belongs to (``kind``), which specific action
    (``action``), and how to map slots into the target dataclass
    (``mapping``).

    Both ``kind`` and ``action`` are opaque strings to this library —
    the consumer interprets them (e.g. as ``Literal`` values for a
    ``ParsedControl`` / ``ParsedCommand`` dataclass).

    :param kind: opaque tag for the intent's command family
        (e.g. ``"control"``, ``"play"``).
    :param action: opaque tag for the specific action within ``kind``.
    :param mapping: tuple of slot-to-field rules. Empty for no-slot
        intents (the dispatcher returns an empty fields dict).
    """

    kind: str
    action: str
    mapping: tuple[ManifestMapping, ...] = ()


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

    ``runtime`` (added in schema 1) carries optional dispatch metadata
    for consumers using :func:`ya_dialogs_api.runtime.apply_runtime_mapping`.
    ``None`` means the intent is matched by Yandex but the consumer
    does not declare a runtime mapping for it (e.g. handled in
    application code by another path).
    """

    form_name: str
    human_readable_name: str = ""
    is_activation: bool = False
    positive_tests: str = ""
    negative_tests: str = ""
    grammar: str = ""
    runtime: ManifestRuntime | None = None


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
                human_readable_name=_require_str(item, "human_readable_name", index, default=""),
                is_activation=_require_bool(item, "is_activation", index, default=False),
                positive_tests=_strip_block(
                    _require_str(item, "positive_tests", index, default="")
                ),
                negative_tests=_strip_block(
                    _require_str(item, "negative_tests", index, default="")
                ),
                grammar=_require_str(item, "grammar", index, default=""),
                runtime=_parse_runtime(item.get("runtime"), index),
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
            f"manifest: intents[{index}].{key} must be a string, got {type(value).__name__}",
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
            f"manifest: intents[{index}].{key} must be a boolean, got {type(value).__name__}",
        )
    return value


def _require_int_or_none(
    item: Mapping[str, Any],
    key: str,
    where: str,
) -> int | None:
    """Read an optional int field, strict on type.

    Returns ``None`` when the key is absent. ``where`` is a short
    location tag for the error message (e.g.
    ``"intents[3].runtime.mapping[1]"``).
    """
    if key not in item:
        return None
    value = item[key]
    # Reject booleans even though Python says ``isinstance(True, int)``.
    if isinstance(value, bool):
        raise SkillManifestError(
            f"manifest: {where}.{key} must be an integer, got bool",
        )
    if not isinstance(value, int):
        raise SkillManifestError(
            f"manifest: {where}.{key} must be an integer, got {type(value).__name__}",
        )
    return value


_VALID_TRANSFORMS = frozenset({"identity", "clamp", "abs_clamp"})
_VALID_SLOT_TYPES = frozenset({"int", "str"})
_VALID_SIGNS = frozenset({"positive", "negative"})


def _parse_runtime(raw: Any, intent_index: int) -> ManifestRuntime | None:
    """Convert the ``runtime`` table of a single intent into a dataclass.

    Returns ``None`` when the runtime block is absent (intent has no
    declared dispatch). Raises :class:`SkillManifestError` on
    structural surprises.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SkillManifestError(
            f"manifest: intents[{intent_index}].runtime must be a TOML table, "
            f"got {type(raw).__name__}",
        )
    where = f"intents[{intent_index}].runtime"
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise SkillManifestError(f"manifest: {where}.kind (non-empty str) is required")
    action = raw.get("action")
    if not isinstance(action, str) or not action:
        raise SkillManifestError(f"manifest: {where}.action (non-empty str) is required")
    mapping_raw = raw.get("mapping") or []
    if not isinstance(mapping_raw, list):
        raise SkillManifestError(
            f"manifest: {where}.mapping must be an array of tables",
        )
    mappings: list[ManifestMapping] = []
    for m_index, m_item in enumerate(mapping_raw):
        if not isinstance(m_item, Mapping):
            raise SkillManifestError(
                f"manifest: {where}.mapping[{m_index}] must be a TOML table",
            )
        mappings.append(_parse_mapping(m_item, intent_index, m_index))
    return ManifestRuntime(kind=kind, action=action, mapping=tuple(mappings))


def _parse_mapping(
    item: Mapping[str, Any],
    intent_index: int,
    mapping_index: int,
) -> ManifestMapping:
    """Convert one ``runtime.mapping[*]`` entry into a dataclass."""
    where = f"intents[{intent_index}].runtime.mapping[{mapping_index}]"
    field = item.get("field")
    if not isinstance(field, str) or not field:
        raise SkillManifestError(f"manifest: {where}.field (non-empty str) is required")
    from_slot = item.get("from_slot")
    if not isinstance(from_slot, str) or not from_slot:
        raise SkillManifestError(f"manifest: {where}.from_slot (non-empty str) is required")
    slot_type = item.get("slot_type", "int")
    if slot_type not in _VALID_SLOT_TYPES:
        raise SkillManifestError(
            f"manifest: {where}.slot_type must be one of {sorted(_VALID_SLOT_TYPES)}, "
            f"got {slot_type!r}",
        )
    transform = item.get("transform", "identity")
    if transform not in _VALID_TRANSFORMS:
        raise SkillManifestError(
            f"manifest: {where}.transform must be one of {sorted(_VALID_TRANSFORMS)}, "
            f"got {transform!r}",
        )
    sign = item.get("sign", "positive")
    if sign not in _VALID_SIGNS:
        raise SkillManifestError(
            f"manifest: {where}.sign must be one of {sorted(_VALID_SIGNS)}, got {sign!r}",
        )
    multiply_when_raw = item.get("multiply_when") or []
    if not isinstance(multiply_when_raw, list):
        raise SkillManifestError(
            f"manifest: {where}.multiply_when must be an array of tables",
        )
    multiply_when: list[ManifestMultiplyWhen] = []
    for w_index, w_item in enumerate(multiply_when_raw):
        if not isinstance(w_item, Mapping):
            raise SkillManifestError(
                f"manifest: {where}.multiply_when[{w_index}] must be a TOML table",
            )
        multiply_when.append(_parse_multiply_when(w_item, where, w_index))
    return ManifestMapping(
        field=field,
        from_slot=from_slot,
        slot_type=slot_type,
        transform=transform,
        min=_require_int_or_none(item, "min", where),
        max=_require_int_or_none(item, "max", where),
        cap=_require_int_or_none(item, "cap", where),
        default=_require_int_or_none(item, "default", where),
        sign=sign,
        reject_if_below=_require_int_or_none(item, "reject_if_below", where),
        multiply_when=tuple(multiply_when),
    )


def _parse_multiply_when(
    item: Mapping[str, Any],
    parent_where: str,
    index: int,
) -> ManifestMultiplyWhen:
    """Convert one ``mapping.multiply_when[*]`` entry into a dataclass."""
    where = f"{parent_where}.multiply_when[{index}]"
    slot = item.get("slot")
    if not isinstance(slot, str) or not slot:
        raise SkillManifestError(f"manifest: {where}.slot (non-empty str) is required")
    equals = item.get("equals")
    if not isinstance(equals, str) or not equals:
        raise SkillManifestError(f"manifest: {where}.equals (non-empty str) is required")
    factor = item.get("factor")
    if isinstance(factor, bool) or not isinstance(factor, int):
        raise SkillManifestError(f"manifest: {where}.factor must be an integer")
    return ManifestMultiplyWhen(slot=slot, equals=equals, factor=factor)


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
