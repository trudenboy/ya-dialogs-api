"""Generic dispatcher applying ``ManifestRuntime`` rules to an ``IntentMatch``.

Sits between the manifest format (which describes what slots an intent
carries and how each one maps to a target dataclass field) and the
consumer's runtime (which builds its own command dataclasses out of
those mapped fields).

Design:

* The dispatcher is **opaque about target types** — it returns a
  ``dict[str, Any]`` keyed by ``ManifestMapping.field`` that the
  consumer feeds into its own ``**kwargs`` constructor. The library
  doesn't know or care whether the consumer's target is a
  ``ParsedControl`` dataclass, a Pydantic model, or a plain function
  call.
* Returns ``None`` when the rule says the intent should be skipped
  (slot missing without default, value below ``reject_if_below``,
  value above ``cap``). This signals the consumer to fall through to
  the next intent / regex parser / graceful response — exception-free.
* Transforms are pinned to the closed set documented on
  :class:`ManifestMapping`. The parser already rejects unknown
  transform / slot_type / sign values at manifest load time, so the
  dispatcher can assume valid inputs.

Typical usage from a consumer::

    for match in iter_intent_matches(nlu_intents):
        intent = manifest_by_form.get(match.form_name)
        if intent is None or intent.runtime is None:
            continue
        fields = apply_runtime_mapping(match, intent.runtime)
        if fields is None:
            continue   # skip — slot missing / out of range
        if intent.runtime.kind == "control":
            return ParsedControl(action=intent.runtime.action, **fields)
        ...
"""

from __future__ import annotations

from typing import Any

from .manifest import ManifestMapping, ManifestRuntime
from .nlu import IntentMatch

__all__ = [
    "RuntimeMappingError",
    "apply_runtime_mapping",
]


class RuntimeMappingError(ValueError):
    """Raised when a :class:`ManifestMapping` cannot be applied.

    Reserved for misconfigurations that the manifest parser couldn't
    catch — currently only an unknown ``slot_type`` reaching the
    dispatcher (the parser already pins ``slot_type`` to a closed
    set, so this is purely defensive). Slot-level surprises (missing
    slot without default, value below ``reject_if_below``, value
    above ``cap``) are signalled via ``None`` return, not exceptions.
    """


def apply_runtime_mapping(
    match: IntentMatch,
    runtime: ManifestRuntime,
) -> dict[str, Any] | None:
    """Apply a runtime's mapping rules to an intent match.

    Returns a dict keyed by each :attr:`ManifestMapping.field` (suitable
    as ``**kwargs`` into the consumer's target dataclass), or ``None``
    if any rule signals "skip this intent" (slot missing, out of range,
    cap exceeded).

    No-mapping runtimes (``runtime.mapping == ()``) yield an empty
    dict — useful for no-slot intents like ``control.pause`` where
    the consumer just needs the ``action`` and no extra fields.
    """
    fields: dict[str, Any] = {}
    for rule in runtime.mapping:
        result = _apply_single(match, rule)
        if result is _SKIP:
            return None
        fields[rule.field] = result
    return fields


# Sentinel to distinguish "skip the intent" (return None upstream) from
# a legitimate ``None`` value in fields (no current rule produces a
# None field, but stay forward-compatible).
class _Skip:
    __slots__ = ()


_SKIP = _Skip()


def _apply_single(match: IntentMatch, rule: ManifestMapping) -> Any:
    """Apply one mapping rule. Return ``_SKIP`` to signal whole-intent skip."""
    if rule.slot_type == "int":
        raw_int = match.slot_int(rule.from_slot)
        if raw_int is None:
            if rule.default is None:
                return _SKIP
            raw_int = rule.default
        return _apply_numeric(match, rule, raw_int)
    if rule.slot_type == "str":
        raw_str = match.slot_str(rule.from_slot)
        if raw_str is None:
            return _SKIP
        return raw_str
    # Parser validates slot_type; defensive only.
    raise RuntimeMappingError(
        f"unknown slot_type {rule.slot_type!r} on field {rule.field!r}",
    )


def _apply_numeric(match: IntentMatch, rule: ManifestMapping, value: int) -> Any:
    """Numeric branch: multiply_when → reject_if_below → cap → transform → sign."""
    # 1. multiply_when (unit conversion).
    for cond in rule.multiply_when:
        if match.slot_str(cond.slot) == cond.equals:
            value *= cond.factor

    # 2. reject_if_below — pre-transform lower-bound guard. Compares
    # the raw (signed) value, not abs(value); a negative slot value
    # below the threshold also rejects.
    if rule.reject_if_below is not None and value < rule.reject_if_below:
        return _SKIP

    # 3. cap — pre-transform upper-bound guard. Compares the raw
    # (signed) value; rejects obvious misclassifications like
    # "seek 30000 seconds" before clamp normalises them.
    if rule.cap is not None and value > rule.cap:
        return _SKIP

    # 4. transform.
    if rule.transform == "clamp":
        value = _clamp(value, rule)
    elif rule.transform == "abs_clamp":
        value = _clamp(abs(value), rule)
    # "identity" — no-op.

    # 5. sign.
    if rule.sign == "negative":
        value = -value
    return value


def _clamp(value: int, rule: ManifestMapping) -> int:
    """Clamp ``value`` to ``[rule.min, rule.max]`` when bounds are set.

    Either bound may be ``None``, in which case that side is
    unbounded. ``min`` > ``max`` is permissible (caller's responsibility
    — parser doesn't enforce ordering).
    """
    if rule.min is not None and value < rule.min:
        value = rule.min
    if rule.max is not None and value > rule.max:
        value = rule.max
    return value
