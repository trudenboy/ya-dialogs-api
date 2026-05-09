"""Low-level client for the undocumented dialogs.yandex.ru developer API.

Implements the captured-from-HAR sequence for creating a Yandex Dialogs skill:

    1. GET   /developer                                            → extract CSRF (secretkey)
    2. GET   /developer/app-store-api/snapshot                      → existing skills list
    3. POST  /developer/app-store-api/apps                          → skill_id
    4. POST  /developer/app-store-api/apps/{id}/draft/upload-logo   → logo_id
    5. PATCH /developer/app-store-api/apps/{id}/draft/update        → settings
    6. POST  /developer/app-store-api/oauth/apps                    → oauth_app_id (Smart Home only)
    7. POST  /developer/app-store-api/apps/{id}/oauthApp            → bind oauth (Smart Home only)
    8. POST  /developer/app-store-api/apps/{id}/draft/request-deploy → publish
    9. DELETE /developer/app-store-api/apps/{id}?channel=...         → delete

This is an UNDOCUMENTED, PRIVATE API. It may break at any time. The
caller is responsible for surfacing that risk to the user.

Authentication: passport session cookies (``Session_id`` / ``sessionid2``)
must already be present in the supplied ``aiohttp.ClientSession``'s
cookie jar. Obtain them via ``ya_passport_auth.PassportClient``.

The CSRF token (returned by ``fetch_csrf``) must be passed as the
``x-csrf-token`` header on every mutating request.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.resources
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

import aiohttp

from .errors import (
    DialogsApiError,
    DialogsAuthError,
    DialogsCsrfError,
    DialogsDuplicateSkillError,
    DialogsEntitiesValidationError,
    DialogsIntentValidationError,
    DialogsSkillNotFoundError,
    DialogsValidationError,
    parse_error_body,
)
from .state import SkillCreationArtifacts, SkillCreationState

# ---------------------------------------------------------------------------
# Channel — Yandex API wire values (sent in payloads + query strings)
# ---------------------------------------------------------------------------

Channel = Literal["smartHome", "aliceSkill"]

SMART_HOME_CHANNEL: Channel = "smartHome"
DIALOG_CHANNEL: Channel = "aliceSkill"

# A caller-provided context manager factory yielding an authorized aiohttp session.
# The session must already carry Yandex Passport cookies (built via Device Flow,
# QR login, or any other means — the lib doesn't care).
AuthenticatorCM = Callable[[], AbstractAsyncContextManager[aiohttp.ClientSession]]

__all__ = [
    "DEVICE_FLOW_TIMEOUT_SECONDS",
    "DIALOGS_API_BASE",
    "DIALOGS_CSRF_REGEX",
    "DIALOGS_DEV_BASE",
    "DIALOGS_DEV_HTML_URL",
    "DIALOG_CHANNEL",
    "SMART_HOME_CHANNEL",
    "AuthenticatorCM",
    "Channel",
    "DialogsApiError",
    "DialogsAuthError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsEntitiesValidationError",
    "DialogsIntentValidationError",
    "DialogsSkillCreator",
    "DialogsSkillNotFoundError",
    "DialogsValidationError",
    "EntityDraft",
    "EntityValue",
    "IntentDraft",
    "SlotDeclaration",
    "auto_create_skill",
    "auto_update_skill",
    "build_dialog_draft_payload",
    "build_oauth_app_payload",
    "build_smart_home_draft_payload",
    "load_default_logo_bytes",
]

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints / patterns
# ---------------------------------------------------------------------------
DIALOGS_DEV_BASE = "https://dialogs.yandex.ru"
DIALOGS_DEV_HTML_URL = f"{DIALOGS_DEV_BASE}/developer"
DIALOGS_API_BASE = f"{DIALOGS_DEV_BASE}/developer/app-store-api"

# The developer console embeds a CSRF token in its HTML as:
#   ..."secretkey":"u9c94f1aca53bf156be4..."...
# Captured from HAR 2026-04-24 and re-verified 2026-05-06. If Yandex re-renders
# differently, this regex will miss and ``fetch_csrf`` raises ``DialogsCsrfError``.
DIALOGS_CSRF_REGEX = re.compile(r'"secretkey":"([^"]+)"')

_MAX_HTML_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# ---------------------------------------------------------------------------
# Custom-intent grammar support (aliceSkill channel only)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SlotDeclaration:
    """Typed slot declaration attached to an :class:`IntentDraft`.

    Slots are the structured parameters that Yandex extracts from a
    matched utterance and surfaces under
    ``request.nlu.intents.<form_name>.slots.<name>`` to the webhook.
    Declaring them programmatically lets the library compose the
    corresponding ``slots:`` block of DSL into the intent's
    ``sourceText`` automatically — see
    :attr:`IntentDraft.rendered_source_text` — so callers can keep the
    structured definition and the grammar in lockstep.

    :param name: Slot key (lowercase identifier, e.g. ``"level"``).
    :param type: Slot type: a built-in (``"YANDEX.NUMBER"``,
        ``"YANDEX.DATETIME"``, ``"YANDEX.STRING"``, ``"YANDEX.GEO"``,
        ``"YANDEX.FIO"``) or the ``name`` of a custom entity declared
        on the same skill.
    :param source: Grammar non-terminal capturing the value, written
        with its leading ``$`` (e.g. ``"$Level"``). Must reference a
        non-terminal defined elsewhere in :attr:`IntentDraft.source_text`.
    """

    name: str
    type: str
    source: str


# Detect a pre-existing ``slots:`` block in a hand-rolled DSL, so
# rendering doesn't double up. Anchored to the start of a line, with
# optional trailing whitespace.
_SLOTS_BLOCK_RE = re.compile(r"^slots:\s*$", re.MULTILINE)


def _render_slots_block(slots: tuple[SlotDeclaration, ...]) -> str:
    """Render a tuple of :class:`SlotDeclaration` as a Yandex ``slots:`` DSL block.

    Indentation matches the convention used by the dev console: four
    spaces for slot keys, eight for ``type:`` / ``source:`` lines.
    Empty input returns the empty string (caller short-circuits before
    appending).
    """
    if not slots:
        return ""
    lines = ["slots:"]
    for slot in slots:
        lines.append(f"    {slot.name}:")
        lines.append(f"        type: {slot.type}")
        lines.append(f"        source: {slot.source}")
    return "\n".join(lines)


@dataclasses.dataclass(frozen=True, slots=True)
class IntentDraft:
    """Single Yandex Dialogs custom-intent definition.

    Maps 1:1 to the API payload at ``/apps/{id}/intents/{id}/draft``. The
    intent system is part of the dialog channel (``aliceSkill``) only;
    smart-home skills don't have a custom NLU layer.

    Two identifiers, both meaningful:

    * ``form_name`` — developer-facing string used in grammar source
      (``intent: play.specific``) and surfaced as ``request.nlu.intents.<form_name>``
      to the skill webhook. Stable across edits — the diff/upsert protocol
      in :meth:`DialogsSkillCreator.set_intents` keys on this.
    * ``intent_id`` — server-assigned UUID, returned by ``create_intent``
      and required for ``update_intent`` / ``delete_intent``. ``None`` for
      a freshly-built local definition.

    ``status`` is set by the server: ``NEW`` for a freshly-saved valid
    intent, ``INVALID_GRAMMAR`` when the most recent PATCH carried bad
    grammar (the previous valid version stays effective). Other values
    documented sparsely by Yandex; treat as opaque.

    ``slots`` carries structured slot declarations; their DSL
    representation is composed into ``sourceText`` at serialise time
    via :attr:`rendered_source_text`. If ``source_text`` already
    contains a ``slots:`` block (e.g. the author hand-rolled it, or the
    intent came back from :meth:`from_api_dict`), the structured
    declarations are silently ignored at render time so the block is
    never duplicated.
    """

    form_name: str
    human_readable_name: str = ""
    source_text: str = ""
    positive_tests: str = ""
    negative_tests: str = ""
    is_activation: bool = False
    slots: tuple[SlotDeclaration, ...] = ()
    intent_id: str | None = None
    status: str = "NEW"

    @property
    def rendered_source_text(self) -> str:
        """``source_text`` with the structured slots block composed in.

        Returns ``source_text`` verbatim when there are no structured
        slots, or when ``source_text`` already contains its own
        ``slots:`` line. Otherwise appends a generated ``slots:`` block
        built from :attr:`slots`. The result is what
        :meth:`to_api_dict` ships to the server, and what
        :meth:`DialogsSkillCreator.set_intents` diffs against the
        server-side ``sourceText``.
        """
        if not self.slots:
            return self.source_text
        if _SLOTS_BLOCK_RE.search(self.source_text):
            return self.source_text
        block = _render_slots_block(self.slots)
        if not self.source_text:
            return f"{block}\n"
        sep = "" if self.source_text.endswith("\n") else "\n"
        return f"{self.source_text}{sep}{block}\n"

    @classmethod
    def from_api_dict(cls, raw: Mapping[str, Any]) -> IntentDraft:
        """Decode a single intent payload as returned by the API.

        The ``slots`` tuple is left empty: the rendered ``slots:`` block
        is part of ``sourceText`` server-side, and the library treats
        the wire form as authoritative for round-trip diffs (see
        :meth:`DialogsSkillCreator.set_intents`).
        """
        return cls(
            form_name=str(raw.get("formName") or ""),
            human_readable_name=str(raw.get("humanReadableName") or ""),
            source_text=str(raw.get("sourceText") or ""),
            positive_tests=str(raw.get("positiveTests") or ""),
            negative_tests=str(raw.get("negativeTests") or ""),
            is_activation=bool(raw.get("isActivation", False)),
            intent_id=(str(raw["id"]) if isinstance(raw.get("id"), str) and raw["id"] else None),
            status=str(raw.get("status") or "NEW"),
        )

    def to_api_dict(self) -> dict[str, Any]:
        """Encode for use in PATCH payloads.

        ``sourceText`` carries :attr:`rendered_source_text` so any
        structured :class:`SlotDeclaration` entries are materialised
        before the payload leaves the process. ``id`` is included when
        known (PATCH path requires it); ``status`` is sent as the
        client-known last value but Yandex re-computes it.
        """
        payload: dict[str, Any] = {
            "humanReadableName": self.human_readable_name,
            "formName": self.form_name,
            "sourceText": self.rendered_source_text,
            "positiveTests": self.positive_tests,
            "negativeTests": self.negative_tests,
            "isActivation": self.is_activation,
            "status": self.status,
        }
        if self.intent_id is not None:
            payload["id"] = self.intent_id
        return payload


# ---------------------------------------------------------------------------
# Custom entities (aliceSkill channel only)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class EntityValue:
    """One named value within an :class:`EntityDraft`.

    ``name`` is the key surfaced to a webhook handler in
    ``request.nlu.entities[i].value`` when an utterance matches; the
    ``phrases`` tuple lists the alternative spoken forms that should
    map to this value.

    :param name: Lowercase identifier (no spaces).
    :param phrases: Alternative spoken phrases that map to ``name``.
        Rendered in DSL as ``phrase | phrase | …``.
    """

    name: str
    phrases: tuple[str, ...]

    def to_dsl(self) -> str:
        """Render this value as two indented DSL lines.

        Format::

            ``        <name>:``
            ``            <phrase> | <phrase> | …``

        The 8/12-space indentation matches the convention emitted by
        Yandex's dev-console editor and accepted by the Granet parser.
        """
        body = " | ".join(self.phrases)
        return f"        {self.name}:\n            {body}"


@dataclasses.dataclass(frozen=True, slots=True)
class EntityDraft:
    """Single Yandex Dialogs custom entity definition.

    Custom entities are reusable enums of phrases that intent grammars
    can reference as a slot type or as a non-terminal. The dialog
    channel (``aliceSkill``) stores them as one Granet ``sourceText``
    blob per skill; the library's :meth:`DialogsSkillCreator.set_entities`
    composes a list of :class:`EntityDraft` into that single blob and
    PUTs it via the entities endpoint.

    :param name: Entity identifier — also the slot ``type`` name when
        referenced from an :class:`IntentDraft`'s
        :class:`SlotDeclaration`.
    :param values: Tuple of :class:`EntityValue`, one per enum entry.
    """

    name: str
    values: tuple[EntityValue, ...]

    def to_dsl(self) -> str:
        """Render this entity as a Granet ``entity`` block.

        Format::

            entity <name>:
                values:
                    <value-block>
                    <value-block>

        Composes :meth:`EntityValue.to_dsl` for each value.
        """
        lines = [f"entity {self.name}:", "    values:", *(v.to_dsl() for v in self.values)]
        return "\n".join(lines)


def _render_entities_source(entities: tuple[EntityDraft, ...]) -> str:
    """Render multiple entity drafts as one Granet entities ``sourceText``.

    Empty input maps to an empty string — equivalent to "no custom
    entities". The endpoint accepts that as a clear-all and returns
    HTTP 200.
    """
    if not entities:
        return ""
    return "\n".join(entity.to_dsl() for entity in entities) + "\n"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DialogsSkillCreator:
    """Thin async wrapper over dialogs.yandex.ru developer-console API.

    Every method is idempotent on the transport layer: a single call
    either succeeds or raises. Retry / state-machine logic lives in the
    orchestrator (see :func:`auto_create_skill`).
    """

    __slots__ = ("_channel", "_logger", "_session")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        logger: logging.Logger | None = None,
        *,
        channel: Channel = SMART_HOME_CHANNEL,
    ) -> None:
        """Take a session that already carries Passport auth cookies.

        ``channel`` selects the Yandex Dialogs skill family — defaults to
        :data:`SMART_HOME_CHANNEL` (``"smartHome"``); pass
        :data:`DIALOG_CHANNEL` (``"aliceSkill"``) for custom Alice skills.
        """
        self._session = session
        self._logger = logger or _LOGGER
        self._channel = channel

    # -----------------------------------------------------------------------
    # Step 1: CSRF token extraction
    # -----------------------------------------------------------------------

    async def fetch_csrf(self) -> str:
        """Fetch the developer page HTML and extract the CSRF ``secretkey``.

        Caller uses the returned value as the ``x-csrf-token`` header on
        all mutating requests. Returns a fresh token on every call; the
        orchestrator caches it for the duration of a single attempt.

        Disables redirect-following: an anonymous request hits a 30x to
        ``passport.yandex.ru`` and following the redirect would dump
        Passport's HTML at us, where the CSRF regex never matches. We
        treat the redirect as an explicit auth signal.
        """
        async with self._session.get(DIALOGS_DEV_HTML_URL, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                raise DialogsAuthError(
                    "redirected to Passport — passport session cookies missing or expired",
                    step="fetch_csrf",
                    http_status=resp.status,
                )
            if resp.status == 401:
                raise DialogsAuthError(
                    "not authenticated — passport session cookies missing or expired",
                    step="fetch_csrf",
                    http_status=401,
                )
            if resp.status != 200:
                raise DialogsApiError(
                    f"dialogs.yandex.ru/developer returned HTTP {resp.status}",
                    step="fetch_csrf",
                    http_status=resp.status,
                )
            # Enforce the size cap while reading so an oversized
            # response can't buffer fully in memory.
            body = bytearray()
            async for chunk in resp.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _MAX_HTML_RESPONSE_BYTES:
                    raise DialogsApiError(
                        "developer page response exceeded size cap",
                        step="fetch_csrf",
                    )
            html = body.decode(resp.get_encoding() or "utf-8", errors="replace")

        match = DIALOGS_CSRF_REGEX.search(html)
        if not match:
            raise DialogsCsrfError(
                "could not locate CSRF token in developer page HTML — "
                "Yandex may have changed the rendering format",
                step="fetch_csrf",
            )
        token = match.group(1).strip()
        if not token:
            raise DialogsCsrfError(
                "CSRF token matched but is empty",
                step="fetch_csrf",
            )
        self._logger.debug("dialogs CSRF token fetched (len=%d)", len(token))
        return token

    # -----------------------------------------------------------------------
    # Step 2: list existing skills (for duplicate-name detection)
    # -----------------------------------------------------------------------

    async def list_existing_skills(self, csrf: str) -> list[dict[str, Any]]:
        """Return the user's existing skills from the snapshot endpoint.

        The dashboard uses this to populate its skill list; we use it to
        warn the user before they hit a duplicate-name 4xx on create_app.
        """
        url = f"{DIALOGS_API_BASE}/snapshot"
        data = await self._get_json(url, csrf=csrf, step="list_existing_skills")
        result = data.get("result")
        if not isinstance(result, dict):
            return []
        skills = result.get("skills")
        if not isinstance(skills, list):
            return []
        return [s for s in skills if isinstance(s, dict)]

    # -----------------------------------------------------------------------
    # Step 3: create the skill app
    # -----------------------------------------------------------------------

    async def create_app(self, csrf: str, name: str) -> str:
        """Create a skill on the configured channel with the given name.

        Returns the newly-minted ``skill_id`` (UUID). Raises
        :class:`DialogsDuplicateSkillError` if the name is already taken
        by another skill on this account.
        """
        url = f"{DIALOGS_API_BASE}/apps"
        payload = {
            "channel": self._channel,
            "language": "ru",
            "isYangoConsole": False,
            "appName": name,
        }
        data = await self._post_json(url, payload, csrf=csrf, step="create_app")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError(
                "create_app response missing 'result' object",
                step="create_app",
            )
        skill_id = result.get("id") or result.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise DialogsApiError(
                "create_app response missing skill id",
                step="create_app",
            )
        self._logger.info("dialogs skill created: id=%s name=%r", skill_id, name)
        return skill_id

    # -----------------------------------------------------------------------
    # Step 4: upload logo
    # -----------------------------------------------------------------------

    async def upload_logo(self, csrf: str, skill_id: str, png: bytes) -> str:
        """Upload a PNG logo for the skill.

        Returns a ``logo_id`` that must be referenced in ``update_draft``.
        The logo file is sent as multipart with the field name ``file``
        and filename ``icon.png`` (matching the HAR capture).
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/upload-logo?channel={self._channel}"
        form = aiohttp.FormData()
        form.add_field("file", png, filename="icon.png", content_type="image/png")
        headers = {"x-csrf-token": csrf}
        async with self._session.post(url, data=form, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise parse_error_body(body, http_status=resp.status, step="upload_logo")
            data = _try_json_or_raise(body, step="upload_logo", method="POST", url=url)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise DialogsApiError("upload_logo response missing 'result'", step="upload_logo")
        logo_id = result.get("id")
        if not isinstance(logo_id, str) or not logo_id:
            raise DialogsApiError("upload_logo response missing logo id", step="upload_logo")
        return logo_id

    # -----------------------------------------------------------------------
    # Step 5: update draft settings
    # -----------------------------------------------------------------------

    async def update_draft(self, csrf: str, skill_id: str, payload: Mapping[str, Any]) -> None:
        """PATCH the skill draft with backend URL / publishing metadata."""
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/update"
        await self._patch_json(url, dict(payload), csrf=csrf, step="update_draft")

    # -----------------------------------------------------------------------
    # Step 6: create OAuth app (account-linking)
    # -----------------------------------------------------------------------

    async def create_oauth_app(
        self,
        csrf: str,
        *,
        name: str,
        client_id: str,
        client_secret: str,
        authorize_url: str,
        token_url: str,
        refresh_url: str,
    ) -> str:
        """Create the OAuth app that powers account-linking in the skill.

        Returns the OAuth-app UUID which is then bound to the skill via
        ``attach_oauth``.
        """
        url = f"{DIALOGS_API_BASE}/oauth/apps"
        payload = {
            "name": name,
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUrl": authorize_url,
            "tokenUrl": token_url,
            "refreshTokenUrl": refresh_url,
            "scope": "",
            "yandexClientId": "",
        }
        data = await self._post_json(url, payload, csrf=csrf, step="create_oauth_app")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError(
                "create_oauth_app response missing 'result'", step="create_oauth_app"
            )
        oauth_app_id = result.get("id")
        if not isinstance(oauth_app_id, str) or not oauth_app_id:
            raise DialogsApiError(
                "create_oauth_app response missing oauth app id", step="create_oauth_app"
            )
        return oauth_app_id

    # -----------------------------------------------------------------------
    # Step 7: bind OAuth app to the skill
    # -----------------------------------------------------------------------

    async def attach_oauth(self, csrf: str, skill_id: str, oauth_app_id: str) -> None:
        """Attach an existing OAuth app to the skill's account-linking slot."""
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/oauthApp?channel={self._channel}"
        payload = {"oauthAppId": oauth_app_id}
        await self._post_json(url, payload, csrf=csrf, step="attach_oauth")

    # -----------------------------------------------------------------------
    # Step 8: publish (send for moderation)
    # -----------------------------------------------------------------------

    async def request_deploy(self, csrf: str, skill_id: str) -> None:
        """Send the draft to moderation / publish.

        Body is empty; all params are in the query string. Returns on
        2xx; otherwise raises.
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/draft/request-deploy?channel={self._channel}"
        headers = {"x-csrf-token": csrf}
        async with self._session.post(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status not in (200, 201, 202, 204):
                raise parse_error_body(body, http_status=resp.status, step="request_deploy")

    # -----------------------------------------------------------------------
    # Step 9: delete the skill (cleanup / CI use)
    # -----------------------------------------------------------------------

    async def delete_skill(self, csrf: str, skill_id: str) -> None:
        """Delete a skill and its associated draft, logos, and operations.

        The OAuth-app attached to the skill (if any) is **not** removed by
        this call — orphaned OAuth apps remain in ``/oauth/apps``.

        Verified empirically 2026-05-06: returns ``HTTP 200`` with body
        ``{}`` on success. The skill (and its in-flight moderation, if
        any) is removed immediately and disappears from ``/snapshot``.
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}?channel={self._channel}"
        headers = {"x-csrf-token": csrf}
        async with self._session.delete(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status not in (200, 204):
                raise parse_error_body(body, http_status=resp.status, step="delete_skill")

    # -----------------------------------------------------------------------
    # Custom-intent management (aliceSkill channel only)
    # -----------------------------------------------------------------------

    async def list_intents(self, csrf: str, skill_id: str) -> list[IntentDraft]:
        """List all custom-intent drafts for the skill, with full per-intent content.

        Channel is hard-coded to ``aliceSkill`` regardless of
        ``self._channel`` because intents only exist on the dialog
        channel — calling this on a smart-home skill returns an empty
        list at the API level.

        The bulk listing endpoint returns only ``id``, ``humanReadableName``,
        ``status`` and ``isActivation`` — ``formName`` and ``sourceText`` are
        omitted. Since :meth:`set_intents` matches existing entries by
        ``form_name``, this method fans out to :meth:`get_intent` for each
        listed id (in parallel) so the returned drafts carry the full payload
        the diff/upsert protocol needs.
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/intents/drafts?channel={DIALOG_CHANNEL}"
        data = await self._get_json(url, csrf=csrf, step="list_intents")
        result = data.get("result")
        # Yandex returns either a top-level list or a wrapped {result: [...]}.
        # Probed shape was the wrapped form; the unwrapped fallback is
        # defensive against future protocol revisions.
        items: Any = result if isinstance(result, list) else data
        if not isinstance(items, list):
            raise DialogsApiError("list_intents response missing list payload", step="list_intents")
        ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if isinstance(raw_id, str) and raw_id:
                ids.append(raw_id)
        if not ids:
            return []
        return list(
            await asyncio.gather(*(self.get_intent(csrf, skill_id, intent_id) for intent_id in ids))
        )

    async def get_intent(self, csrf: str, skill_id: str, intent_id: str) -> IntentDraft:
        """Fetch a single intent draft by id."""
        url = (
            f"{DIALOGS_API_BASE}/apps/{skill_id}/intents/drafts/{intent_id}"
            f"?channel={DIALOG_CHANNEL}"
        )
        data = await self._get_json(url, csrf=csrf, step="get_intent")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError("get_intent response missing 'result'", step="get_intent")
        return IntentDraft.from_api_dict(result)

    async def create_intent(self, csrf: str, skill_id: str) -> IntentDraft:
        """Create a fresh empty intent shell and return its server-assigned id.

        Yandex's API uses a two-phase create: POST creates an empty intent
        (id and ``status="NEW"`` populated by server), then PATCH carries
        the actual content. :meth:`update_intent` covers the PATCH half;
        :meth:`set_intents` chains both for declarative use.
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/intents/draft?channel={DIALOG_CHANNEL}"
        data = await self._post_json(url, {}, csrf=csrf, step="create_intent")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError("create_intent response missing 'result'", step="create_intent")
        intent = IntentDraft.from_api_dict(result)
        if intent.intent_id is None:
            raise DialogsApiError("create_intent response missing intent id", step="create_intent")
        return intent

    async def update_intent(self, csrf: str, skill_id: str, intent: IntentDraft) -> IntentDraft:
        """PATCH an existing intent's content. Server validates grammar synchronously.

        On valid grammar: returns the saved :class:`IntentDraft` with
        ``status="NEW"``.

        On invalid grammar: Yandex still returns HTTP 200 and saves the
        intent (subsequent reads show it with the bad source), but the
        response carries a ``validationError`` block. We surface that as
        :class:`DialogsIntentValidationError` so callers can handle the
        error programmatically. The previously-valid version of the
        intent (if any) remains effective at runtime — Yandex preserves
        the last-good source until a valid PATCH replaces it.
        """
        if intent.intent_id is None:
            raise DialogsApiError(
                "update_intent requires intent.intent_id (call create_intent first)",
                step="update_intent",
            )
        url = (
            f"{DIALOGS_API_BASE}/apps/{skill_id}/intents/{intent.intent_id}/draft"
            f"?channel={DIALOG_CHANNEL}"
        )
        data = await self._patch_json(url, intent.to_api_dict(), csrf=csrf, step="update_intent")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DialogsApiError("update_intent response missing 'result'", step="update_intent")
        intent_raw = result.get("intent")
        if not isinstance(intent_raw, dict):
            raise DialogsApiError(
                "update_intent response missing 'result.intent'", step="update_intent"
            )
        validation_error = result.get("validationError")
        if isinstance(validation_error, dict):
            bounds = validation_error.get("errorBounds")
            char_count = char_offset = line_number = -1
            if isinstance(bounds, dict):
                char_count = int(bounds.get("charCount", -1) or -1)
                char_offset = int(bounds.get("charOffset", -1) or -1)
                line_number = int(bounds.get("lineNumber", -1) or -1)
            # DEBUG-level raw dump so a postmortem with --log-level=debug
            # in the host MA can see the exact payload Yandex rejected.
            # Helpful for messages like "Некорректный аргумент" that don't
            # by themselves identify the offending field.
            _LOGGER.debug(
                "update_intent validation failure: form_name=%r id=%s "
                "validationError=%r request_payload=%r",
                intent.form_name,
                intent.intent_id,
                validation_error,
                intent.to_api_dict(),
            )
            raise DialogsIntentValidationError(
                str(validation_error.get("text") or "Grammar validation failed"),
                step="update_intent",
                error_code=str(validation_error.get("errorCode") or "VALIDATION_ERROR"),
                char_count=char_count,
                char_offset=char_offset,
                line_number=line_number,
                intent_id=intent.intent_id,
                form_name=intent.form_name,
            )
        return IntentDraft.from_api_dict(intent_raw)

    async def delete_intent(self, csrf: str, skill_id: str, intent_id: str) -> None:
        """Delete a custom intent.

        DELETE on this endpoint omits the ``?channel`` query parameter —
        the only intent endpoint to do so (verified via Playwright probe
        2026-05-07).
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/intents/{intent_id}/draft"
        headers = {"x-csrf-token": csrf}
        async with self._session.delete(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status not in (200, 204):
                raise parse_error_body(body, http_status=resp.status, step="delete_intent")

    async def set_intents(
        self,
        csrf: str,
        skill_id: str,
        intents: list[IntentDraft],
        *,
        delete_missing: bool = True,
    ) -> list[IntentDraft]:
        """Idempotent declarative setter for a skill's custom intents.

        Diffs ``intents`` against the live state (matched by ``form_name``)
        and issues the minimum PATCH/POST/DELETE sequence. Returns the
        post-sync list of intents (intent_id populated, status from server).

        Behaviour:

        * Existing intents (matched by ``form_name``) are PATCHed when their
          definition differs from the local one; otherwise skipped.
        * New intents (no matching ``form_name`` on the server) are
          created via POST + immediately PATCHed.
        * Server intents whose ``form_name`` is missing from the input
          list are deleted when ``delete_missing=True`` (the default —
          matches the declarative-config use case where the input is
          authoritative). Pass ``delete_missing=False`` to leave them
          alone (additive merge).

        Raises :class:`DialogsIntentValidationError` on the FIRST invalid
        grammar encountered — partial progress (already-PATCHed intents)
        remains on the server. Caller can re-invoke after fixing the
        offending grammar; the operation is fully idempotent.
        """
        existing = await self.list_intents(csrf, skill_id)
        existing_by_form = {ent.form_name: ent for ent in existing if ent.form_name}

        out: list[IntentDraft] = []
        seen_form_names: set[str] = set()
        for index, desired in enumerate(intents):
            seen_form_names.add(desired.form_name)
            current = existing_by_form.get(desired.form_name)
            if current is None:
                _LOGGER.info(
                    "set_intents [%d/%d]: creating new intent form_name=%r",
                    index + 1,
                    len(intents),
                    desired.form_name,
                )
                created = await self.create_intent(csrf, skill_id)
                merged = dataclasses.replace(desired, intent_id=created.intent_id)
                out.append(await self.update_intent(csrf, skill_id, merged))
                continue
            # Existing — PATCH only if anything user-controllable differs.
            # Compare against the rendered DSL: a desired intent built
            # from structured ``slots`` carries the slot block in
            # ``rendered_source_text``, while the server-side
            # ``current.source_text`` already carries the rendered DSL
            # too (``from_api_dict`` doesn't try to parse the block back
            # into structured slots). Matching on the raw ``source_text``
            # would treat every slot-bearing intent as drifted and force
            # a useless PATCH on every sync.
            merged = dataclasses.replace(desired, intent_id=current.intent_id)
            if (
                merged.human_readable_name == current.human_readable_name
                and merged.rendered_source_text == current.source_text
                and merged.positive_tests == current.positive_tests
                and merged.negative_tests == current.negative_tests
                and merged.is_activation == current.is_activation
            ):
                _LOGGER.debug(
                    "set_intents [%d/%d]: skipping unchanged form_name=%r id=%s",
                    index + 1,
                    len(intents),
                    desired.form_name,
                    current.intent_id,
                )
                out.append(current)
                continue
            _LOGGER.info(
                "set_intents [%d/%d]: patching existing form_name=%r id=%s",
                index + 1,
                len(intents),
                desired.form_name,
                current.intent_id,
            )
            out.append(await self.update_intent(csrf, skill_id, merged))

        if delete_missing:
            for stale in existing:
                if stale.intent_id and stale.form_name not in seen_form_names:
                    await self.delete_intent(csrf, skill_id, stale.intent_id)

        return out

    # -----------------------------------------------------------------------
    # Custom-entities management (aliceSkill channel only)
    # -----------------------------------------------------------------------

    async def set_entities_source(
        self,
        csrf: str,
        skill_id: str,
        source_text: str,
    ) -> None:
        """Replace the skill's custom-entities source text in one PUT.

        Single-shot replace: the endpoint stores the entire Granet
        DSL text for all entities together (Yandex doesn't expose
        per-entity CRUD). An empty ``source_text`` clears all custom
        entities. The PUT is naturally idempotent server-side.

        Raises :class:`DialogsEntitiesValidationError` on Granet
        validation failures (HTTP 400 with a ``"Granet grammar
        validation error."`` body), and other typed
        :class:`DialogsApiError` subclasses on transport failures via
        :func:`parse_error_body`.
        """
        url = f"{DIALOGS_API_BASE}/apps/{skill_id}/drafts/entities?channel={DIALOG_CHANNEL}"
        async with self._session.request(
            "PUT",
            url,
            json={"sourceText": source_text},
            headers={
                "x-csrf-token": csrf,
                "content-type": "application/json",
                "accept": "application/json",
            },
        ) as resp:
            body = await resp.text()
            if resp.status == 200:
                return
            # Granet validation comes back as a Spring servlet 400. We can't
            # rely on parse_error_body alone to discriminate it from a
            # generic 400 because both ride the same envelope; sniff for the
            # "Granet" marker in the message.
            if resp.status == 400 and "Granet" in body:
                _LOGGER.debug(
                    "set_entities validation failure: skill=%s body=%r request=%r",
                    skill_id,
                    body,
                    source_text,
                )
                raise DialogsEntitiesValidationError(
                    "Granet grammar validation error in custom entities",
                    step="set_entities",
                    http_status=400,
                    yandex_error=body,
                )
            raise parse_error_body(body, http_status=resp.status, step="set_entities")

    async def set_entities(
        self,
        csrf: str,
        skill_id: str,
        entities: list[EntityDraft],
    ) -> None:
        """Idempotent declarative setter for the skill's custom entities.

        Renders ``entities`` to a single Granet ``sourceText`` blob via
        :func:`_render_entities_source` and replaces the server-side
        state with one PUT. An empty ``entities`` list clears all
        custom entities (the endpoint accepts an empty ``sourceText``).

        Idempotency is server-side: re-running with unchanged input
        yields HTTP 200 without observable effect. Unlike
        :meth:`set_intents`, no client-side diff is performed — the
        custom-entities endpoint stores raw text and a single PUT is
        cheap.
        """
        rendered = _render_entities_source(tuple(entities))
        await self.set_entities_source(csrf, skill_id, rendered)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _get_json(self, url: str, *, csrf: str, step: str) -> dict[str, Any]:
        headers = {"x-csrf-token": csrf}
        async with self._session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise parse_error_body(body, http_status=resp.status, step=step)
            data = _try_json_or_raise(body, step=step, method="GET", url=url)
        if not isinstance(data, dict):
            raise DialogsApiError(f"GET {url} returned non-object JSON", step=step)
        return data

    async def _post_json(
        self, url: str, payload: dict[str, Any], *, csrf: str, step: str
    ) -> dict[str, Any]:
        return await self._send_json("POST", url, payload, csrf=csrf, step=step)

    async def _patch_json(
        self, url: str, payload: dict[str, Any], *, csrf: str, step: str
    ) -> dict[str, Any]:
        return await self._send_json("PATCH", url, payload, csrf=csrf, step=step)

    async def _send_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        csrf: str,
        step: str,
    ) -> dict[str, Any]:
        headers = {"x-csrf-token": csrf, "content-type": "application/json"}
        async with self._session.request(method, url, json=payload, headers=headers) as resp:
            body = await resp.text()
            # Only ``create_app`` can fail with duplicate-name errors —
            # other endpoints use 409 for unrelated conflicts and would
            # be misclassified as duplicates if the mapping were global.
            duplicate_candidate = step == "create_app" and (
                resp.status == 409 or (resp.status in (400, 422) and _looks_like_duplicate(body))
            )
            if duplicate_candidate:
                raise DialogsDuplicateSkillError(
                    f"{step}: skill with this name already exists",
                    step=step,
                    http_status=resp.status,
                    yandex_error=_extract_first_error_string(body),
                )
            if resp.status not in (200, 201, 202):
                # Empty / very short body 4xx — log a small safe subset of
                # response headers so the user can see what Yandex actually
                # returned (helps diagnose e.g. wrong "channel" parameter
                # where the API rejects the request before generating a body).
                if not body.strip():
                    safe_headers = {
                        k: resp.headers.get(k)
                        for k in (
                            "Content-Type",
                            "Content-Length",
                            "X-Request-Id",
                            "X-RateLimit-Remaining",
                            "X-RateLimit-Limit",
                        )
                        if resp.headers.get(k) is not None
                    }
                    # NOTE: do NOT log fields from `payload` directly — it can
                    # contain OAuth client_secret. Channel is duplicated on the
                    # client object, log it from there instead.
                    _LOGGER.warning(
                        "Yandex %s %s returned %s with empty body; response "
                        "headers=%s, client channel=%r",
                        method,
                        url,
                        resp.status,
                        safe_headers,
                        self._channel,
                    )
                raise parse_error_body(body, http_status=resp.status, step=step)
            data = _try_json_or_raise(body, step=step, method=method, url=url)
        if not isinstance(data, dict):
            raise DialogsApiError(f"{method} {url} returned non-object JSON", step=step)
        return data


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _try_json_or_raise(body: str, *, step: str, method: str, url: str) -> Any:
    """Parse JSON body, return ``None`` for empty body, raise on malformed."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError) as exc:
        msg = f"{method} {url} returned malformed JSON: {exc}"
        raise DialogsApiError(msg, step=step) from exc


def _looks_like_duplicate(body: str) -> bool:
    """Heuristic for whether a 4xx body indicates a duplicate-name error."""
    if not body:
        return False
    lowered = body.lower()
    return any(
        kw in lowered for kw in ("already exists", "duplicate", "exists with name", "not_unique")
    )


def _extract_first_error_string(body: str) -> str | None:
    """Pull a top-level error string out of a JSON body (best-effort)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("error", "errorCode", "message", "code"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Pure helpers: payload builders
# ---------------------------------------------------------------------------


def build_smart_home_draft_payload(
    *,
    skill_name: str,
    backend_uri: str,
    logo_id: str | None,
    developer_name: str = "Skill creator",
) -> dict[str, Any]:
    """Compose the PATCH /draft/update body for a Smart Home skill.

    Matches the HAR sample field-for-field; every key that the
    dashboard UI sends on save is reproduced so Yandex's validator
    sees a complete draft and allows ``request-deploy`` afterwards.
    """
    return {
        "logo2": None,
        "name": skill_name,
        "voice": "shitova.us",
        "logoId": logo_id,
        "skillAccess": "private",
        "hideInStore": False,
        "noteForModerator": "",
        "backendSettings": {
            "uri": backend_uri,
            "functionId": "",
            "backendType": "webhook",
        },
        "publishingSettings": {
            "brandVerificationWebsite": "",
            "category": "smart_home",
            "developerName": developer_name,
            "secondaryTitle": "",
            "email": "",
            "smartHome": {
                "deepLinks": {
                    "android": {"url": ""},
                    "ios": {"url": "", "fallbackUrl": ""},
                },
            },
            "multilingualSettings": {
                "ru": {
                    "name": skill_name,
                    "secondaryTitle": "",
                    "externalSettingsDescription": skill_name,
                    "supportedUnitsDescription": skill_name,
                },
            },
        },
        "oauthAppId": None,
        "isTrustedSmartHomeSkill": False,
        "enableAllAvailableRegions": True,
        "selectedRegions": [],
        "channel": SMART_HOME_CHANNEL,
    }


def build_dialog_draft_payload(
    *,
    skill_name: str,
    backend_uri: str,
    logo_id: str | None,
    description: str,
    structured_examples: list[dict[str, Any]] | None = None,
    activation_phrases: list[str] | None = None,
    category: str = "music_audio",
    voice: str = "good_oksana",
    developer_name: str = "Skill creator",
) -> dict[str, Any]:
    """Compose the PATCH /draft/update body for a custom Alice (``aliceSkill``) skill.

    All fields and shapes were captured from a live PATCH issued by the
    dev console after the user filled the form successfully.

    Yandex validates ``name``: it must contain at least two words, otherwise
    PATCH returns 400 ``{"error":{"fields":{"name":"Название должно содержать
    минимум два слова"}}}``.
    """
    if structured_examples is None:
        structured_examples = [
            {
                "marker": "попроси",
                "activationPhrase": skill_name,
                "request": "включи что-нибудь",
                "is_valid": True,
            },
        ]
    if activation_phrases is None:
        activation_phrases = [skill_name]
    return {
        "logo2": None,
        "name": skill_name,
        "voice": voice,
        "activationPhrases": activation_phrases,
        "logoId": logo_id,
        "noteForModerator": "",
        "yaCloudGrant": False,
        "backendSettings": {
            "uri": backend_uri,
            "functionId": "",
            "backendType": "webhook",
        },
        "publishingSettings": {
            "brandVerificationWebsite": "",
            "category": category,
            "developerName": developer_name,
            "explicitContent": False,
            "structuredExamples": structured_examples,
            "description": description,
            "email": "",
        },
        "requiredInterfaces": [],
        "exactSurfaces": [],
        "surfaceWhitelist": [],
        "surfaceBlacklist": [],
        "oauthAppId": None,
        "appMetricaApiKey": "",
        "useStateStorage": False,
        "rsyPlatformId": "",
        "skillAccess": "private",
        "hideInStore": True,
        "channel": DIALOG_CHANNEL,
    }


def build_oauth_app_payload(
    *,
    skill_name: str,
    client_id: str,
    client_secret: str,
    authorize_url: str,
    token_url: str,
) -> dict[str, Any]:
    """Compose the POST /oauth/apps body for account-linking.

    The caller is responsible for computing ``client_id`` / ``client_secret`` /
    ``authorize_url`` / ``token_url`` for their integration. ``refreshTokenUrl``
    always equals ``token_url`` — Yandex's flow uses the same endpoint
    for both grant types.
    """
    return {
        "name": skill_name,
        "clientId": client_id,
        "clientSecret": client_secret,
        "authorizationUrl": authorize_url,
        "tokenUrl": token_url,
        "refreshTokenUrl": token_url,
        "scope": "",
        "yandexClientId": "",
    }


# ---------------------------------------------------------------------------
# Orchestrator: state-machine pipeline
# ---------------------------------------------------------------------------

# Hard cap on how long the caller's authenticator may take.
DEVICE_FLOW_TIMEOUT_SECONDS = 300.0


# A pipeline executor takes the fetched CSRF token and a checkpoint callback,
# and advances the pipeline as far as it can. Used by ``_run_with_recovery``
# so the recovery harness is independent of the specific channel/OAuth shape.
_PipelineExecutor = Callable[
    [str, Callable[[SkillCreationArtifacts], Awaitable[None]]],
    Awaitable[SkillCreationArtifacts],
]


def _validate_oauth_args(
    *,
    channel: Channel,
    oauth_authorize_url: str | None,
    oauth_token_url: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
) -> bool:
    """Return ``True`` if OAuth pipeline should run; raise on inconsistent inputs.

    Rules:

    - All-four or none. Mixed is a programmer error → ``ValueError``.
    - ``smartHome`` requires all four (Smart Home skills always need
      account-linking) → ``ValueError`` if missing.
    - ``aliceSkill`` accepts both with or without OAuth; the four-tuple
      simply selects the pipeline shape.
    """
    parts = (oauth_authorize_url, oauth_token_url, oauth_client_id, oauth_client_secret)
    has_all = all(p is not None for p in parts)
    has_any = any(p is not None for p in parts)
    if has_any and not has_all:
        msg = (
            "OAuth params are partial — pass all four "
            "(oauth_authorize_url, oauth_token_url, oauth_client_id, oauth_client_secret) or none"
        )
        raise ValueError(msg)
    if channel == SMART_HOME_CHANNEL and not has_all:
        msg = (
            "channel='smartHome' requires OAuth params "
            "(oauth_authorize_url, oauth_token_url, oauth_client_id, oauth_client_secret)"
        )
        raise ValueError(msg)
    return has_all


async def auto_create_skill(
    *,
    authenticator: AuthenticatorCM,
    skill_name: str,
    artifacts: SkillCreationArtifacts,
    backend_uri: str,
    channel: Channel = SMART_HOME_CHANNEL,
    oauth_authorize_url: str | None = None,
    oauth_token_url: str | None = None,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    logo_bytes: bytes | None = None,
    description: str | None = None,
    structured_examples: list[dict[str, Any]] | None = None,
    activation_phrases: list[str] | None = None,
    category: str | None = None,
    voice: str | None = None,
    intents: list[IntentDraft] | None = None,
    entities: list[EntityDraft] | None = None,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts:
    """Run the full skill-creation pipeline against an authenticated session.

    Channel selection (Yandex API wire values):

    - ``channel="smartHome"`` (default) — Smart Home skill. Requires all four
      ``oauth_*`` params (account-linking is mandatory for Smart Home).
    - ``channel="aliceSkill"`` — custom Alice dialog skill. ``description``
      (non-empty after ``.strip()``) is required. OAuth is optional: provide
      all four ``oauth_*`` to attach an OAuth app, or omit them all to skip
      account-linking entirely. Verified empirically 2026-05-06 that Yandex
      accepts ``request_deploy`` for ``aliceSkill`` skills with no OAuth.

    Other parameters:

    - ``logo_bytes`` defaults to :func:`load_default_logo_bytes` when ``None``.
    - For ``aliceSkill``: ``category`` defaults to ``"music_audio"``, ``voice``
      to ``"good_oksana"``, ``activation_phrases`` to ``[skill_name]``, and
      ``structured_examples`` to a single placeholder.
    - ``progress_cb`` is invoked after each successful step with the updated
      artifacts; persist them and on the next call pass the saved artifacts
      back in so the pipeline resumes from the latest completed step.

    Authentication is the caller's responsibility: ``authenticator`` is a
    no-arg async-context-manager factory that yields an
    ``aiohttp.ClientSession`` already carrying Yandex Passport cookies.

    Resumes from ``artifacts.state`` — steps that already completed are
    skipped. On any pipeline error, returns artifacts with ``state=FAILED``
    and a human-readable ``last_error`` instead of re-raising, so a config-flow
    UI can render the message without crashing.
    """
    # Channel-specific input validation
    has_oauth = _validate_oauth_args(
        channel=channel,
        oauth_authorize_url=oauth_authorize_url,
        oauth_token_url=oauth_token_url,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
    )

    if channel == DIALOG_CHANNEL and (description is None or not description.strip()):
        msg = "description (non-empty) is required for channel='aliceSkill'"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)

    if logo_bytes is None:
        logo_bytes = load_default_logo_bytes()

    try:
        async with authenticator() as session:
            creator = (
                creator_factory(session)
                if creator_factory is not None
                else DialogsSkillCreator(session, channel=channel)
            )

            async def executor(
                csrf: str,
                track: Callable[[SkillCreationArtifacts], Awaitable[None]],
            ) -> SkillCreationArtifacts:
                return await _execute_pipeline(
                    creator=creator,
                    csrf=csrf,
                    artifacts=artifacts,
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    channel=channel,
                    has_oauth=has_oauth,
                    oauth_authorize_url=oauth_authorize_url,
                    oauth_token_url=oauth_token_url,
                    oauth_client_id=oauth_client_id,
                    oauth_client_secret=oauth_client_secret,
                    logo_bytes=logo_bytes,
                    description=description,
                    structured_examples=structured_examples,
                    activation_phrases=activation_phrases,
                    category=category,
                    voice=voice,
                    intents=intents,
                    entities=entities,
                    developer_name=developer_name,
                    progress_cb=track,
                )

            return await _run_with_recovery(
                creator=creator,
                artifacts=artifacts,
                executor=executor,
                progress_cb=progress_cb,
            )
    except asyncio.CancelledError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        _LOGGER.exception("auto-create hit unexpected error")
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=repr(exc))


async def _run_with_recovery(
    *,
    creator: DialogsSkillCreator,
    artifacts: SkillCreationArtifacts,
    executor: _PipelineExecutor,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Fetch CSRF and run the executor, preserving partial state on failure.

    Holds a ``current`` reference that the executor updates via the tracker
    callback, so a mid-pipeline raise lets us surface whatever progress was
    captured (skill_id / logo_id / oauth_app_id) as a FAILED artifact
    instead of losing it.
    """
    current = artifacts

    async def _track(a: SkillCreationArtifacts) -> None:
        nonlocal current
        current = a
        if progress_cb is not None:
            await progress_cb(a)

    try:
        _LOGGER.info("auto-skill: fetching CSRF from dialogs.yandex.ru")
        csrf = await creator.fetch_csrf()
        _LOGGER.info("auto-skill: CSRF acquired, starting skill pipeline")
        return await executor(csrf, _track)
    except DialogsApiError as exc:
        _LOGGER.warning("auto-create failed at %s: %s", exc.step, exc, exc_info=True)
        return dataclasses.replace(current, state=SkillCreationState.FAILED, last_error=str(exc))


async def _execute_pipeline(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    backend_uri: str,
    channel: Channel,
    has_oauth: bool,
    oauth_authorize_url: str | None,
    oauth_token_url: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    logo_bytes: bytes,
    description: str | None,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    category: str | None,
    voice: str | None,
    intents: list[IntentDraft] | None,
    entities: list[EntityDraft] | None,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Compose the channel-appropriate sequence of pipeline steps.

    OAuth steps run only when ``has_oauth=True``. The deploy-checkpoint step
    accepts both ``DRAFT_UPDATED`` (no-OAuth path) and ``OAUTH_ATTACHED``
    (with-OAuth path), so the same harness drives both flows.
    """
    artifacts = await _step_create_app(
        creator=creator,
        csrf=csrf,
        artifacts=artifacts,
        skill_name=skill_name,
        progress_cb=progress_cb,
    )
    artifacts = await _step_upload_logo_and_update_draft(
        creator=creator,
        csrf=csrf,
        artifacts=artifacts,
        skill_name=skill_name,
        backend_uri=backend_uri,
        logo_bytes=logo_bytes,
        channel=channel,
        description=description,
        structured_examples=structured_examples,
        activation_phrases=activation_phrases,
        category=category,
        voice=voice,
        developer_name=developer_name,
        progress_cb=progress_cb,
    )
    if has_oauth:
        # Type-narrow for mypy: _validate_oauth_args proved all four are non-None.
        # Use explicit raise rather than assert so behaviour is preserved under -O.
        if (
            oauth_authorize_url is None
            or oauth_token_url is None
            or oauth_client_id is None
            or oauth_client_secret is None
        ):
            msg = "internal error: has_oauth=True but oauth_* params are None"
            raise RuntimeError(msg)
        artifacts = await _step_create_oauth_app(
            creator=creator,
            csrf=csrf,
            artifacts=artifacts,
            skill_name=skill_name,
            oauth_authorize_url=oauth_authorize_url,
            oauth_token_url=oauth_token_url,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            progress_cb=progress_cb,
        )
        artifacts = await _step_attach_oauth(
            creator=creator,
            csrf=csrf,
            artifacts=artifacts,
            progress_cb=progress_cb,
        )
    # Custom entities (aliceSkill only) — must run BEFORE _step_set_intents
    # so intent grammars referencing entity types pass Granet validation.
    artifacts = await _step_set_entities(
        creator=creator,
        csrf=csrf,
        artifacts=artifacts,
        channel=channel,
        entities=entities,
    )
    # Custom intents (aliceSkill only) — sync between draft update / OAuth attach
    # and deploy. No-op when intents=None or channel=smartHome.
    artifacts = await _step_set_intents(
        creator=creator,
        csrf=csrf,
        artifacts=artifacts,
        channel=channel,
        intents=intents,
    )
    artifacts = await _step_checkpoint_deploy_requested(
        artifacts=artifacts,
        progress_cb=progress_cb,
    )
    artifacts = await _step_request_deploy(
        creator=creator,
        csrf=csrf,
        artifacts=artifacts,
        progress_cb=progress_cb,
    )
    return artifacts


# ---------------------------------------------------------------------------
# Pipeline step helpers — each is a single-state-transition Yandex API call.
# Each helper is idempotent w.r.t. its input state: if the state is past its
# transition, it returns artifacts unchanged.
# ---------------------------------------------------------------------------


async def _step_create_app(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """NONE/FAILED → APP_CREATED via ``creator.create_app``.

    On retry from ``state=FAILED`` an existing ``skill_id`` means a previous
    run already created the skill but failed at a later step — promote to
    ``APP_CREATED`` instead of creating a duplicate skill.
    """
    if artifacts.state not in (SkillCreationState.NONE, SkillCreationState.FAILED):
        return artifacts
    if artifacts.skill_id is not None:
        # Resume: skill already created in an earlier attempt.
        artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.APP_CREATED,
            last_error=None,
        )
        await _maybe_save(progress_cb, artifacts)
        return artifacts
    _LOGGER.info("auto-skill: creating skill app")
    new_skill_id = await creator.create_app(csrf, skill_name)
    artifacts = dataclasses.replace(
        artifacts,
        state=SkillCreationState.APP_CREATED,
        skill_id=new_skill_id,
        last_error=None,
    )
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_upload_logo_and_update_draft(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    backend_uri: str,
    logo_bytes: bytes,
    channel: Channel,
    description: str | None,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    category: str | None,
    voice: str | None,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """APP_CREATED → DRAFT_UPDATED via ``upload_logo`` + ``update_draft``."""
    if artifacts.state != SkillCreationState.APP_CREATED:
        return artifacts
    if artifacts.skill_id is None:
        msg = "internal error: skill_id missing at logo+draft step"
        raise RuntimeError(msg)
    skill_id: str = artifacts.skill_id

    logo_id = artifacts.logo_id
    if logo_id is None:
        _LOGGER.info("auto-skill: uploading logo")
        logo_id = await creator.upload_logo(csrf, skill_id, logo_bytes)
        artifacts = dataclasses.replace(artifacts, logo_id=logo_id)

    if channel == DIALOG_CHANNEL:
        if description is None:
            msg = "description is required for channel='aliceSkill'"
            raise ValueError(msg)
        draft = build_dialog_draft_payload(
            skill_name=skill_name,
            backend_uri=backend_uri,
            logo_id=logo_id,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
            category=category if category is not None else "music_audio",
            voice=voice if voice is not None else "good_oksana",
            developer_name=developer_name,
        )
    else:
        draft = build_smart_home_draft_payload(
            skill_name=skill_name,
            backend_uri=backend_uri,
            logo_id=logo_id,
            developer_name=developer_name,
        )
    _LOGGER.info("auto-skill: updating draft with settings")
    await creator.update_draft(csrf, skill_id, draft)
    artifacts = dataclasses.replace(
        artifacts,
        state=SkillCreationState.DRAFT_UPDATED,
        last_known_name=skill_name,
    )
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_create_oauth_app(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    oauth_authorize_url: str,
    oauth_token_url: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """DRAFT_UPDATED → OAUTH_CREATED via ``creator.create_oauth_app``."""
    if artifacts.state != SkillCreationState.DRAFT_UPDATED:
        return artifacts
    _LOGGER.info("auto-skill: creating OAuth app")
    oauth_app_id = await creator.create_oauth_app(
        csrf,
        name=skill_name,
        client_id=oauth_client_id,
        client_secret=oauth_client_secret,
        authorize_url=oauth_authorize_url,
        token_url=oauth_token_url,
        refresh_url=oauth_token_url,
    )
    artifacts = dataclasses.replace(
        artifacts,
        state=SkillCreationState.OAUTH_CREATED,
        oauth_app_id=oauth_app_id,
    )
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_attach_oauth(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """OAUTH_CREATED → OAUTH_ATTACHED via ``creator.attach_oauth``."""
    if artifacts.state != SkillCreationState.OAUTH_CREATED:
        return artifacts
    if artifacts.skill_id is None or artifacts.oauth_app_id is None:
        msg = "internal error: skill_id/oauth_app_id missing at attach_oauth"
        raise RuntimeError(msg)
    await creator.attach_oauth(csrf, artifacts.skill_id, artifacts.oauth_app_id)
    artifacts = dataclasses.replace(artifacts, state=SkillCreationState.OAUTH_ATTACHED)
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_checkpoint_deploy_requested(
    *,
    artifacts: SkillCreationArtifacts,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """{OAUTH_ATTACHED, DRAFT_UPDATED} → DEPLOY_REQUESTED checkpoint.

    Persists ``DEPLOY_REQUESTED`` *before* the network call so a crash after
    Yandex accepted the deploy but before we returned can skip straight to
    DONE on retry (the underlying API is idempotent for re-deploys but we'd
    rather not redrive the whole flow).

    Smart-home arrives here from ``OAUTH_ATTACHED``; OAuth-free dialog
    pipelines from ``DRAFT_UPDATED``.
    """
    if artifacts.state in (
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DRAFT_UPDATED,
    ):
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DEPLOY_REQUESTED)
        await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_set_entities(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    channel: Channel,
    entities: list[EntityDraft] | None,
) -> SkillCreationArtifacts:
    """Sync custom entities on the skill. No-op when not applicable.

    Custom entities are an ``aliceSkill``-only feature, so the step
    short-circuits on ``smartHome``. ``entities=None`` means "leave
    whatever's there alone"; ``entities=[]`` clears all custom entities
    (declarative empty state).

    Doesn't transition the state machine — runs between draft update /
    OAuth attach and ``_step_set_intents``. Idempotent server-side, so
    retries are cheap.
    """
    if channel != DIALOG_CHANNEL or entities is None:
        return artifacts
    if artifacts.skill_id is None:
        return artifacts
    if artifacts.state not in (
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_ATTACHED,
    ):
        return artifacts
    _LOGGER.info(
        "auto-skill: syncing %d custom entit%s on skill %s",
        len(entities),
        "y" if len(entities) == 1 else "ies",
        artifacts.skill_id,
    )
    await creator.set_entities(csrf, artifacts.skill_id, entities)
    return artifacts


async def _step_set_intents(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    channel: Channel,
    intents: list[IntentDraft] | None,
) -> SkillCreationArtifacts:
    """Sync custom intents on the skill. No-op when intents are not applicable.

    Custom intents are an ``aliceSkill``-only feature, so the step short-circuits
    on ``smartHome``. ``intents=None`` means "leave whatever's there alone";
    ``intents=[]`` means "delete everything custom" (declarative empty state).

    Doesn't transition the state machine — runs between ``DRAFT_UPDATED`` /
    ``OAUTH_ATTACHED`` and ``DEPLOY_REQUESTED`` as a side-effecting sync, and
    relies on :meth:`DialogsSkillCreator.set_intents`' idempotency for retries.
    """
    if channel != DIALOG_CHANNEL or intents is None:
        return artifacts
    if artifacts.skill_id is None:
        return artifacts
    if artifacts.state not in (
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_ATTACHED,
    ):
        return artifacts
    _LOGGER.info(
        "auto-skill: syncing %d custom intent(s) on skill %s",
        len(intents),
        artifacts.skill_id,
    )
    await creator.set_intents(csrf, artifacts.skill_id, intents)
    return artifacts


async def _step_request_deploy(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """DEPLOY_REQUESTED → DONE via ``creator.request_deploy``.

    Yandex's deploy is async — for ``smartHome`` it usually completes in a
    few seconds, but for ``aliceSkill`` it can take 5–15 minutes under typical
    moderation queue conditions. The request was accepted, Yandex will finish
    on its side. Callers surface a direct link to the skill's dev-console
    page so the user can check the on-air indicator at their convenience.
    """
    if artifacts.state != SkillCreationState.DEPLOY_REQUESTED:
        return artifacts
    if artifacts.skill_id is None:
        msg = "internal error: skill_id missing at request_deploy"
        raise RuntimeError(msg)
    skill_id: str = artifacts.skill_id
    _LOGGER.info("auto-skill: publishing skill")
    await creator.request_deploy(csrf, skill_id)
    _LOGGER.info(
        "auto-skill: deploy requested for skill %s — Yandex processes "
        "this asynchronously (a few seconds for smartHome, several "
        "minutes for aliceSkill). Watch on-air status at "
        "https://dialogs.yandex.ru/developer/skills/%s",
        skill_id,
        skill_id,
    )
    artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DONE)
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def auto_update_skill(
    *,
    authenticator: AuthenticatorCM,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    backend_uri: str,
    channel: Channel = SMART_HOME_CHANNEL,
    description: str | None = None,
    structured_examples: list[dict[str, Any]] | None = None,
    activation_phrases: list[str] | None = None,
    category: str | None = None,
    voice: str | None = None,
    intents: list[IntentDraft] | None = None,
    entities: list[EntityDraft] | None = None,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts:
    """Update a skill draft and re-deploy it.

    Works for both ``channel="smartHome"`` and ``channel="aliceSkill"``.
    Patches the full draft payload and calls ``request_deploy``. Does not
    raise on failure — returns artifacts with ``state=FAILED`` and
    ``last_error`` set so the UI can display the message.

    On success the returned artifacts have ``last_known_name=skill_name``
    and ``state=DONE``.

    Args:
        authenticator: No-arg async context-manager factory yielding an
            authenticated ``aiohttp.ClientSession``.
        artifacts: Current state machine snapshot (must have ``skill_id`` set).
        skill_name: New display name for the skill.
        backend_uri: Webhook backend URL.
        channel: ``"smartHome"`` or ``"aliceSkill"``. Selects the draft
            payload builder and the deploy channel query parameter.
        description: Required for ``channel="aliceSkill"``, ignored for
            ``channel="smartHome"``.
        structured_examples: Alice dialog examples (``aliceSkill`` only).
        activation_phrases: Activation phrases (``aliceSkill`` only).
        category: Skill category (``aliceSkill`` only).
        voice: TTS voice (``aliceSkill`` only).
        intents: Declarative list of custom intents to sync after the
            draft update (``aliceSkill`` only). ``None`` leaves whatever
            is on the server alone; ``[]`` deletes all custom intents.
            Idempotent: matched against the server state by ``form_name``,
            only diffs are PATCHed.
        entities: Declarative list of custom entities to sync (``aliceSkill``
            only). ``None`` leaves the server-side ``customEntities``
            ``sourceText`` alone; ``[]`` clears all custom entities. Synced
            **before** ``intents`` so intent grammars referencing entity
            types pass Granet validation. Single-shot replace via PUT —
            see :meth:`DialogsSkillCreator.set_entities`.
        progress_cb: Awaitable called after each state transition.
        creator_factory: Override for the low-level client (used in tests).
        developer_name: Developer display name embedded in the draft.
    """
    if artifacts.skill_id is None:
        msg = "skill_id is missing — cannot update a skill that has not been created"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)
    if channel == DIALOG_CHANNEL and not (description or "").strip():
        msg = "description (non-empty) is required for channel='aliceSkill'"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)

    skill_id = artifacts.skill_id

    try:
        async with authenticator() as session:
            creator = (
                creator_factory(session)
                if creator_factory is not None
                else DialogsSkillCreator(session, channel=channel)
            )
            csrf = await creator.fetch_csrf()

            if channel == SMART_HOME_CHANNEL:
                draft: dict[str, Any] = build_smart_home_draft_payload(
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    logo_id=artifacts.logo_id,
                    developer_name=developer_name,
                )
            else:
                draft = build_dialog_draft_payload(
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    logo_id=artifacts.logo_id,
                    description=description or "",
                    structured_examples=structured_examples,
                    activation_phrases=activation_phrases,
                    category=category or "music_audio",
                    voice=voice or "good_oksana",
                    developer_name=developer_name,
                )

            await creator.update_draft(csrf, skill_id, draft)
            # Custom entities sync (aliceSkill only). MUST run before
            # set_intents so intent grammars that reference entity
            # types pass Granet validation. set_entities is a single
            # PUT, idempotent on the server side.
            if channel == DIALOG_CHANNEL and entities is not None:
                _LOGGER.info(
                    "auto-skill: syncing %d custom entit%s on skill %s",
                    len(entities),
                    "y" if len(entities) == 1 else "ies",
                    skill_id,
                )
                await creator.set_entities(csrf, skill_id, entities)
            # Custom intents sync (aliceSkill only). set_intents is idempotent
            # against the live state, so re-runs are cheap.
            if channel == DIALOG_CHANNEL and intents is not None:
                _LOGGER.info(
                    "auto-skill: syncing %d custom intent(s) on skill %s",
                    len(intents),
                    skill_id,
                )
                await creator.set_intents(csrf, skill_id, intents)
            await creator.request_deploy(csrf, skill_id)
            _LOGGER.info(
                "auto-skill: skill %r updated to name=%r and re-deployed (channel=%s)",
                skill_id,
                skill_name,
                channel,
            )
            updated = dataclasses.replace(
                artifacts,
                state=SkillCreationState.DONE,
                last_known_name=skill_name,
                last_error=None,
            )
            await _maybe_save(progress_cb, updated)
            return updated
    except asyncio.CancelledError:
        raise
    except DialogsApiError as exc:
        _LOGGER.warning("auto-update-skill failed: %s", exc, exc_info=True)
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=str(exc))
    except Exception as exc:
        _LOGGER.exception("auto-update-skill hit unexpected error")
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=repr(exc))


# Minimal 1x1 transparent PNG — used when the packaged logo asset is missing
# (e.g. during unit tests before the asset commit lands).
_FALLBACK_LOGO_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000050001d0a0c9a30000000049454e44ae426082"
)


def load_default_logo_bytes() -> bytes:
    """Return PNG bytes for the bundled default skill logo.

    Reads ``ya_dialogs_api/assets/default_logo.png`` (packaged via
    ``importlib.resources``). Falls back to a 1x1 transparent PNG so tests
    can run without the asset.
    """
    try:
        ref = importlib.resources.files("ya_dialogs_api.assets").joinpath("default_logo.png")
        return ref.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return _FALLBACK_LOGO_PNG


async def _maybe_save(
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
    artifacts: SkillCreationArtifacts,
) -> None:
    """Call ``progress_cb`` if provided, swallowing any save errors."""
    if progress_cb is None:
        return
    try:
        await progress_cb(artifacts)
    except Exception:
        _LOGGER.exception("progress_cb raised; continuing pipeline anyway")
