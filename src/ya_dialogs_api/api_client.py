"""Low-level client for the undocumented dialogs.yandex.ru developer API.

Implements the 8-step sequence captured from Chrome DevTools HAR for
creating a Smart Home skill with account-linking:

    1. GET  /developer                       → extract CSRF (secretkey)
    2. GET  /developer/app-store-api/snapshot → existing skills list (optional)
    3. POST /developer/app-store-api/apps                      → skill_id
    4. POST /developer/app-store-api/apps/{id}/draft/upload-logo → logo_id
    5. PATCH /developer/app-store-api/apps/{id}/draft/update    → settings
    6. POST /developer/app-store-api/oauth/apps                → oauth_app_id
    7. POST /developer/app-store-api/apps/{id}/oauthApp        → bind oauth
    8. POST /developer/app-store-api/apps/{id}/draft/request-deploy → publish

This is an UNDOCUMENTED, PRIVATE API. It may break at any time. The
caller is responsible for surfacing that risk to the user (see
``provider.auto_skill_ui``).

Authentication: passport session cookies (``Session_id`` / ``sessionid2``)
must already be present in the supplied ``aiohttp.ClientSession``'s
cookie jar. Obtain them via ``ya_passport_auth.PassportClient``:

    creds = await client.login_device_code(...)
    await client.refresh_passport_cookies(creds.x_token)
    creator = DialogsSkillCreator(client._session)

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

from .state import SkillCreationArtifacts, SkillCreationState

# Yandex Dialogs Developer API channel strings (sent in payloads + query strings).
# These are HTTP protocol values, not framework configuration — keep them in the lib.
SMART_HOME_CHANNEL = "smartHome"
DIALOG_CHANNEL = "aliceSkill"

SkillType = Literal["smart_home", "dialog"]

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
    "DialogsApiError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsSkillCreator",
    "SkillType",
    "auto_create_dialog_skill",
    "auto_create_skill",
    "auto_rename_dialog_skill",
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
# Captured from HAR 2026-04-24. If Yandex re-renders differently, this
# regex will miss and ``fetch_csrf`` raises ``DialogsCsrfError`` so the
# user falls back to manual setup.
DIALOGS_CSRF_REGEX = re.compile(r'"secretkey":"([^"]+)"')

_MAX_HTML_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


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


class DialogsDuplicateSkillError(DialogsApiError):
    """Raised when create_app rejects because a skill with the same name exists."""


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
        channel: str = SMART_HOME_CHANNEL,
    ) -> None:
        """Take a session that already carries Passport auth cookies.

        ``channel`` selects the Yandex Dialogs skill family — defaults to
        ``smartHome`` for the existing Smart Home pipeline; pass
        :data:`DIALOG_CHANNEL` for the experimental «Навык» pipeline.
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
        """
        async with self._session.get(DIALOGS_DEV_HTML_URL) as resp:
            if resp.status == 401:
                raise DialogsApiError(
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
            # response can't buffer fully in memory (T5 pattern from
            # ya-passport-auth).
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
        """Create a Smart Home skill with the given name.

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
        form.add_field(
            "file",
            png,
            filename="icon.png",
            content_type="image/png",
        )
        headers = {"x-csrf-token": csrf}
        async with self._session.post(url, data=form, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise DialogsApiError(
                    f"upload_logo HTTP {resp.status}: {body[:200]}",
                    step="upload_logo",
                    http_status=resp.status,
                )
            data = _try_json(body)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise DialogsApiError(
                "upload_logo response missing 'result'",
                step="upload_logo",
            )
        logo_id = result.get("id")
        if not isinstance(logo_id, str) or not logo_id:
            raise DialogsApiError(
                "upload_logo response missing logo id",
                step="upload_logo",
            )
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
                "create_oauth_app response missing 'result'",
                step="create_oauth_app",
            )
        oauth_app_id = result.get("id")
        if not isinstance(oauth_app_id, str) or not oauth_app_id:
            raise DialogsApiError(
                "create_oauth_app response missing oauth app id",
                step="create_oauth_app",
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
                raise DialogsApiError(
                    f"request_deploy HTTP {resp.status}: {body[:200]}",
                    step="request_deploy",
                    http_status=resp.status,
                )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _get_json(self, url: str, *, csrf: str, step: str) -> dict[str, Any]:
        headers = {"x-csrf-token": csrf}
        async with self._session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise DialogsApiError(
                    f"GET {url} HTTP {resp.status}: {body[:200]}",
                    step=step,
                    http_status=resp.status,
                )
            data = _try_json(body)
        if not isinstance(data, dict):
            raise DialogsApiError(
                f"GET {url} returned non-object JSON",
                step=step,
            )
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
        headers = {
            "x-csrf-token": csrf,
            "content-type": "application/json",
        }
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
                    yandex_error=_extract_error_code(body),
                )
            if resp.status not in (200, 201, 202):
                # Empty / very short body 4xx — log a small safe subset of
                # response headers so the user can see what Yandex actually
                # returned (helps diagnose e.g. wrong "channel" parameter
                # where the API rejects the request before generating a body).
                # Avoid dumping the full header map: it includes Set-Cookie
                # and other potentially sensitive values.
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
                    _LOGGER.warning(
                        "Yandex %s %s returned %s with empty body; response "
                        "headers=%s, request payload channel=%r",
                        method,
                        url,
                        resp.status,
                        safe_headers,
                        payload.get("channel"),
                    )
                raise DialogsApiError(
                    f"{method} {url} HTTP {resp.status}: {body[:200] or '<empty>'}",
                    step=step,
                    http_status=resp.status,
                    yandex_error=_extract_error_code(body),
                )
            data = _try_json(body)
        if not isinstance(data, dict):
            raise DialogsApiError(
                f"{method} {url} returned non-object JSON",
                step=step,
            )
        return data


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _try_json(body: str) -> Any:
    """Parse JSON defensively — return None on any error."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _looks_like_duplicate(body: str) -> bool:
    """Heuristic for whether a 4xx body indicates a duplicate-name error."""
    if not body:
        return False
    lowered = body.lower()
    return any(
        kw in lowered for kw in ("already exists", "duplicate", "exists with name", "not_unique")
    )


def _extract_error_code(body: str) -> str | None:
    """Pull Yandex error code/message out of a 4xx response body (best-effort)."""
    data = _try_json(body)
    if not isinstance(data, dict):
        return None
    for key in ("error", "errorCode", "message", "code"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Pure helpers: backend/oauth URLs, payload builders, preconditions
#
# All of these are side-effect-free and separately unit-testable; the
# orchestrator in a later commit wires them together.
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
            "email": "",  # server pulls from the authenticated session
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
    """Compose the PATCH /draft/update body for a Yandex Dialogs custom skill.

    All fields and shapes were captured from a live PATCH issued by the
    dev console after the user filled the form successfully. Notable
    discoveries:

    - ``structuredExamples`` shape: each entry is
      ``{"marker": <activator>, "activationPhrase": <skill_name>,
      "request": <phrase>, "is_valid": true}`` — NOT ``{"phrase": "..."}``
      as we previously guessed (that wrong shape was the cause of all
      the silent HTTP 400 + empty-body rejections from Yandex).
    - ``description``: required non-empty (caller provides domain-relevant text).
    - ``category``: defaults to ``"music_audio"`` (API key for "Аудио и подкасты").
    - ``email``: empty string is OK; Yandex pre-fills it from the user's
      Passport account on its side anyway.

    Args:
        skill_name: Display name of the skill.
        backend_uri: Full HTTPS webhook URL the skill should call.
        logo_id: ID returned by ``upload_logo``, or None.
        description: Required non-empty description shown in the skill catalog.
        structured_examples: Optional override for the list of activator/phrase
            examples shown to moderators. If None, uses a single example
            ``"попроси {skill_name} <something>"`` placeholder.
        activation_phrases: Optional override for the activation phrase list.
            Defaults to ``[skill_name]``.
        category: Yandex catalog category key. Default ``"music_audio"``.
        voice: TTS voice identifier. Default ``"good_oksana"``.
        developer_name: Display name for the developer attribution.
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

    The caller is responsible for computing ``client_id`` / ``client_secret``
    / ``authorize_url`` / ``token_url`` for their integration. ``refreshTokenUrl``
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
# so the same harness can drive both the smart_home (with OAuth) and dialog
# (without OAuth) flows.
_PipelineExecutor = Callable[
    [str, Callable[[SkillCreationArtifacts], Awaitable[None]]],
    Awaitable[SkillCreationArtifacts],
]


