"""Typed errors for dialogs.yandex.ru API failures.

Yandex returns four distinct error shapes that must each be parsed differently:

1. **Spring ``dispatcherServlet`` errors** — leaked from the underlying
   service when an endpoint or method does not exist::

       {"servlet": "dispatcherServlet",
        "message": "Skill not found with id: ...",
        "url": "/api/dev-console/v1/...",
        "status": "404"}

2. **Domain validation errors** — application-level rejections, e.g. a draft
   field that violates a content rule::

       {"error": {"message": "Validation error",
                  "code": 400,
                  "fields": {"name": "Название должно содержать минимум два слова"}}}

3. **Plain JSON errors** — older shape, single ``error``/``errorCode``/
   ``message``/``code`` field at top level.

4. **HTML 403** — the dev-console returns ``<!DOCTYPE html>...<pre>Forbidden</pre>``
   when the CSRF token is missing or invalid (mutating requests only).

This module exposes:

- :class:`DialogsApiError` — base.
- :class:`DialogsAuthError` — HTML 403 / 401, redirect-to-Passport.
- :class:`DialogsValidationError` — domain-level field validation.
- :class:`DialogsSkillNotFoundError` — 404 on an existing skill route.
- :class:`DialogsCsrfError` — CSRF extraction failure (raised before any
  network call to a JSON endpoint).
- :class:`DialogsDuplicateSkillError` — special-case 409/4xx on ``create_app``
  when the skill name is already taken.
- :func:`parse_error_body` — best-effort parser returning the most specific
  exception type derivable from an error body.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "DialogsApiError",
    "DialogsAuthError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsIntentValidationError",
    "DialogsSkillNotFoundError",
    "DialogsValidationError",
    "parse_error_body",
]


class DialogsApiError(Exception):
    """Base error for dialogs.yandex.ru API failures."""

    def __init__(
        self,
        message: str,
        *,
        step: str,
        http_status: int | None = None,
        yandex_error: str | None = None,
    ) -> None:
        """Initialise with the pipeline step that failed for clearer messages."""
        super().__init__(message)
        self.step = step
        self.http_status = http_status
        self.yandex_error = yandex_error


class DialogsCsrfError(DialogsApiError):
    """Raised when the CSRF token cannot be extracted from the developer page."""


class DialogsAuthError(DialogsApiError):
    """Raised when Yandex rejects the request as unauthenticated.

    Triggers:

    - ``HTTP 401`` from any endpoint (cookies missing/expired).
    - ``HTTP 403`` with HTML body (CSRF expired / missing on a mutating request).
    - ``HTTP 30x`` redirect to ``passport.yandex.ru`` from
      ``GET /developer`` (anonymous access — caller forgot to provide cookies).

    Catching this is the signal to refresh Passport cookies and / or re-fetch
    the CSRF token before retrying.
    """


class DialogsValidationError(DialogsApiError):
    """Raised when Yandex rejects a payload at the domain level.

    Carries the per-field error map when present in the body
    (`error.fields` shape), so callers can render targeted messages.
    """

    def __init__(
        self,
        message: str,
        *,
        step: str,
        http_status: int | None = None,
        yandex_error: str | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        """Initialise with optional per-field error map."""
        super().__init__(
            message,
            step=step,
            http_status=http_status,
            yandex_error=yandex_error,
        )
        self.fields = fields or {}


class DialogsSkillNotFoundError(DialogsApiError):
    """Raised when an endpoint reports the requested skill_id does not exist.

    Detected from the Spring servlet message ``"Skill not found with id: <uuid>"``.
    """

    def __init__(
        self,
        message: str,
        *,
        step: str,
        skill_id: str | None = None,
        http_status: int | None = None,
        yandex_error: str | None = None,
    ) -> None:
        """Initialise with the missing skill_id when extractable."""
        super().__init__(
            message,
            step=step,
            http_status=http_status,
            yandex_error=yandex_error,
        )
        self.skill_id = skill_id


class DialogsDuplicateSkillError(DialogsApiError):
    """Raised when create_app rejects because a skill with the same name exists."""


class DialogsIntentValidationError(DialogsValidationError):
    """Raised when Yandex rejects an intent grammar at PATCH time.

    Custom-intent updates use a soft-failure protocol: HTTP 200 with the
    saved-but-invalid intent and a ``validationError`` block describing
    what was wrong with the grammar. This error wraps that block so
    callers can inspect ``error_code``, the human-readable ``text``, and
    the error position in ``sourceText`` (for editor highlighting).

    Position fields use ``-1`` when Yandex couldn't pin the location
    (typically: empty grammar, or top-level structural errors).
    """

    def __init__(
        self,
        message: str,
        *,
        step: str,
        error_code: str,
        char_count: int,
        char_offset: int,
        line_number: int,
        intent_id: str | None = None,
        form_name: str | None = None,
    ) -> None:
        """Initialise with the validation block details."""
        # Build a richer textual representation that surfaces the
        # offending intent's form_name and error position. The base
        # ``message`` carries the human-readable error from Yandex (e.g.
        # "Неизвестный элемент 'root'" or "Некорректный аргумент"); on
        # its own that's not enough to find the failing intent in a
        # multi-grammar set_intents() call. We prepend ``[form_name]`` /
        # ``[intent_id]`` and append the position when it's known.
        parts: list[str] = []
        if form_name:
            parts.append(f"[{form_name}]")
        elif intent_id:
            parts.append(f"[id={intent_id}]")
        parts.append(message)
        if line_number > 0 or char_offset >= 0:
            location = f"line={line_number} offset={char_offset}"
            if char_count >= 0:
                location += f" len={char_count}"
            parts.append(f"({location})")
        if error_code and error_code != "VALIDATION_ERROR":
            parts.append(f"[{error_code}]")
        enriched = " ".join(parts)
        super().__init__(enriched, step=step, yandex_error=message)
        self.error_code = error_code
        self.char_count = char_count
        self.char_offset = char_offset
        self.line_number = line_number
        self.intent_id = intent_id
        self.form_name = form_name


# ---------------------------------------------------------------------------
# Body parsers
# ---------------------------------------------------------------------------

# Spring "Skill not found with id: <uuid>" pattern
_SPRING_SKILL_NOT_FOUND_RE = re.compile(
    r"Skill\s+not\s+found\s+with\s+id:\s*([0-9a-f-]{20,})",
    re.IGNORECASE,
)


def _try_json(body: str) -> Any:
    """Parse JSON defensively — return ``None`` on any error or empty input."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _looks_like_html(body: str) -> bool:
    """Heuristic for HTML error body (e.g. ``<!DOCTYPE html>...<pre>Forbidden</pre>``)."""
    if not body:
        return False
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<pre>" in head


