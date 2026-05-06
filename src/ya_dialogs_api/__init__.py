r"""ya-dialogs-api — Yandex Dialogs Developer API client.

Programmatic skill creation, draft management, and publication against the
``dialogs.yandex.ru/developer-api/v2/`` endpoints. Framework-agnostic: the
caller produces an authorized ``aiohttp.ClientSession`` (typically via
``ya-passport-auth.PassportClient.login_device_code``) and hands it to the
library through a context-manager factory.

Public API:

- :func:`auto_create_skill` — full pipeline: create app → upload logo →
  patch draft → create OAuth app → attach → publish. Resumable via
  ``SkillCreationArtifacts``.
- :func:`auto_create_dialog_skill` — OAuth-free dialog (``aliceSkill``)
  pipeline. Use this for custom Alice skills that don't need account-linking.
- :func:`auto_rename_dialog_skill` — patch a dialog skill draft and re-deploy.
- :class:`DialogsSkillCreator` — low-level dev-console client (one method
  per pipeline step). Use this if you need finer control than
  :func:`auto_create_skill` provides.
- :data:`SkillType` — Literal\\["smart_home", "dialog"\\].
- Payload builders: :func:`build_smart_home_draft_payload`,
  :func:`build_dialog_draft_payload`, :func:`build_oauth_app_payload`.
- State machine: :class:`SkillCreationArtifacts`, :class:`SkillCreationState`,
  :func:`dump_artifacts`, :func:`load_artifacts`.
- Default logo asset: :func:`load_default_logo_bytes`.
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
    DialogsApiError,
    DialogsCsrfError,
    DialogsDuplicateSkillError,
    DialogsSkillCreator,
    SkillType,
    auto_create_dialog_skill,
    auto_create_skill,
    auto_rename_dialog_skill,
    build_dialog_draft_payload,
    build_oauth_app_payload,
    build_smart_home_draft_payload,
    load_default_logo_bytes,
)
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
    "AuthenticatorCM",
    "DialogsApiError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsSkillCreator",
    "SecretStr",
    "SkillCreationArtifacts",
    "SkillCreationState",
    "SkillType",
    "auto_create_dialog_skill",
    "auto_create_skill",
    "auto_rename_dialog_skill",
    "build_dialog_draft_payload",
    "build_oauth_app_payload",
    "build_smart_home_draft_payload",
    "dump_artifacts",
    "load_artifacts",
    "load_default_logo_bytes",
]
