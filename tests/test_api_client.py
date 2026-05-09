"""Tests for ya_dialogs_api.api_client — DialogsSkillCreator + orchestrator."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiohttp
import pytest

from ya_dialogs_api import (
    DIALOG_CHANNEL,
    DIALOGS_API_BASE,
    DIALOGS_DEV_HTML_URL,
    SMART_HOME_CHANNEL,
    DialogsApiError,
    DialogsAuthError,
    DialogsCsrfError,
    DialogsDuplicateSkillError,
    DialogsIntentValidationError,
    DialogsSkillCreator,
    DialogsSkillNotFoundError,
    DialogsValidationError,
    IntentDraft,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
    auto_update_skill,
    build_dialog_draft_payload,
    build_oauth_app_payload,
    build_smart_home_draft_payload,
    load_default_logo_bytes,
)
from ya_dialogs_api.errors import parse_error_body

# Pre-computed URLs / OAuth params used by orchestrator tests.
# (Replaces the smarthome connection-type derivation that lived inside the lib.)
_TEST_BACKEND_URI = "https://example.test/api/yandex_smarthome"
_TEST_AUTH_AUTHORIZE_URL = "https://example.test/api/yandex_smarthome/auth/authorize"
_TEST_AUTH_TOKEN_URL = "https://example.test/api/yandex_smarthome/auth/token"
_TEST_OAUTH_CLIENT_ID = "https://social.yandex.net/"
_TEST_OAUTH_CLIENT_SECRET = "test-secret"
_TEST_DIALOG_BACKEND_URI = "https://example.test/api/yandex_dialogs/webhook/secret"
_TEST_DIALOG_DESCRIPTION = "Test skill description for Yandex moderation."

# ---------------------------------------------------------------------------
# Test helpers: mock aiohttp session matching existing test_cloud.py pattern
# ---------------------------------------------------------------------------


def _mock_response(*, status: int = 200, body_text: str = "", body_json: Any = None) -> AsyncMock:
    """Build a mock aiohttp.ClientResponse.

    If *body_json* is given, ``text()`` returns its JSON-encoded form;
    otherwise ``body_text`` is used verbatim. Also wires up
    ``content.iter_chunked(size)`` as a single-chunk async iterator over
    ``body_text.encode()`` so ``fetch_csrf``'s streaming reader works.
    """
    resp = AsyncMock()
    resp.status = status
    if body_json is not None:
        body_text = json.dumps(body_json, ensure_ascii=False)
    resp.text = AsyncMock(return_value=body_text)
    resp.get_encoding = MagicMock(return_value="utf-8")

    body_bytes = body_text.encode("utf-8")

    async def _aiter(_size: int) -> Any:  # pragma: no cover — trivial
        if body_bytes:
            yield body_bytes

    resp.content = MagicMock()
    resp.content.iter_chunked = _aiter
    return resp


def _install_ctx(session_method: MagicMock, mock_resp: AsyncMock) -> MagicMock:
    """Attach an async context manager returning *mock_resp* to a session method."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session_method.return_value = ctx
    return ctx


def _make_session() -> MagicMock:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock()
    session.post = MagicMock()
    session.request = MagicMock()
    return session


# ---------------------------------------------------------------------------
# fetch_csrf
# ---------------------------------------------------------------------------


class TestFetchCsrf:
    """CSRF token extraction from developer console HTML."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_token(self) -> None:
        """CSRF is extracted from the secretkey field in the developer HTML."""
        html = (
            "<html><head><script>"
            'window.state = {"user":{"id":1},"secretkey":"u9c94f1aca53bf156be4abc","foo":"bar"}'
            "</script></head></html>"
        )
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=200, body_text=html))
        creator = DialogsSkillCreator(session)

        token = await creator.fetch_csrf()
        assert token == "u9c94f1aca53bf156be4abc"
        # Verify we hit the expected URL
        session.get.assert_called_once_with(DIALOGS_DEV_HTML_URL, allow_redirects=False)

    @pytest.mark.asyncio
    async def test_regex_miss_raises_csrf_error(self) -> None:
        """Yandex changed the HTML format → clean typed error for fallback."""
        html = "<html>no secretkey here</html>"
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=200, body_text=html))
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsCsrfError) as exc_info:
            await creator.fetch_csrf()
        assert exc_info.value.step == "fetch_csrf"

    @pytest.mark.asyncio
    async def test_401_maps_to_auth_error(self) -> None:
        """401 means passport cookies missing/expired — retryable via relogin."""
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=401, body_text="nope"))
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsApiError) as exc_info:
            await creator.fetch_csrf()
        assert exc_info.value.http_status == 401
        assert exc_info.value.step == "fetch_csrf"

    @pytest.mark.asyncio
    async def test_empty_token_raises(self) -> None:
        """Regex matched but captured empty string — treat as miss."""
        html = '{"secretkey":""}'
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=200, body_text=html))
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsCsrfError):
            await creator.fetch_csrf()


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


class TestCreateApp:
    """POST /apps — returns skill_id on success."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_skill_id(self) -> None:
        """POST /apps returns a skill UUID and sends the expected payload."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=200,
                body_json={"result": {"id": "7584419b-6815-4a68-8e32-b9fb111b596d"}},
            ),
        )
        creator = DialogsSkillCreator(session)

        skill_id = await creator.create_app("csrf-token", "My Skill")
        assert skill_id == "7584419b-6815-4a68-8e32-b9fb111b596d"

        # Verify request shape
        method, url = session.request.call_args.args[:2]
        assert method == "POST"
        assert url == f"{DIALOGS_API_BASE}/apps"
        payload = session.request.call_args.kwargs["json"]
        assert payload["channel"] == "smartHome"
        assert payload["language"] == "ru"
        assert payload["isYangoConsole"] is False
        assert payload["appName"] == "My Skill"
        headers = session.request.call_args.kwargs["headers"]
        assert headers["x-csrf-token"] == "csrf-token"

    @pytest.mark.asyncio
    async def test_duplicate_name_raises_typed_error(self) -> None:
        """HTTP 409 with a duplicate-indicator body maps to DialogsDuplicateSkillError."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=409,
                body_json={"error": "not_unique", "message": "skill already exists"},
            ),
        )
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsDuplicateSkillError) as exc_info:
            await creator.create_app("csrf", "duplicate name")
        assert exc_info.value.http_status == 409

    @pytest.mark.asyncio
    async def test_generic_4xx_raises_api_error(self) -> None:
        """Non-duplicate 4xx surfaces as plain DialogsApiError, not the subclass."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(status=400, body_text="bad request"),
        )
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsApiError) as exc_info:
            await creator.create_app("csrf", "X")
        # Not a duplicate — should be plain DialogsApiError, not subclass
        assert not isinstance(exc_info.value, DialogsDuplicateSkillError)
        assert exc_info.value.http_status == 400

    @pytest.mark.asyncio
    async def test_missing_skill_id_raises(self) -> None:
        """A 200 response without an id field is treated as a protocol break."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(status=200, body_json={"result": {}}),
        )
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsApiError):
            await creator.create_app("csrf", "X")


# ---------------------------------------------------------------------------
# upload_logo
# ---------------------------------------------------------------------------


