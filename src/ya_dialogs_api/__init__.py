r"""ya-dialogs-api — Yandex Dialogs Developer API client.

Programmatic skill creation, draft management, and publication against the
``dialogs.yandex.ru/developer-api/v2/`` endpoints. Framework-agnostic: the
caller produces an authorized ``aiohttp.ClientSession`` (typically via
``ya-passport-auth.PassportClient.login_device_code``) and hands it to the
library through a context-manager factory.

Public API:

- :func:`auto_create_skill` — full pipeline; ``channel="smartHome"`` requires
  OAuth (account-linking), ``channel="aliceSkill"`` accepts OAuth-free or
  OAuth-attached. Resumable via :class:`SkillCreationArtifacts`.
- :func:`auto_update_skill` — patch an existing skill draft and re-deploy.
  Works for both channels.
- :class:`DialogsSkillCreator` — low-level dev-console client (one method
  per pipeline step). Use this if you need finer control than
  :func:`auto_create_skill` provides — includes ``delete_skill``.
- :data:`Channel` — ``Literal["smartHome", "aliceSkill"]`` (Yandex API wire values).
- Payload builders: :func:`build_smart_home_draft_payload`,
  :func:`build_dialog_draft_payload`, :func:`build_oauth_app_payload`.
- State machine: :class:`SkillCreationArtifacts`, :class:`SkillCreationState`,
  :func:`dump_artifacts`, :func:`load_artifacts`.
- Default logo asset: :func:`load_default_logo_bytes`.
- Typed errors: :class:`DialogsApiError` (base), :class:`DialogsAuthError`,
  :class:`DialogsCsrfError`, :class:`DialogsValidationError`,
  :class:`DialogsSkillNotFoundError`, :class:`DialogsDuplicateSkillError`.
- :class:`SecretStr` re-exported from :mod:`ya_passport_auth` for token
  redaction in repr/format/tracebacks.
"""

from __future__ import annotations

from ya_passport_auth import SecretStr

from .api_client import (
    DEVICE_FLOW_TIMEOUT_SECONDS,
    DIALOG_CHANNEL,
    DIALOGS_API_BASE,
    DIALOGS_CSRF_REGEX,
    DIALOGS_DEV_BASE,
    DIALOGS_DEV_HTML_URL,
    SMART_HOME_CHANNEL,
    AuthenticatorCM,
    Channel,
    DialogsApiError,
    DialogsAuthError,
    DialogsCsrfError,
    DialogsDuplicateSkillError,
    DialogsEntitiesValidationError,
    DialogsIntentValidationError,
    DialogsSkillCreator,
    DialogsSkillNotFoundError,
    DialogsValidationError,
    EntityDraft,
    EntityValue,
    IntentDraft,
    SlotDeclaration,
    auto_create_skill,
    auto_update_skill,
    build_dialog_draft_payload,
    build_oauth_app_payload,
    build_smart_home_draft_payload,
    load_default_logo_bytes,
)
from .manifest import (
    SUPPORTED_SCHEMA_VERSION,
    ManifestEntities,
    ManifestIntent,
    SkillManifest,
    SkillManifestError,
    entities_to_drafts,
    intent_to_draft,
    parse_manifest,
    parse_manifest_text,
)
from .nlu import IntentMatch, iter_intent_matches
from .state import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)

__all__ = [
    "DEVICE_FLOW_TIMEOUT_SECONDS",
    "DIALOGS_API_BASE",
    "DIALOGS_CSRF_REGEX",
    "DIALOGS_DEV_BASE",
    "DIALOGS_DEV_HTML_URL",
    "DIALOG_CHANNEL",
    "SMART_HOME_CHANNEL",
    "SUPPORTED_SCHEMA_VERSION",
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
    "IntentMatch",
    "ManifestEntities",
    "ManifestIntent",
    "SecretStr",
    "SkillCreationArtifacts",
    "SkillCreationState",
    "SkillManifest",
    "SkillManifestError",
    "SlotDeclaration",
    "auto_create_skill",
    "auto_update_skill",
    "build_dialog_draft_payload",
    "build_oauth_app_payload",
    "build_smart_home_draft_payload",
    "dump_artifacts",
    "entities_to_drafts",
    "intent_to_draft",
    "iter_intent_matches",
    "load_artifacts",
    "load_default_logo_bytes",
    "parse_manifest",
    "parse_manifest_text",
]