async def auto_create_skill(
    *,
    authenticator: AuthenticatorCM,
    skill_name: str,
    artifacts: SkillCreationArtifacts,
    backend_uri: str,
    oauth_authorize_url: str,
    oauth_token_url: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    logo_bytes: bytes,
    skill_type: SkillType = "smart_home",
    dialog_description: str | None = None,
    dialog_structured_examples: list[dict[str, Any]] | None = None,
    dialog_activation_phrases: list[str] | None = None,
    dialog_category: str = "music_audio",
    dialog_voice: str = "good_oksana",
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts:
    """Run the full skill-creation pipeline against an authenticated session.

    The caller is responsible for authentication: ``authenticator`` is a
    no-arg async-context-manager factory that yields a ``aiohttp.ClientSession``
    already carrying Yandex Passport cookies. Typically the caller wraps
    :class:`ya_passport_auth.PassportClient` (Device Flow / QR / cookie login)
    and any UX surface they want around it.

    Resumes from ``artifacts.state`` — steps that already completed
    (skill_id present, etc.) are skipped. On any pipeline error, returns
    artifacts with ``state=FAILED`` and a human-readable ``last_error``
    instead of re-raising, so a config-flow UI can render the message
    without crashing.

    ``progress_cb`` is invoked after each successful step with the
    updated artifacts; persist them and on the next call pass the saved
    artifacts back in so the pipeline resumes from the latest completed step.

    For ``skill_type="dialog"``, ``dialog_description`` is required (Yandex
    rejects empty descriptions for custom Alice skills). For OAuth-free
    dialog-skill creation prefer :func:`auto_create_dialog_skill`.
    """
    if skill_type == "dialog" and not dialog_description:
        msg = "dialog_description is required for skill_type='dialog'"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)

    channel = DIALOG_CHANNEL if skill_type == "dialog" else SMART_HOME_CHANNEL

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
                    oauth_authorize_url=oauth_authorize_url,
                    oauth_token_url=oauth_token_url,
                    oauth_client_id=oauth_client_id,
                    oauth_client_secret=oauth_client_secret,
                    logo_bytes=logo_bytes,
                    skill_type=skill_type,
                    dialog_description=dialog_description,
                    dialog_structured_examples=dialog_structured_examples,
                    dialog_activation_phrases=dialog_activation_phrases,
                    dialog_category=dialog_category,
                    dialog_voice=dialog_voice,
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
        # Preserve cooperative cancellation — do not absorb into FAILED.
        raise
    except ValueError:
        raise
    except Exception as exc:
        _LOGGER.exception("auto-create hit unexpected error")
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=repr(exc))