class TestUploadLogo:
    """POST /apps/{id}/draft/upload-logo — multipart file upload."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_logo_id(self) -> None:
        """upload_logo posts multipart PNG and returns the avatar id."""
        session = _make_session()
        _install_ctx(
            session.post,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "id": "be043706-a868-4999-83c8-f17bbd60745d",
                        "url": "https://avatars.mds.yandex.net/...",
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session)

        logo_id = await creator.upload_logo("csrf", "skill-1", b"\x89PNG\r\n\x1a\n...")
        assert logo_id == "be043706-a868-4999-83c8-f17bbd60745d"

        call = session.post.call_args
        url = call.args[0]
        assert "/draft/upload-logo" in url
        assert "channel=smartHome" in url
        # FormData used for multipart
        assert isinstance(call.kwargs["data"], aiohttp.FormData)
        assert call.kwargs["headers"]["x-csrf-token"] == "csrf"

    @pytest.mark.asyncio
    async def test_upload_500_raises(self) -> None:
        """Server-side 500 surfaces as DialogsApiError carrying the step name."""
        session = _make_session()
        _install_ctx(session.post, _mock_response(status=500, body_text="oops"))
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsApiError) as exc_info:
            await creator.upload_logo("csrf", "sk", b"data")
        assert exc_info.value.step == "upload_logo"
        assert exc_info.value.http_status == 500


# ---------------------------------------------------------------------------
# update_draft
# ---------------------------------------------------------------------------


class TestUpdateDraft:
    """PATCH /apps/{id}/draft/update — accepts arbitrary payload dict."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        """PATCH goes to the right URL with the forwarded payload and CSRF header."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(status=200, body_json={"result": {"ok": True}}),
        )
        creator = DialogsSkillCreator(session)

        payload = {"name": "Music Assistant", "channel": "smartHome"}
        await creator.update_draft("csrf", "skill-1", payload)

        method, url = session.request.call_args.args[:2]
        assert method == "PATCH"
        assert url == f"{DIALOGS_API_BASE}/apps/skill-1/draft/update"
        assert session.request.call_args.kwargs["json"] == payload


# ---------------------------------------------------------------------------
# create_oauth_app
# ---------------------------------------------------------------------------


class TestCreateOAuthApp:
    """POST /oauth/apps — returns oauth_app_id."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        """create_oauth_app builds the correct payload and returns the new id."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=200,
                body_json={"result": {"id": "oauth-uuid-123"}},
            ),
        )
        creator = DialogsSkillCreator(session)

        oauth_id = await creator.create_oauth_app(
            "csrf",
            name="My Skill",
            client_id="yandex_smart_home:inst1",
            client_secret="secret",
            authorize_url="https://yaha-cloud.ru/oauth/authorize",
            token_url="https://yaha-cloud.ru/oauth/token",
            refresh_url="https://yaha-cloud.ru/oauth/token",
        )
        assert oauth_id == "oauth-uuid-123"

        payload = session.request.call_args.kwargs["json"]
        assert payload["clientId"] == "yandex_smart_home:inst1"
        assert payload["clientSecret"] == "secret"
        assert payload["authorizationUrl"] == "https://yaha-cloud.ru/oauth/authorize"
        assert payload["scope"] == ""


# ---------------------------------------------------------------------------
# attach_oauth
# ---------------------------------------------------------------------------


class TestAttachOAuth:
    """POST /apps/{id}/oauthApp — links oauth_app to skill."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        """attach_oauth sends POST with channel query + oauthAppId body."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(status=200, body_json={"result": "oauth-id"}),
        )
        creator = DialogsSkillCreator(session)

        await creator.attach_oauth("csrf", "skill-1", "oauth-1")

        method, url = session.request.call_args.args[:2]
        assert method == "POST"
        assert "channel=smartHome" in url
        assert session.request.call_args.kwargs["json"] == {"oauthAppId": "oauth-1"}


# ---------------------------------------------------------------------------
# request_deploy
# ---------------------------------------------------------------------------


class TestRequestDeploy:
    """POST /apps/{id}/draft/request-deploy — publishes draft."""

    @pytest.mark.asyncio
    async def test_happy_path_empty_body(self) -> None:
        """Request-deploy uses an empty body and relies on the URL query only."""
        session = _make_session()
        _install_ctx(session.post, _mock_response(status=200, body_json={"result": {}}))
        creator = DialogsSkillCreator(session)

        await creator.request_deploy("csrf", "skill-1")

        call = session.post.call_args
        url = call.args[0]
        assert "request-deploy" in url
        assert "channel=smartHome" in url
        # No body (only headers)
        assert "data" not in call.kwargs
        assert "json" not in call.kwargs
        assert call.kwargs["headers"]["x-csrf-token"] == "csrf"

    @pytest.mark.asyncio
    async def test_deploy_accepts_2xx_variants(self) -> None:
        """Some publish responses are 202/204 depending on server side."""
        for status in (201, 202, 204):
            session = _make_session()
            _install_ctx(session.post, _mock_response(status=status, body_text=""))
            creator = DialogsSkillCreator(session)
            await creator.request_deploy("csrf", "skill-1")  # must not raise


# ---------------------------------------------------------------------------
# list_existing_skills
# ---------------------------------------------------------------------------


class TestListExistingSkills:
    """GET /snapshot — returns existing skills; raises DialogsApiError on malformed JSON."""

    @pytest.mark.asyncio
    async def test_returns_skill_dicts(self) -> None:
        """Snapshot is parsed into a list of skill dicts."""
        session = _make_session()
        _install_ctx(
            session.get,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "skills": [
                            {"id": "s1", "name": "First"},
                            {"id": "s2", "name": "Second"},
                        ]
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session)

        skills = await creator.list_existing_skills("csrf")
        assert len(skills) == 2
        assert skills[0]["name"] == "First"

    @pytest.mark.asyncio
    async def test_raises_when_malformed(self) -> None:
        """Non-JSON snapshot body is a protocol break — raise instead of hiding it."""
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=200, body_text="not json"))
        creator = DialogsSkillCreator(session)

        with pytest.raises(DialogsApiError):
            await creator.list_existing_skills("csrf")


# ---------------------------------------------------------------------------
# Payload builders (pure functions)
# ---------------------------------------------------------------------------


class TestBuildSmartHomeDraftPayload:
    """build_smart_home_draft_payload — Smart Home draft body."""

    def test_returns_dict_with_required_keys(self) -> None:
        """Payload contains every key Yandex's validator expects."""
        payload = build_smart_home_draft_payload(
            skill_name="Music Assistant",
            backend_uri="https://yaha-cloud.ru/api/yandex_smart_home",
            logo_id="be043706-a868-4999-83c8-f17bbd60745d",
            developer_name="alice",
        )
        for key in ("name", "logoId", "backendSettings", "publishingSettings", "channel"):
            assert key in payload
        assert payload["name"] == "Music Assistant"
        assert payload["logoId"] == "be043706-a868-4999-83c8-f17bbd60745d"
        assert payload["backendSettings"]["uri"] == "https://yaha-cloud.ru/api/yandex_smart_home"
        assert payload["publishingSettings"]["category"] == "smart_home"
        assert payload["publishingSettings"]["developerName"] == "alice"
        assert payload["channel"] == "smartHome"

    def test_logo_id_can_be_none(self) -> None:
        """Skipping the logo step still produces a valid (logoId-less) draft."""
        payload = build_smart_home_draft_payload(
            skill_name="x",
            backend_uri="https://x.test/api",
            logo_id=None,
        )
        assert payload["logoId"] is None


class TestBuildDialogDraftPayload:
    """build_dialog_draft_payload — Alice custom-skill draft body."""

    def test_required_fields(self) -> None:
        """Description is mandatory; structuredExamples + activationPhrases get sane defaults."""
        payload = build_dialog_draft_payload(
            skill_name="My Skill",
            backend_uri="https://x.test/webhook",
            logo_id=None,
            description="Voice control demo skill.",
        )
        assert payload["name"] == "My Skill"
        assert payload["channel"] == "aliceSkill"
        assert payload["activationPhrases"] == ["My Skill"]
        assert payload["publishingSettings"]["description"] == "Voice control demo skill."
        assert len(payload["publishingSettings"]["structuredExamples"]) >= 1
        # Default category and voice
        assert payload["publishingSettings"]["category"] == "music_audio"
        assert payload["voice"] == "good_oksana"

    def test_overrides_for_examples_and_phrases(self) -> None:
        """Caller can override examples/phrases/category/voice."""
        examples = [
            {
                "marker": "попроси",
                "activationPhrase": "X",
                "request": "включи джаз",
                "is_valid": True,
            },
        ]
        payload = build_dialog_draft_payload(
            skill_name="X",
            backend_uri="https://x.test/y",
            logo_id="abc",
            description="d",
            structured_examples=examples,
            activation_phrases=["X", "Х"],
            category="other",
            voice="alyss",
        )
        assert payload["publishingSettings"]["structuredExamples"] == examples
        assert payload["activationPhrases"] == ["X", "Х"]
        assert payload["publishingSettings"]["category"] == "other"
        assert payload["voice"] == "alyss"


