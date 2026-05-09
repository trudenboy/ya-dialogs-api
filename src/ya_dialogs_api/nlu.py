"""Runtime helpers for the Yandex Dialogs NLU payload.

Encapsulates the *shape* of ``request.nlu.intents.<form_name>`` so a
calling application doesn't reach into raw dicts. If Yandex revises the
field naming (e.g. moves ``slots[name].value`` to
``slots[name].extracted_value``), only this module changes — every
downstream provider keeps the same calling pattern.

Typical usage::

    from ya_dialogs_api.nlu import iter_intent_matches

    nlu_intents = request_body.get("request", {}).get("nlu", {}).get("intents", {})
    for match in iter_intent_matches(nlu_intents):
        if match.form_name == "control.volume_set":
            level = match.slot_int("level")
            if level is not None:
                ...

Design notes:

* Iteration order follows the dict insertion order Yandex produces —
  the platform doesn't guarantee a stable order across requests, but
  for our conservative grammar set each phrase pattern lives in
  exactly one intent, so overlap is engineered out.
* Slot accessors return ``None`` on missing / wrong-type values rather
  than raising. That keeps caller code branchless and lets the
  webhook handler fall through to a regex / graceful response without
  exception handling.
* Booleans are explicitly rejected from numeric accessors because
  Python's ``isinstance(True, int)`` is true — without the guard, a
  slot whose payload accidentally carries a boolean would be coerced
  to ``0``/``1``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from typing import Any

__all__ = [
    "IntentMatch",
    "iter_intent_matches",
]


@dataclasses.dataclass(frozen=True, slots=True)
class IntentMatch:
    """Single ``request.nlu.intents.<form_name>`` payload, typed.

    Wraps the raw dict for a one matched intent so callers don't have
    to reach into nested mappings. Slot accessors are defensive — they
    return ``None`` on any structural surprise rather than raising.

    :param form_name: Stable intent identifier as authored on the skill
        (``IntentDraft.form_name`` / Yandex API ``formName``).
    :param payload: Raw mapping from ``request.nlu.intents[form_name]``.
        Treat as opaque; access via :meth:`slot_int` / :meth:`slot_str`
        / :meth:`raw_slot`.
    """

    form_name: str
    payload: Mapping[str, Any]

    def _slots(self) -> Mapping[str, Any] | None:
        slots = self.payload.get("slots")
        return slots if isinstance(slots, Mapping) else None

    def has_slot(self, name: str) -> bool:
        """Return ``True`` if a slot named ``name`` is present in the payload."""
        slots = self._slots()
        return slots is not None and name in slots

    def raw_slot(self, name: str) -> Mapping[str, Any] | None:
        """Return the raw ``slots[name]`` mapping, or ``None`` if absent."""
        slots = self._slots()
        if slots is None:
            return None
        slot = slots.get(name)
        return slot if isinstance(slot, Mapping) else None

    def slot_value(self, name: str) -> Any:
        """Return ``slots[name].value`` (any type), or ``None`` if absent."""
        slot = self.raw_slot(name)
        if slot is None:
            return None
        return slot.get("value")

    def slot_int(self, name: str) -> int | None:
        """Return ``slots[name].value`` coerced to ``int``, or ``None``.

        Yandex's ``YANDEX.NUMBER`` slot can come back as either ``int``
        (whole numbers like 30) or ``float`` (fractional numbers like
        3.5). Both are coerced via ``int(...)``. Boolean values are
        rejected — Python's ``isinstance(True, int)`` is true, but a
        boolean here means the payload's slot type isn't numeric.

        Returns ``None`` when the slot is missing, the value is of an
        unexpected type, or the value isn't a number.
        """
        value = self.slot_value(name)
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return int(value)
        return None

    def slot_str(self, name: str) -> str | None:
        """Return ``slots[name].value`` if it's a string, else ``None``.

        Used for custom entity slots (e.g. a ``time_unit`` slot whose
        value is the entity-value name like ``"seconds"``) and for
        ``YANDEX.STRING`` slots.
        """
        value = self.slot_value(name)
        return value if isinstance(value, str) else None


def iter_intent_matches(
    nlu_intents: Mapping[str, Any] | None,
) -> Iterator[IntentMatch]:
    """Iterate over the ``request.nlu.intents`` block.

    Yields one :class:`IntentMatch` per ``form_name`` Yandex matched on
    the user's utterance, in dict insertion order. Empty / missing /
    malformed input yields nothing — callers don't need to null-check
    before calling.

    :param nlu_intents: ``request.nlu.intents`` mapping from the
        Yandex Dialogs webhook envelope. Pass the value as-received
        from JSON parsing; the function tolerates ``None`` and
        non-mapping inputs.
    """
    if not isinstance(nlu_intents, Mapping):
        return
    for form_name, payload in nlu_intents.items():
        # Defensive: while ``Mapping[str, Any]`` types ``form_name`` as
        # ``str``, the runtime payload comes from JSON parsing — keys
        # could in principle be other types if a custom decoder is in
        # use. Skip anything that isn't a non-empty string.
        if not isinstance(form_name, str) or not form_name:  # type: ignore[redundant-expr]
            continue
        # Yandex always sends a dict, but callers might unpack JSON via
        # libraries that produce different mapping types — accept any
        # Mapping. Wrap empty dict for missing-payload defence.
        wrapped: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        yield IntentMatch(form_name=form_name, payload=wrapped)