async def auto_create_dialog_skill(
    *,
    authenticator: AuthenticatorCM,
    skill_name: str,
    artifacts: SkillCreationArtifacts,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None = None,
    activation_phrases: list[str] | None = None,
    category: str = "music_audio",
    voice: str = "good_oksana",
    logo_bytes: bytes | None = None,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts:
    """Create a custom Alice (``aliceSkill``) skill without OAuth account-linking.

    Symmetric to :func:`auto_rename_dialog_skill`. Runs the OAuth-free subset
    of the pipeline: ``create_app → upload_logo → update_draft →
    request_deploy``. Yandex does not require an attached OAuth app for
    custom dialog skills, so callers that don't expose an OAuth provider on
    their backend (most voice-skill use cases) can use this entry point
    directly.

    ``logo_bytes`` defaults to :func:`load_default_logo_bytes` when ``None``.

    Resumes from ``artifacts.state`` (NONE/FAILED → APP_CREATED → DRAFT_UPDATED
    → DEPLOY_REQUESTED → DONE), skipping steps that already completed. Never
    raises on Yandex API errors — surfaces them as ``state=FAILED`` with a
    human-readable ``last_error``.
    """
    if not description:
        msg = "description is required for auto_create_dialog_skill"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)

    if logo_bytes is None:
        logo_bytes = load_default_logo_bytes()

    try:
        async with authenticator() as session:
            creator = (
                creator_factory(session)
                if creator_factory is not None
                else DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
            )

            async def executor(
                csrf: str,
                track: Callable[[SkillCreationArtifacts], Awaitable[None]],
            ) -> SkillCreationArtifacts:
                return await _execute_dialog_pipeline(
                    creator=creator,
                    csrf=csrf,
                    artifacts=artifacts,
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    logo_bytes=logo_bytes,
                    description=description,
                    structured_examples=structured_examples,
                    activation_phrases=activation_phrases,
                    category=category,
                    voice=voice,
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
        _LOGGER.exception("auto-create-dialog hit unexpected error")
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
    oauth_authorize_url: str,
    oauth_token_url: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    logo_bytes: bytes,
    skill_type: SkillType,
    dialog_description: str | None,
    dialog_structured_examples: list[dict[str, Any]] | None,
    dialog_activation_phrases: list[str] | None,
    dialog_category: str,
    dialog_voice: str,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Smart Home / dialog-with-OAuth pipeline.

    Composes the OAuth-free shared steps with the OAuth-app create + attach
    steps that Smart Home skills require for account-linking.
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
        skill_type=skill_type,
        dialog_description=dialog_description,
        dialog_structured_examples=dialog_structured_examples,
        dialog_activation_phrases=dialog_activation_phrases,
        dialog_category=dialog_category,
        dialog_voice=dialog_voice,
        developer_name=developer_name,
        progress_cb=progress_cb,
    )
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