class TestBuildOAuthAppPayload:
    """build_oauth_app_payload round-trips the OAuth app fields."""

    def test_round_trips_fields(self) -> None:
        payload = build_oauth_app_payload(
            skill_name="Music Assistant",
            client_id="https://social.yandex.net/",
            client_secret="abc123deadbeef",
            authorize_url="https://x.test/auth/authorize",
            token_url="https://x.test/auth/token",
        )
        assert payload == {
            "name": "Music Assistant",
            "clientId": "https://social.yandex.net/",
            "clientSecret": "abc123deadbeef",
            "authorizationUrl": "https://x.test/auth/authorize",
            "tokenUrl": "https://x.test/auth/token",
            "refreshTokenUrl": "https://x.test/auth/token",
            "scope": "",
            "yandexClientId": "",
        }


# ---------------------------------------------------------------------------
# Orchestrator: auto_create_skill / auto_rename_dialog_skill
# ---------------------------------------------------------------------------


def _make_creator_mock() -> AsyncMock:
    """Build an AsyncMock DialogsSkillCreator with all-success defaults."""
    creator = AsyncMock(spec=DialogsSkillCreator)
    creator.fetch_csrf.return_value = "csrf-token"
    creator.create_app.return_value = "skill-id-123"
    creator.upload_logo.return_value = "logo-id-456"
    creator.update_draft.return_value = None
    creator.create_oauth_app.return_value = "oauth-app-id-789"
    creator.attach_oauth.return_value = None
    creator.request_deploy.return_value = None
    return creator


def _fake_authenticator(session: aiohttp.ClientSession | MagicMock | None = None) -> Any:
    """Build a no-arg async-context-manager factory yielding *session*.

    Matches the ``AuthenticatorCM`` Protocol the lib expects. If *session*
    is None, yields a MagicMock spec'd against aiohttp.ClientSession.
    """
    if session is None:
        session = MagicMock(spec=aiohttp.ClientSession)

    @asynccontextmanager
    async def _cm() -> AsyncIterator[aiohttp.ClientSession]:
        yield session

    def _factory() -> Any:
        return _cm()

    return _factory


async def _run_orch(
    *,
    creator: AsyncMock,
    artifacts: SkillCreationArtifacts | None = None,
    progress_cb: Any = None,
    channel: str = SMART_HOME_CHANNEL,
    description: str | None = None,
) -> SkillCreationArtifacts:
    """Run auto_create_skill (smart_home, with OAuth) — fake authenticator + injected creator."""
    return await auto_create_skill(
        authenticator=_fake_authenticator(),
        skill_name="Test Skill",
        artifacts=artifacts if artifacts is not None else SkillCreationArtifacts(),
        backend_uri=_TEST_BACKEND_URI,
        oauth_authorize_url=_TEST_AUTH_AUTHORIZE_URL,
        oauth_token_url=_TEST_AUTH_TOKEN_URL,
        oauth_client_id=_TEST_OAUTH_CLIENT_ID,
        oauth_client_secret=_TEST_OAUTH_CLIENT_SECRET,
        logo_bytes=b"\x89PNG fake",
        channel=channel,  # type: ignore[arg-type]
        description=description,
        creator_factory=lambda _session: creator,
        progress_cb=progress_cb,
    )


class TestAutoCreateSkillHappyPath:
    """auto_create_skill end-to-end: each pipeline step runs in order."""

    @pytest.mark.asyncio
    async def test_smart_home_pipeline_runs_all_steps(self) -> None:
        """All 5 pipeline calls happen and final state is DONE."""
        creator = _make_creator_mock()
        result = await _run_orch(creator=creator)
        assert result.state == SkillCreationState.DONE
        assert result.skill_id == "skill-id-123"
        assert result.logo_id == "logo-id-456"
        assert result.oauth_app_id == "oauth-app-id-789"
        assert result.last_error is None
        creator.fetch_csrf.assert_awaited_once()
        creator.create_app.assert_awaited_once_with("csrf-token", "Test Skill")
        creator.upload_logo.assert_awaited_once()
        creator.update_draft.assert_awaited_once()
        creator.create_oauth_app.assert_awaited_once()
        creator.attach_oauth.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_progress_cb_called_per_state_transition(self) -> None:
        """progress_cb fires at every state checkpoint."""
        states_seen: list[SkillCreationState] = []

        async def _cb(art: SkillCreationArtifacts) -> None:
            states_seen.append(art.state)

        result = await _run_orch(creator=_make_creator_mock(), progress_cb=_cb)
        assert result.state == SkillCreationState.DONE
        # Should see APP_CREATED, DRAFT_UPDATED, OAUTH_CREATED, OAUTH_ATTACHED,
        # DEPLOY_REQUESTED, DONE.
        assert SkillCreationState.APP_CREATED in states_seen
        assert SkillCreationState.DRAFT_UPDATED in states_seen
        assert SkillCreationState.OAUTH_CREATED in states_seen
        assert SkillCreationState.OAUTH_ATTACHED in states_seen
        assert SkillCreationState.DEPLOY_REQUESTED in states_seen
        assert states_seen[-1] == SkillCreationState.DONE


class TestAutoCreateSkillResume:
    """Pipeline skips already-completed steps when given partial artifacts."""

    @pytest.mark.asyncio
    async def test_resume_from_app_created_skips_create(self) -> None:
        """Resuming from APP_CREATED skips create_app."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="existing-skill",
        )
        result = await _run_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        assert result.skill_id == "existing-skill"  # not overwritten
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_awaited_once()
        creator.update_draft.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_from_failed_with_skill_id_does_not_recreate(self) -> None:
        """Retry after FAILED with existing skill_id must not call create_app again.

        Otherwise callers persisting artifacts and retrying would create
        duplicate skills on every transient failure.
        """
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.FAILED,
            skill_id="already-created",
            last_error="prev attempt died at logo upload",
        )
        result = await _run_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        assert result.skill_id == "already-created"  # ID preserved, not overwritten
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_awaited_once()
        creator.update_draft.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_from_oauth_attached_only_publishes(self) -> None:
        """Resuming from OAUTH_ATTACHED only runs request_deploy."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.OAUTH_ATTACHED,
            skill_id="x",
            logo_id="y",
            oauth_app_id="z",
        )
        result = await _run_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_not_awaited()
        creator.update_draft.assert_not_awaited()
        creator.create_oauth_app.assert_not_awaited()
        creator.attach_oauth.assert_not_awaited()
        creator.request_deploy.assert_awaited_once()


class TestAutoCreateSkillFailure:
    """Pipeline failures surface as FAILED artifacts with last_error."""

    @pytest.mark.asyncio
    async def test_create_app_failure_returns_failed(self) -> None:
        """A DialogsApiError mid-pipeline returns FAILED artifacts (no raise)."""
        creator = _make_creator_mock()
        creator.create_app.side_effect = DialogsApiError(
            "duplicate", step="create_app", http_status=409
        )
        result = await _run_orch(creator=creator)
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "duplicate" in result.last_error
        creator.upload_logo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_progress_preserved_on_failure(self) -> None:
        """If draft fails after the app step, the FAILED artifacts retain skill_id.

        Note: logo_id is intentionally only checkpointed together with the
        DRAFT_UPDATED transition — the logo+draft pair is treated as a single
        atomic step so a partially-uploaded skill always retries the
        logo+draft together. skill_id, oauth_app_id are persisted independently.
        """
        creator = _make_creator_mock()
        creator.update_draft.side_effect = DialogsApiError(
            "patch failed", step="update_draft", http_status=500
        )
        result = await _run_orch(creator=creator)
        assert result.state == SkillCreationState.FAILED
        assert result.skill_id == "skill-id-123"
        assert result.oauth_app_id is None  # never reached