def parse_error_body(
    body: str,
    *,
    http_status: int,
    step: str,
) -> DialogsApiError:
    """Best-effort parser: return the most specific exception derivable from *body*.

    Order of precedence:

    1. **HTTP 401 (any body)** → :class:`DialogsAuthError`. 401 is unambiguously
       an authentication signal per HTTP semantics; body shape is irrelevant.
    2. **HTML 403** → :class:`DialogsAuthError` (CSRF/cookies-expired auth wall).
       Non-HTML 403 falls through to body-based parsing — Yandex also uses 403
       for domain-level "forbidden" responses.
    3. **Spring servlet 404 with "Skill not found"** → :class:`DialogsSkillNotFoundError`.
    4. **Spring servlet 4xx/5xx** → :class:`DialogsValidationError` (404/400) or
       :class:`DialogsApiError` (else), with ``yandex_error`` populated from
       ``message``.
    5. **Domain validation** ``{error: {message, code, fields}}`` →
       :class:`DialogsValidationError` with ``fields`` filled.
    6. **Top-level JSON error** ``{error|errorCode|message|code: "..."}`` →
       :class:`DialogsApiError` with ``yandex_error`` populated.
    7. Otherwise — generic :class:`DialogsApiError`.
    """
    # 1. HTTP 401 always means auth, regardless of body shape.
    if http_status == 401:
        return DialogsAuthError(
            f"Yandex returned HTTP 401 — CSRF / cookies missing or expired: "
            f"{body[:200] or '<empty>'}",
            step=step,
            http_status=http_status,
        )
    # 2. HTML 403 is the auth-wall variant (non-HTML 403 falls through).
    if http_status == 403 and _looks_like_html(body):
        return DialogsAuthError(
            "Yandex returned HTML auth response — CSRF / cookies missing or expired",
            step=step,
            http_status=http_status,
        )

    data = _try_json(body)
    if not isinstance(data, dict):
        # 6. Unknown body shape
        return DialogsApiError(
            f"HTTP {http_status}: {body[:200] or '<empty>'}",
            step=step,
            http_status=http_status,
        )

    # 2./3. Spring dispatcherServlet
    if data.get("servlet") == "dispatcherServlet":
        message_raw = data.get("message")
        message = message_raw if isinstance(message_raw, str) else f"HTTP {http_status}"

        # 2. Specific: skill not found
        if http_status == 404:
            match = _SPRING_SKILL_NOT_FOUND_RE.search(message)
            if match:
                return DialogsSkillNotFoundError(
                    message,
                    step=step,
                    skill_id=match.group(1),
                    http_status=http_status,
                    yandex_error=message,
                )

        # 3. Generic Spring error — treat 4xx as validation, others as base
        if 400 <= http_status < 500:
            return DialogsValidationError(
                message,
                step=step,
                http_status=http_status,
                yandex_error=message,
            )
        return DialogsApiError(
            message,
            step=step,
            http_status=http_status,
            yandex_error=message,
        )

    # 4. Domain validation envelope
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        message_raw = error_obj.get("message")
        message = message_raw if isinstance(message_raw, str) else f"HTTP {http_status}"
        fields_raw = error_obj.get("fields")
        fields: dict[str, str] = {}
        if isinstance(fields_raw, dict):
            fields = {
                str(k): str(v) for k, v in fields_raw.items() if isinstance(v, str | int | float)
            }
        return DialogsValidationError(
            message,
            step=step,
            http_status=http_status,
            yandex_error=message,
            fields=fields,
        )

    # 5. Top-level error string
    for key in ("error", "errorCode", "message", "code"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return DialogsApiError(
                value,
                step=step,
                http_status=http_status,
                yandex_error=value,
            )

    # 6. Fallback
    return DialogsApiError(
        f"HTTP {http_status}: {body[:200] or '<empty>'}",
        step=step,
        http_status=http_status,
    )