async def _execute_dialog_pipeline(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    backend_uri: str,
    logo_bytes: bytes,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    category: str,
    voice: str,
    developer_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """Dialog-skill pipeline without OAuth account-linking."""
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
        skill_type="dialog",
        dialog_description=description,
        dialog_structured_examples=structured_examples,
        dialog_activation_phrases=activation_phrases,
        dialog_category=category,
        dialog_voice=voice,
        developer_name=developer_name,
        progress_cb=progress_cb,
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
# transition, it returns artifacts unchanged so callers can compose steps in
# different orders without re-checking state in the caller.
# ---------------------------------------------------------------------------


async def _step_create_app(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """NONE/FAILED → APP_CREATED via ``creator.create_app``."""
    if artifacts.state not in (SkillCreationState.NONE, SkillCreationState.FAILED):
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
    skill_type: SkillType,
    dialog_description: str | None,
    dialog_structured_examples: list[dict[str, Any]] | None,
    dialog_activation_phrases: list[str] | None,
    dialog_category: str,
    dialog_voice: str,
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

    if skill_type == "dialog":
        if dialog_description is None:
            msg = "dialog_description is required for skill_type='dialog'"
            raise ValueError(msg)
        draft = build_dialog_draft_payload(
            skill_name=skill_name,
            backend_uri=backend_uri,
            logo_id=logo_id,
            description=dialog_description,
            structured_examples=dialog_structured_examples,
            activation_phrases=dialog_activation_phrases,
            category=dialog_category,
            voice=dialog_voice,
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

    Persists DEPLOY_REQUESTED *before* the network call so a crash after
    Yandex accepted the deploy but before we returned can skip straight to
    DONE on retry (the underlying API is idempotent for re-deploys but we'd
    rather not redrive the whole flow).

    Smart-home / dialog-with-OAuth pipelines arrive here from
    ``OAUTH_ATTACHED``; OAuth-free dialog pipelines from ``DRAFT_UPDATED``.
    """
    if artifacts.state in (
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DRAFT_UPDATED,
    ):
        artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DEPLOY_REQUESTED)
        await _maybe_save(progress_cb, artifacts)
    return artifacts


async def _step_request_deploy(
    *,
    creator: DialogsSkillCreator,
    csrf: str,
    artifacts: SkillCreationArtifacts,
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None,
) -> SkillCreationArtifacts:
    """DEPLOY_REQUESTED → DONE via ``creator.request_deploy``.

    Yandex's deploy is async — for smart_home it usually completes in a few
    seconds, but for aliceSkill ("Навык") it can take 5-15 minutes under
    typical moderation queue conditions. The request was accepted, Yandex
    will finish on its side. Callers surface a direct link to the skill's
    dev-console page so the user can check the on-air indicator at their
    convenience.
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
        "this asynchronously (a few seconds for smart_home, several "
        "minutes for dialog skills). Watch on-air status at "
        "https://dialogs.yandex.ru/developer/skills/%s",
        skill_id,
        skill_id,
    )
    artifacts = dataclasses.replace(artifacts, state=SkillCreationState.DONE)
    await _maybe_save(progress_cb, artifacts)
    return artifacts


async def auto_rename_dialog_skill(
    *,
    authenticator: AuthenticatorCM,
    artifacts: SkillCreationArtifacts,
    new_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None = None,
    activation_phrases: list[str] | None = None,
    category: str = "music_audio",
    voice: str = "good_oksana",
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts:
    """Rename a dialog skill and re-deploy it.

    Patches the draft ``name`` field and calls ``request_deploy``. Does
    not raise on failure — returns artifacts with ``state=FAILED`` and
    ``last_error`` so the UI can display the message.

    On success the returned artifacts have ``last_known_name=new_name``
    and ``state=DONE`` so the drift-detector in the UI clears the banner.
    """
    if artifacts.skill_id is None:
        msg = "skill_id is missing — cannot rename a skill that has not been created"
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=msg)

    skill_id = artifacts.skill_id

    try:
        async with authenticator() as session:
            creator = (
                creator_factory(session)
                if creator_factory is not None
                else DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
            )
            csrf = await creator.fetch_csrf()

            draft = build_dialog_draft_payload(
                skill_name=new_name,
                backend_uri=backend_uri,
                logo_id=artifacts.logo_id,
                description=description,
                structured_examples=structured_examples,
                activation_phrases=activation_phrases,
                category=category,
                voice=voice,
                developer_name=developer_name,
            )
            await creator.update_draft(csrf, skill_id, draft)
            await creator.request_deploy(csrf, skill_id)
            _LOGGER.info("auto-skill: dialog skill renamed to %r and re-deployed", new_name)
            return dataclasses.replace(
                artifacts,
                state=SkillCreationState.DONE,
                last_known_name=new_name,
                last_error=None,
            )
    except asyncio.CancelledError:
        raise
    except DialogsApiError as exc:
        _LOGGER.warning("rename-dialog-skill failed: %s", exc, exc_info=True)
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=str(exc))
    except Exception as exc:
        _LOGGER.exception("rename-dialog-skill hit unexpected error")
        return dataclasses.replace(artifacts, state=SkillCreationState.FAILED, last_error=repr(exc))


# Minimal 1x1 transparent PNG — used when the packaged logo asset is missing
# (e.g. during unit tests before the asset commit lands).
_FALLBACK_LOGO_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000050001d0a0c9a30000000049454e44ae426082"
)


def load_default_logo_bytes() -> bytes:
    """Return PNG bytes for the bundled default skill logo.

    Reads ``ya_dialogs_api/assets/default_logo.png`` (packaged via importlib.resources).
    Falls back to a 1x1 transparent PNG so tests can run without the asset.
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