class TestAutoCreateSkillDialogChannel:
    """channel='aliceSkill' with OAuth attached — symmetric to smart_home but dialog payload."""

    @pytest.mark.asyncio
    async def test_dialog_requires_description(self) -> None:
        """Empty description → FAILED with explicit message, no API calls."""
        creator = _make_creator_mock()
        result = await _run_orch(creator=creator, channel=DIALOG_CHANNEL, description=None)
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "description" in result.last_error
        creator.fetch_csrf.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dialog_whitespace_description_returns_failed(self) -> None:
        """Whitespace-only description → FAILED before any API call."""
        creator = _make_creator_mock()
        result = await _run_orch(creator=creator, channel=DIALOG_CHANNEL, description="   \t\n  ")
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "description" in result.last_error
        creator.fetch_csrf.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dialog_full_pipeline_uses_dialog_payload(self) -> None:
        """channel='aliceSkill' runs end-to-end with the right payload shape."""
        creator = _make_creator_mock()
        result = await _run_orch(
            creator=creator,
            channel=DIALOG_CHANNEL,
            description=_TEST_DIALOG_DESCRIPTION,
        )
        assert result.state == SkillCreationState.DONE
        # update_draft was called with the dialog payload (channel=aliceSkill).
        update_call = creator.update_draft.await_args
        assert update_call is not None
        payload = (
            update_call.args[2] if len(update_call.args) >= 3 else update_call.kwargs.get("payload")
        )
        assert payload["channel"] == "aliceSkill"
        assert payload["publishingSettings"]["description"] == _TEST_DIALOG_DESCRIPTION


class TestAuthenticatorRequired:
    """authenticator is a required parameter — no default."""

    @pytest.mark.asyncio
    async def test_authenticator_session_passed_to_creator(self) -> None:
        """The session yielded by the authenticator is passed to creator_factory."""
        session = MagicMock(spec=aiohttp.ClientSession)
        captured: list[Any] = []

        def _factory(s: Any) -> Any:
            captured.append(s)
            return _make_creator_mock()

        await auto_create_skill(
            authenticator=_fake_authenticator(session),
            skill_name="x",
            artifacts=SkillCreationArtifacts(),
            backend_uri=_TEST_BACKEND_URI,
            oauth_authorize_url=_TEST_AUTH_AUTHORIZE_URL,
            oauth_token_url=_TEST_AUTH_TOKEN_URL,
            oauth_client_id=_TEST_OAUTH_CLIENT_ID,
            oauth_client_secret=_TEST_OAUTH_CLIENT_SECRET,
            logo_bytes=b"\x89PNG",
            creator_factory=_factory,
        )
        assert captured == [session]


async def _run_dialog_orch(
    *,
    creator: AsyncMock,
    artifacts: SkillCreationArtifacts | None = None,
    progress_cb: Any = None,
    description: str = _TEST_DIALOG_DESCRIPTION,
    logo_bytes: bytes | None = b"\x89PNG fake",
) -> SkillCreationArtifacts:
    """Run auto_create_skill(channel='aliceSkill', no oauth_*) — OAuth-free dialog pipeline."""
    return await auto_create_skill(
        authenticator=_fake_authenticator(),
        skill_name="Test Dialog Skill",
        artifacts=artifacts if artifacts is not None else SkillCreationArtifacts(),
        backend_uri=_TEST_DIALOG_BACKEND_URI,
        channel=DIALOG_CHANNEL,
        description=description,
        logo_bytes=logo_bytes,
        creator_factory=lambda _session: creator,
        progress_cb=progress_cb,
    )


class TestAutoCreateDialogSkillHappyPath:
    """auto_create_skill(channel='aliceSkill') without OAuth: runs the 4-step pipeline."""

    @pytest.mark.asyncio
    async def test_runs_create_logo_draft_deploy_no_oauth(self) -> None:
        """All 4 OAuth-free steps run; OAuth methods never called; final state DONE."""
        creator = _make_creator_mock()
        result = await _run_dialog_orch(creator=creator)
        assert result.state == SkillCreationState.DONE
        assert result.skill_id == "skill-id-123"
        assert result.logo_id == "logo-id-456"
        assert result.last_known_name == "Test Dialog Skill"
        assert result.last_error is None
        # OAuth-free: oauth_app_id never populated
        assert result.oauth_app_id is None
        creator.fetch_csrf.assert_awaited_once()
        creator.create_app.assert_awaited_once_with("csrf-token", "Test Dialog Skill")
        creator.upload_logo.assert_awaited_once()
        creator.update_draft.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()
        # Critical: OAuth methods MUST NOT be called for dialog skills.
        creator.create_oauth_app.assert_not_awaited()
        creator.attach_oauth.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_progress_cb_called_per_state_transition(self) -> None:
        """progress_cb fires at every state checkpoint; no OAuth states emitted."""
        states_seen: list[SkillCreationState] = []

        async def _cb(art: SkillCreationArtifacts) -> None:
            states_seen.append(art.state)

        result = await _run_dialog_orch(creator=_make_creator_mock(), progress_cb=_cb)
        assert result.state == SkillCreationState.DONE
        assert SkillCreationState.APP_CREATED in states_seen
        assert SkillCreationState.DRAFT_UPDATED in states_seen
        assert SkillCreationState.DEPLOY_REQUESTED in states_seen
        assert states_seen[-1] == SkillCreationState.DONE
        # OAuth-free flow never touches OAuth_* states.
        assert SkillCreationState.OAUTH_CREATED not in states_seen
        assert SkillCreationState.OAUTH_ATTACHED not in states_seen

    @pytest.mark.asyncio
    async def test_update_draft_uses_alice_skill_channel_payload(self) -> None:
        """update_draft is invoked with the aliceSkill channel payload."""
        creator = _make_creator_mock()
        await _run_dialog_orch(creator=creator)
        update_call = creator.update_draft.await_args
        assert update_call is not None
        payload = (
            update_call.args[2] if len(update_call.args) >= 3 else update_call.kwargs.get("payload")
        )
        assert payload["channel"] == "aliceSkill"
        assert payload["publishingSettings"]["description"] == _TEST_DIALOG_DESCRIPTION


class TestAutoCreateDialogSkillResume:
    """Pipeline skips already-completed steps when given partial artifacts."""

    @pytest.mark.asyncio
    async def test_resume_from_app_created_skips_create(self) -> None:
        """Resuming from APP_CREATED skips create_app; runs logo+draft+deploy."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="existing-skill",
        )
        result = await _run_dialog_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        assert result.skill_id == "existing-skill"  # not overwritten
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_awaited_once()
        creator.update_draft.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_from_draft_updated_only_publishes(self) -> None:
        """Resuming from DRAFT_UPDATED checkpoints DEPLOY_REQUESTED + publishes only."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DRAFT_UPDATED,
            skill_id="existing-skill",
            logo_id="existing-logo",
        )
        result = await _run_dialog_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_not_awaited()
        creator.update_draft.assert_not_awaited()
        creator.request_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_from_deploy_requested_only_publishes(self) -> None:
        """Resuming from DEPLOY_REQUESTED only runs request_deploy."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DEPLOY_REQUESTED,
            skill_id="existing-skill",
            logo_id="existing-logo",
        )
        result = await _run_dialog_orch(creator=creator, artifacts=artifacts)
        assert result.state == SkillCreationState.DONE
        creator.create_app.assert_not_awaited()
        creator.upload_logo.assert_not_awaited()
        creator.update_draft.assert_not_awaited()
        creator.request_deploy.assert_awaited_once()


class TestAutoCreateDialogSkillFailure:
    """Failures surface as FAILED artifacts with last_error."""

    @pytest.mark.asyncio
    async def test_empty_description_returns_failed_no_network(self) -> None:
        """Empty description short-circuits to FAILED before any API call."""
        creator = _make_creator_mock()
        result = await _run_dialog_orch(creator=creator, description="")
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "description" in result.last_error
        creator.fetch_csrf.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_app_failure_returns_failed(self) -> None:
        """A DialogsApiError mid-pipeline returns FAILED, not a raise."""
        creator = _make_creator_mock()
        creator.create_app.side_effect = DialogsApiError(
            "boom",
            step="create_app",
            http_status=500,
        )
        result = await _run_dialog_orch(creator=creator)
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "boom" in result.last_error
        creator.upload_logo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_skill_returns_failed(self) -> None:
        """DialogsDuplicateSkillError surfaces as FAILED with last_error preserved."""
        creator = _make_creator_mock()
        creator.create_app.side_effect = DialogsDuplicateSkillError(
            "Skill name is already taken",
            step="create_app",
            http_status=409,
        )
        result = await _run_dialog_orch(creator=creator)
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "already taken" in result.last_error

    @pytest.mark.asyncio
    async def test_partial_progress_preserved_on_deploy_failure(self) -> None:
        """If request_deploy fails, FAILED artifacts retain skill_id + logo_id."""
        creator = _make_creator_mock()
        creator.request_deploy.side_effect = DialogsApiError(
            "deploy failed",
            step="request_deploy",
            http_status=500,
        )
        result = await _run_dialog_orch(creator=creator)
        assert result.state == SkillCreationState.FAILED
        assert result.skill_id == "skill-id-123"
        assert result.logo_id == "logo-id-456"


class TestAutoCreateDialogSkillDefaults:
    """Default parameter behaviour."""

    @pytest.mark.asyncio
    async def test_default_logo_bytes_loaded_when_none(self) -> None:
        """logo_bytes=None triggers load_default_logo_bytes."""
        creator = _make_creator_mock()
        result = await _run_dialog_orch(creator=creator, logo_bytes=None)
        assert result.state == SkillCreationState.DONE
        upload_call = creator.upload_logo.await_args
        assert upload_call is not None
        png_arg = (
            upload_call.args[2] if len(upload_call.args) >= 3 else upload_call.kwargs.get("png")
        )
        assert isinstance(png_arg, bytes)
        # PNG magic header (real bundled asset or 1x1 fallback both start with this)
        assert png_arg[:8] == bytes.fromhex("89504e470d0a1a0a")


class TestAutoUpdateSkill:
    """auto_update_skill patches the draft and re-deploys for both channels."""

    @pytest.mark.asyncio
    async def test_smart_home_happy_path(self) -> None:
        """smartHome update produces DONE with new name and a smartHome-shaped payload."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            logo_id="lg-1",
            last_known_name="Old Name",
        )
        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=artifacts,
            skill_name="New Smart Home Name",
            backend_uri=_TEST_BACKEND_URI,
            channel=SMART_HOME_CHANNEL,
            creator_factory=lambda _s: creator,
        )
        assert result.state == SkillCreationState.DONE
        assert result.last_known_name == "New Smart Home Name"
        assert result.last_error is None
        creator.update_draft.assert_awaited_once()
        creator.request_deploy.assert_awaited_once()
        # smartHome payload shape: has publishingSettings.category="smart_home"
        sent_payload = creator.update_draft.await_args.args[2]
        assert sent_payload["channel"] == SMART_HOME_CHANNEL
        assert sent_payload["name"] == "New Smart Home Name"
        assert "structuredExamples" not in sent_payload.get("publishingSettings", {})

    @pytest.mark.asyncio
    async def test_dialog_happy_path(self) -> None:
        """aliceSkill update produces DONE; payload carries description."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            logo_id="lg-1",
        )
        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=artifacts,
            skill_name="New Dialog Name",
            backend_uri=_TEST_DIALOG_BACKEND_URI,
            channel=DIALOG_CHANNEL,
            description="A dialog skill description.",
            creator_factory=lambda _s: creator,
        )
        assert result.state == SkillCreationState.DONE
        assert result.last_known_name == "New Dialog Name"
        sent_payload = creator.update_draft.await_args.args[2]
        assert sent_payload["channel"] == DIALOG_CHANNEL
        assert sent_payload["publishingSettings"]["description"] == "A dialog skill description."

    @pytest.mark.asyncio
    async def test_missing_skill_id_returns_failed_immediately(self) -> None:
        """Updating an uncreated skill is a no-op error, not a crash."""
        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=SkillCreationArtifacts(),  # no skill_id
            skill_name="Whatever",
            backend_uri=_TEST_BACKEND_URI,
        )
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "skill_id" in result.last_error

    @pytest.mark.asyncio
    async def test_dialog_empty_description_returns_failed(self) -> None:
        """aliceSkill requires non-empty description; whitespace alone is rejected."""
        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=SkillCreationArtifacts(skill_id="sk-1", logo_id="lg-1"),
            skill_name="N",
            backend_uri=_TEST_DIALOG_BACKEND_URI,
            channel=DIALOG_CHANNEL,
            description="   ",
        )
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "description" in result.last_error

    @pytest.mark.asyncio
    async def test_dialogs_api_error_returns_failed(self) -> None:
        """A DialogsApiError during update produces FAILED, not a raise."""
        creator = _make_creator_mock()
        creator.update_draft.side_effect = DialogsApiError(
            "moderation rejected", step="update_draft", http_status=400
        )
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            logo_id="lg-1",
        )
        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=artifacts,
            skill_name="N",
            backend_uri=_TEST_BACKEND_URI,
            channel=SMART_HOME_CHANNEL,
            creator_factory=lambda _s: creator,
        )
        assert result.state == SkillCreationState.FAILED
        assert result.last_error is not None
        assert "moderation" in result.last_error

    @pytest.mark.asyncio
    async def test_progress_cb_invoked_on_success(self) -> None:
        """progress_cb fires once with the final DONE artifacts on success."""
        creator = _make_creator_mock()
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            logo_id="lg-1",
        )
        snapshots: list[SkillCreationArtifacts] = []

        async def cb(a: SkillCreationArtifacts) -> None:
            snapshots.append(a)

        result = await auto_update_skill(
            authenticator=_fake_authenticator(),
            artifacts=artifacts,
            skill_name="Updated",
            backend_uri=_TEST_BACKEND_URI,
            channel=SMART_HOME_CHANNEL,
            progress_cb=cb,
            creator_factory=lambda _s: creator,
        )
        assert result.state == SkillCreationState.DONE
        assert len(snapshots) == 1
        assert snapshots[0].last_known_name == "Updated"


class TestLoadDefaultLogoBytes:
    """load_default_logo_bytes reads the bundled PNG from package resources."""

    def test_returns_real_png(self) -> None:
        """Bundled assets/default_logo.png exists and has a PNG magic header."""
        data = load_default_logo_bytes()
        # PNG magic: 89 50 4E 47 0D 0A 1A 0A
        assert data[:8] == bytes.fromhex("89504e470d0a1a0a")
        # Sanity: the bundled asset is non-trivial, not the 1x1 fallback.
        assert len(data) > 1000, f"expected real logo asset, got {len(data)} bytes (fallback?)"


# ---------------------------------------------------------------------------
# delete_skill
# ---------------------------------------------------------------------------


class TestDeleteSkill:
    """DialogsSkillCreator.delete_skill — DELETE /apps/{id}?channel=..."""

    @pytest.mark.asyncio
    async def test_delete_smart_home_skill_returns_none(self) -> None:
        """200 + empty-object body → returns None, channel=smartHome in query string."""
        session = _make_session()
        session.delete = MagicMock()
        _install_ctx(session.delete, _mock_response(status=200, body_text="{}"))
        creator = DialogsSkillCreator(session, channel=SMART_HOME_CHANNEL)
        await creator.delete_skill("csrf", "skill-uuid-123")
        call_args = session.delete.call_args
        url = call_args.args[0]
        assert "/apps/skill-uuid-123" in url
        assert "channel=smartHome" in url
        assert call_args.kwargs["headers"]["x-csrf-token"] == "csrf"

    @pytest.mark.asyncio
    async def test_delete_dialog_skill_uses_alice_channel(self) -> None:
        """channel=aliceSkill in query for dialog skill creator."""
        session = _make_session()
        session.delete = MagicMock()
        _install_ctx(session.delete, _mock_response(status=200, body_text="{}"))
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        await creator.delete_skill("csrf", "skill-uuid-456")
        url = session.delete.call_args.args[0]
        assert "channel=aliceSkill" in url

    @pytest.mark.asyncio
    async def test_delete_404_raises_skill_not_found(self) -> None:
        """Spring 404 'Skill not found' parsed into DialogsSkillNotFoundError."""
        session = _make_session()
        session.delete = MagicMock()
        _install_ctx(
            session.delete,
            _mock_response(
                status=404,
                body_text=(
                    '{"servlet":"dispatcherServlet",'
                    '"message":"Skill not found with id: 12345678-aaaa-bbbb-cccc-1234567890ab",'
                    '"url":"/api/dev-console/v1/apps/12345678-aaaa-bbbb-cccc-1234567890ab",'
                    '"status":"404"}'
                ),
            ),
        )
        creator = DialogsSkillCreator(session, channel=SMART_HOME_CHANNEL)
        with pytest.raises(DialogsSkillNotFoundError) as exc_info:
            await creator.delete_skill("csrf", "12345678-aaaa-bbbb-cccc-1234567890ab")
        assert exc_info.value.skill_id == "12345678-aaaa-bbbb-cccc-1234567890ab"
        assert exc_info.value.http_status == 404


# ---------------------------------------------------------------------------
# Intent CRUD (aliceSkill custom NLU grammar)
# ---------------------------------------------------------------------------


class TestIntentDraft:
    """Encode/decode round-trip for the IntentDraft dataclass."""

    def test_from_api_dict_full(self) -> None:
        """All fields decoded from a server-shaped payload."""
        intent = IntentDraft.from_api_dict(
            {
                "id": "uuid-1",
                "humanReadableName": "Pause music",
                "formName": "control.pause",
                "sourceText": "root: пауза",
                "positiveTests": "пауза\nстоп",
                "negativeTests": "включи",
                "isActivation": False,
                "status": "NEW",
            }
        )
        assert intent.intent_id == "uuid-1"
        assert intent.form_name == "control.pause"
        assert intent.source_text == "root: пауза"
        assert intent.status == "NEW"

    def test_from_api_dict_missing_id_yields_none(self) -> None:
        """Empty / missing id is normalised to None (locally-built intent)."""
        intent = IntentDraft.from_api_dict({"formName": "x", "id": ""})
        assert intent.intent_id is None

    def test_to_api_dict_includes_id_when_present(self) -> None:
        """to_api_dict injects ``id`` only when intent_id is set (PATCH path)."""
        with_id = IntentDraft(form_name="x", intent_id="abc").to_api_dict()
        assert with_id["id"] == "abc"
        without_id = IntentDraft(form_name="x").to_api_dict()
        assert "id" not in without_id


class TestIntentCRUD:
    """list / get / create / update / delete / set_intents on DialogsSkillCreator."""

    @pytest.mark.asyncio
    async def test_list_intents_fans_out_for_full_payload(self) -> None:
        """list_intents fetches each id's full draft so form_name / source survive.

        Yandex's bulk listing endpoint omits ``formName`` and ``sourceText``;
        only ``id`` / ``humanReadableName`` / ``status`` / ``isActivation``
        come back. ``set_intents`` matches existing entries by ``form_name``
        so the listing alone is unusable for diff/upsert. ``list_intents``
        therefore fans out per-id GETs in parallel and returns IntentDrafts
        carrying the full payload.
        """
        session = _make_session()

        def _ctx(resp: AsyncMock) -> MagicMock:
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=resp)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        listing_resp = _mock_response(
            status=200,
            body_json={
                "result": [
                    {"id": "u1", "humanReadableName": "Play"},
                    {"id": "u2", "humanReadableName": "Pause"},
                ]
            },
        )
        full_u1 = _mock_response(
            status=200,
            body_json={
                "result": {
                    "id": "u1",
                    "formName": "play.specific",
                    "humanReadableName": "Play",
                    "sourceText": "root: x",
                }
            },
        )
        full_u2 = _mock_response(
            status=200,
            body_json={
                "result": {"id": "u2", "formName": "control.pause", "humanReadableName": "Pause"}
            },
        )
        session.get.side_effect = [_ctx(listing_resp), _ctx(full_u1), _ctx(full_u2)]

        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intents = await creator.list_intents("csrf", "skill-1")
        assert [i.form_name for i in intents] == ["play.specific", "control.pause"]
        # 1 listing GET + 2 per-id GETs.
        assert session.get.call_count == 3
        listing_url = session.get.call_args_list[0].args[0]
        assert "channel=aliceSkill" in listing_url
        assert "/apps/skill-1/intents/drafts" in listing_url
        per_id_urls = [c.args[0] for c in session.get.call_args_list[1:]]
        assert all("/intents/drafts/u" in u for u in per_id_urls)

    @pytest.mark.asyncio
    async def test_list_intents_empty_listing_skips_fanout(self) -> None:
        """Empty listing returns [] without firing per-id GETs."""
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=200, body_json={"result": []}))
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intents = await creator.list_intents("csrf", "skill-1")
        assert intents == []
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_get_intent_decodes_single(self) -> None:
        """GET /apps/{id}/intents/drafts/{intent_id} returns IntentDraft."""
        session = _make_session()
        _install_ctx(
            session.get,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "id": "u1",
                        "formName": "play.specific",
                        "humanReadableName": "Play",
                        "sourceText": "root: x",
                        "positiveTests": "p",
                        "negativeTests": "n",
                        "isActivation": False,
                        "status": "NEW",
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intent = await creator.get_intent("csrf", "skill-1", "u1")
        assert intent.intent_id == "u1"
        assert intent.form_name == "play.specific"
        assert intent.human_readable_name == "Play"

    @pytest.mark.asyncio
    async def test_create_intent_returns_server_id(self) -> None:
        """POST /apps/{id}/intents/draft with empty body returns server-assigned UUID."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "id": "fresh-uuid",
                        "humanReadableName": "",
                        "formName": "",
                        "sourceText": "",
                        "positiveTests": "",
                        "negativeTests": "",
                        "isActivation": False,
                        "status": "NEW",
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intent = await creator.create_intent("csrf", "skill-1")
        assert intent.intent_id == "fresh-uuid"
        # Empty POST body
        method, url = session.request.call_args.args[:2]
        assert method == "POST"
        assert "/intents/draft" in url
        assert session.request.call_args.kwargs["json"] == {}

    @pytest.mark.asyncio
    async def test_create_intent_missing_id_raises(self) -> None:
        """A 200 response without id → DialogsApiError (protocol break)."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(status=200, body_json={"result": {}}),
        )
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        with pytest.raises(DialogsApiError):
            await creator.create_intent("csrf", "skill-1")

    @pytest.mark.asyncio
    async def test_update_intent_happy_path_returns_saved(self) -> None:
        """PATCH with valid grammar returns the saved IntentDraft (no validationError)."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "intent": {
                            "id": "u1",
                            "humanReadableName": "Pause",
                            "formName": "control.pause",
                            "sourceText": "root: пауза",
                            "positiveTests": "пауза",
                            "negativeTests": "",
                            "isActivation": False,
                            "status": "NEW",
                        }
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intent = IntentDraft(
            form_name="control.pause",
            human_readable_name="Pause",
            source_text="root: пауза",
            positive_tests="пауза",
            intent_id="u1",
        )
        saved = await creator.update_intent("csrf", "skill-1", intent)
        assert saved.status == "NEW"
        # PATCH body carried our payload
        method, url = session.request.call_args.args[:2]
        assert method == "PATCH"
        assert "/intents/u1/draft" in url
        body = session.request.call_args.kwargs["json"]
        assert body["sourceText"] == "root: пауза"
        assert body["formName"] == "control.pause"

    @pytest.mark.asyncio
    async def test_update_intent_invalid_grammar_raises_typed_error(self) -> None:
        """Server-side grammar errors surface as DialogsIntentValidationError."""
        session = _make_session()
        _install_ctx(
            session.request,
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "intent": {
                            "id": "u1",
                            "formName": "control.pause",
                            "humanReadableName": "P",
                            "sourceText": "broken",
                            "positiveTests": "",
                            "negativeTests": "",
                            "isActivation": False,
                            "status": "INVALID_GRAMMAR",
                        },
                        "validationError": {
                            "errorCode": "VALIDATION_ERROR",
                            "errorBounds": {
                                "charCount": 7,
                                "charOffset": 0,
                                "lineNumber": 1,
                            },
                            "text": 'Неизвестный элемент "broken"',
                        },
                    }
                },
            ),
        )
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intent = IntentDraft(form_name="control.pause", source_text="broken", intent_id="u1")
        with pytest.raises(DialogsIntentValidationError) as exc_info:
            await creator.update_intent("csrf", "skill-1", intent)
        assert exc_info.value.error_code == "VALIDATION_ERROR"
        assert exc_info.value.line_number == 1
        assert exc_info.value.intent_id == "u1"
        assert exc_info.value.form_name == "control.pause"
        # Stringified form surfaces form_name + position so logs at the
        # orchestrator level (which print str(exc)) point at the
        # offending intent without callers having to introspect.
        msg = str(exc_info.value)
        assert "[control.pause]" in msg
        assert "line=1" in msg

    @pytest.mark.asyncio
    async def test_update_intent_without_id_raises(self) -> None:
        """update_intent on a locally-built intent (no intent_id) is a programmer error."""
        session = _make_session()
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        intent = IntentDraft(form_name="control.pause")  # no intent_id
        with pytest.raises(DialogsApiError):
            await creator.update_intent("csrf", "skill-1", intent)

    @pytest.mark.asyncio
    async def test_delete_intent_omits_channel_param(self) -> None:
        """DELETE /apps/{id}/intents/{intent_id}/draft does NOT carry ?channel."""
        session = _make_session()
        session.delete = MagicMock()
        _install_ctx(session.delete, _mock_response(status=200, body_text="{}"))
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        await creator.delete_intent("csrf", "skill-1", "u1")
        url = session.delete.call_args.args[0]
        assert "/apps/skill-1/intents/u1/draft" in url
        assert "channel=" not in url

    @pytest.mark.asyncio
    async def test_set_intents_creates_new_and_patches_changed(self) -> None:
        """set_intents POSTs new ones, PATCHes changed ones, leaves identical alone."""
        session = _make_session()
        # Sequence:
        # 1. list_intents → returns one existing intent (formName='control.pause')
        # 2. POST create → new intent shell for 'play.specific'
        # 3. PATCH the new intent with our content
        # No PATCH for control.pause (our copy is identical to server's).
        session.get = MagicMock()
        session.request = MagicMock()
        # GET responses for list_intents:
        # 1) bulk listing returns id + status, no formName/sourceText
        # 2) per-id GET on drafts/{intent_id} returns full payload
        list_resp = _mock_response(
            status=200,
            body_json={
                "result": [
                    {
                        "id": "existing-uuid",
                        "humanReadableName": "Pause",
                        "isActivation": False,
                        "status": "NEW",
                    }
                ]
            },
        )
        full_resp = _mock_response(
            status=200,
            body_json={
                "result": {
                    "id": "existing-uuid",
                    "formName": "control.pause",
                    "humanReadableName": "Pause",
                    "sourceText": "root: пауза",
                    "positiveTests": "",
                    "negativeTests": "",
                    "isActivation": False,
                    "status": "NEW",
                }
            },
        )

        def _ctx(resp: AsyncMock) -> MagicMock:
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=resp)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session.get.side_effect = [_ctx(list_resp), _ctx(full_resp)]
        # POST + PATCH chained — return values per call.
        responses = [
            # POST create → returns new shell with id
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "id": "new-uuid",
                        "formName": "",
                        "humanReadableName": "",
                        "sourceText": "",
                        "positiveTests": "",
                        "negativeTests": "",
                        "isActivation": False,
                        "status": "NEW",
                    }
                },
            ),
            # PATCH update → returns saved intent
            _mock_response(
                status=200,
                body_json={
                    "result": {
                        "intent": {
                            "id": "new-uuid",
                            "formName": "play.specific",
                            "humanReadableName": "Play",
                            "sourceText": "root: включи",
                            "positiveTests": "",
                            "negativeTests": "",
                            "isActivation": False,
                            "status": "NEW",
                        }
                    }
                },
            ),
        ]
        request_ctx = MagicMock()
        request_ctx.__aenter__ = AsyncMock(side_effect=responses)
        request_ctx.__aexit__ = AsyncMock(return_value=False)
        session.request.return_value = request_ctx
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        desired = [
            IntentDraft(
                form_name="control.pause",
                human_readable_name="Pause",
                source_text="root: пауза",
            ),  # identical to server → no PATCH
            IntentDraft(
                form_name="play.specific",
                human_readable_name="Play",
                source_text="root: включи",
            ),  # new → POST + PATCH
        ]
        result = await creator.set_intents("csrf", "skill-1", desired, delete_missing=False)
        # Two intents end-state.
        assert len(result) == 2
        assert {i.form_name for i in result} == {"control.pause", "play.specific"}
        # Two requests: POST create, PATCH update. NO PATCH for control.pause.
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_set_intents_deletes_missing_when_flag_set(self) -> None:
        """delete_missing=True removes server intents whose form_name is not in desired."""
        session = _make_session()
        session.get = MagicMock()
        session.delete = MagicMock()
        # Server has one intent; we pass empty desired → it gets deleted.
        # list_intents fans out: bulk listing → per-id GET for full payload.
        list_resp = _mock_response(
            status=200,
            body_json={
                "result": [
                    {
                        "id": "stale-uuid",
                        "humanReadableName": "Old",
                        "isActivation": False,
                        "status": "NEW",
                    }
                ]
            },
        )
        full_resp = _mock_response(
            status=200,
            body_json={
                "result": {
                    "id": "stale-uuid",
                    "formName": "old.intent",
                    "humanReadableName": "Old",
                    "sourceText": "",
                    "positiveTests": "",
                    "negativeTests": "",
                    "isActivation": False,
                    "status": "NEW",
                }
            },
        )

        def _ctx(resp: AsyncMock) -> MagicMock:
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=resp)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session.get.side_effect = [_ctx(list_resp), _ctx(full_resp)]
        _install_ctx(session.delete, _mock_response(status=200, body_text="{}"))
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        result = await creator.set_intents("csrf", "skill-1", [], delete_missing=True)
        assert result == []
        # DELETE was called once for the stale intent.
        assert session.delete.call_count == 1
        url = session.delete.call_args.args[0]
        assert "/intents/stale-uuid/draft" in url


# ---------------------------------------------------------------------------
# parse_error_body — Spring + domain validation + HTML auth + plain JSON
# ---------------------------------------------------------------------------


class TestParseErrorBody:
    """Multi-format Yandex error body parser."""

    def test_html_403_returns_auth_error(self) -> None:
        """HTML 403 ('<!DOCTYPE html>...Forbidden') → DialogsAuthError."""
        body = (
            "<!DOCTYPE html>\n<html>\n<head><title>Error</title></head>\n"
            "<body><pre>Forbidden</pre></body>\n</html>"
        )
        err = parse_error_body(body, http_status=403, step="create_app")
        assert isinstance(err, DialogsAuthError)
        assert err.http_status == 403
        assert err.step == "create_app"

    def test_html_401_returns_auth_error(self) -> None:
        """HTML 401 → DialogsAuthError too."""

        err = parse_error_body("<html>Unauthorized</html>", http_status=401, step="fetch_csrf")
        assert isinstance(err, DialogsAuthError)

    def test_spring_skill_not_found_returns_typed(self) -> None:
        """Spring servlet 404 'Skill not found with id: <uuid>' → DialogsSkillNotFoundError."""

        body = (
            '{"servlet":"dispatcherServlet",'
            '"message":"Skill not found with id: abc12345-dead-beef-cafe-000000000001",'
            '"url":"/api/dev-console/v1/apps/abc12345-dead-beef-cafe-000000000001",'
            '"status":"404"}'
        )
        err = parse_error_body(body, http_status=404, step="get_skill")
        assert isinstance(err, DialogsSkillNotFoundError)
        assert err.skill_id == "abc12345-dead-beef-cafe-000000000001"
        assert err.yandex_error is not None
        assert "Skill not found" in err.yandex_error

    def test_spring_4xx_returns_validation_error(self) -> None:
        """Spring servlet 4xx (no skill-not-found marker) → DialogsValidationError."""

        body = (
            '{"servlet":"dispatcherServlet",'
            '"message":"Required parameter \\u0027channel\\u0027 is not present.",'
            '"url":"/api/dev-console/v1/apps/x/intents",'
            '"status":"400"}'
        )
        err = parse_error_body(body, http_status=400, step="get_intents")
        assert isinstance(err, DialogsValidationError)
        assert err.http_status == 400
        assert "channel" in (err.yandex_error or "")

    def test_domain_validation_error_with_fields(self) -> None:
        """Domain shape {error:{message, code, fields:{...}}} → DialogsValidationError + fields."""

        body = (
            '{"error":{"message":"Validation error","code":400,'
            '"fields":{"name":"Название должно содержать минимум два слова"}}}'
        )
        err = parse_error_body(body, http_status=400, step="update_draft")
        assert isinstance(err, DialogsValidationError)
        assert err.fields == {"name": "Название должно содержать минимум два слова"}
        assert err.yandex_error == "Validation error"

    def test_top_level_error_string(self) -> None:
        """Plain {error: '...'} → DialogsApiError with yandex_error populated."""

        err = parse_error_body('{"error":"Something bad"}', http_status=500, step="some_step")
        assert isinstance(err, DialogsApiError)
        assert not isinstance(err, DialogsValidationError | DialogsAuthError)
        assert err.yandex_error == "Something bad"

    def test_unknown_body_returns_generic(self) -> None:
        """Body that isn't recognized → generic DialogsApiError with raw preview."""

        err = parse_error_body("not json at all", http_status=500, step="x")
        assert type(err) is DialogsApiError
        assert err.http_status == 500

    def test_empty_body_401_returns_auth_error(self) -> None:
        """HTTP 401 with empty body → DialogsAuthError (per HTTP semantics)."""

        err = parse_error_body("", http_status=401, step="update_draft")
        assert isinstance(err, DialogsAuthError)
        assert err.http_status == 401

    def test_json_body_401_returns_auth_error(self) -> None:
        """HTTP 401 with JSON body → DialogsAuthError, not generic."""

        err = parse_error_body('{"error":"unauthorized"}', http_status=401, step="x")
        assert isinstance(err, DialogsAuthError)
        assert err.http_status == 401

    def test_non_html_403_falls_through(self) -> None:
        """HTTP 403 with non-HTML JSON body parses through to body-shape rules.

        Yandex uses 403 for both auth-walls (HTML) and domain-level "forbidden"
        (JSON), so non-HTML 403 must not be auto-classified as auth.
        """
        body = '{"error":{"message":"Access denied","code":403}}'
        err = parse_error_body(body, http_status=403, step="x")
        assert not isinstance(err, DialogsAuthError)


# ---------------------------------------------------------------------------
# fetch_csrf — auth signals
# ---------------------------------------------------------------------------


class TestFetchCsrfAuthSignals:
    """fetch_csrf disables redirects and surfaces 30x as DialogsAuthError."""

    @pytest.mark.asyncio
    async def test_redirect_to_passport_raises_auth_error(self) -> None:
        """30x (anonymous request) → DialogsAuthError, regex never inspected."""
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=302, body_text=""))
        creator = DialogsSkillCreator(session)
        with pytest.raises(DialogsAuthError) as exc_info:
            await creator.fetch_csrf()
        assert exc_info.value.http_status == 302
        # Confirm we passed allow_redirects=False
        session.get.assert_called_once_with(DIALOGS_DEV_HTML_URL, allow_redirects=False)

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self) -> None:
        """401 → DialogsAuthError (was DialogsApiError in 1.0 — now typed)."""
        session = _make_session()
        _install_ctx(session.get, _mock_response(status=401, body_text=""))
        creator = DialogsSkillCreator(session)
        with pytest.raises(DialogsAuthError) as exc_info:
            await creator.fetch_csrf()
        assert exc_info.value.http_status == 401


# ---------------------------------------------------------------------------
# OAuth argument validation in auto_create_skill
# ---------------------------------------------------------------------------


class TestAutoCreateSkillOAuthValidation:
    """auto_create_skill validates OAuth params per channel and combination."""

    @pytest.mark.asyncio
    async def test_smart_home_without_oauth_raises_value_error(self) -> None:
        """channel='smartHome' without OAuth params → ValueError before any network."""
        with pytest.raises(ValueError, match=r"smartHome.*requires OAuth"):
            await auto_create_skill(
                authenticator=_fake_authenticator(),
                skill_name="x",
                artifacts=SkillCreationArtifacts(),
                backend_uri=_TEST_BACKEND_URI,
                channel=SMART_HOME_CHANNEL,
                logo_bytes=b"\x89PNG",
                creator_factory=lambda _s: _make_creator_mock(),
            )

    @pytest.mark.asyncio
    async def test_partial_oauth_args_raises_value_error(self) -> None:
        """Only some oauth_* set → ValueError (programmer error)."""
        with pytest.raises(ValueError, match=r"OAuth params are partial"):
            await auto_create_skill(
                authenticator=_fake_authenticator(),
                skill_name="x",
                artifacts=SkillCreationArtifacts(),
                backend_uri=_TEST_BACKEND_URI,
                channel=DIALOG_CHANNEL,
                description=_TEST_DIALOG_DESCRIPTION,
                # only one of four OAuth params
                oauth_authorize_url=_TEST_AUTH_AUTHORIZE_URL,
                logo_bytes=b"\x89PNG",
                creator_factory=lambda _s: _make_creator_mock(),
            )

    @pytest.mark.asyncio
    async def test_dialog_with_full_oauth_attaches_oauth(self) -> None:
        """channel='aliceSkill' + full OAuth tuple → OAuth steps run."""
        creator = _make_creator_mock()
        result = await auto_create_skill(
            authenticator=_fake_authenticator(),
            skill_name="Test Dialog Skill",
            artifacts=SkillCreationArtifacts(),
            backend_uri=_TEST_DIALOG_BACKEND_URI,
            channel=DIALOG_CHANNEL,
            description=_TEST_DIALOG_DESCRIPTION,
            oauth_authorize_url=_TEST_AUTH_AUTHORIZE_URL,
            oauth_token_url=_TEST_AUTH_TOKEN_URL,
            oauth_client_id=_TEST_OAUTH_CLIENT_ID,
            oauth_client_secret=_TEST_OAUTH_CLIENT_SECRET,
            logo_bytes=b"\x89PNG",
            creator_factory=lambda _s: creator,
        )
        assert result.state == SkillCreationState.DONE
        assert result.oauth_app_id == "oauth-app-id-789"
        creator.create_oauth_app.assert_awaited_once()
        creator.attach_oauth.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dialog_default_logo_loaded_on_none(self) -> None:
        """logo_bytes=None for channel='aliceSkill' triggers load_default_logo_bytes."""
        creator = _make_creator_mock()
        await auto_create_skill(
            authenticator=_fake_authenticator(),
            skill_name="Test Dialog Skill",
            artifacts=SkillCreationArtifacts(),
            backend_uri=_TEST_DIALOG_BACKEND_URI,
            channel=DIALOG_CHANNEL,
            description=_TEST_DIALOG_DESCRIPTION,
            logo_bytes=None,
            creator_factory=lambda _s: creator,
        )
        upload_call = creator.upload_logo.await_args
        assert upload_call is not None
        png_arg = (
            upload_call.args[2] if len(upload_call.args) >= 3 else upload_call.kwargs.get("png")
        )
        assert isinstance(png_arg, bytes)
        # PNG magic header
        assert png_arg[:8] == bytes.fromhex("89504e470d0a1a0a")
