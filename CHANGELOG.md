# Changelog

All notable changes to this project will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-05-06

This release combines an `aliceSkill` OAuth-free pipeline (originally drafted
as a separate `auto_create_dialog_skill` function), a public-API rename to
match Yandex wire values (`channel`), and the P0 quick-wins from the
`RESEARCH.md` audit. Empirically verified against the live developer console
on 2026-05-06: `request_deploy` succeeds for `channel="aliceSkill"` skills
without an attached OAuth application.

### Breaking changes

- **`channel` param replaces `skill_type`.** The Yandex API uses wire values
  `"smartHome"` and `"aliceSkill"`. The library now accepts the same strings
  directly:
    - `auto_create_skill(skill_type="smart_home", ...)` → `auto_create_skill(channel="smartHome", ...)`
    - `auto_create_skill(skill_type="dialog", ...)` → `auto_create_skill(channel="aliceSkill", ...)`
  Type alias `SkillType` is removed; use `Channel = Literal["smartHome", "aliceSkill"]` instead.
- **`dialog_*` parameters dropped.** `dialog_description`, `dialog_structured_examples`,
  `dialog_activation_phrases`, `dialog_category`, `dialog_voice` are now
  `description`, `structured_examples`, `activation_phrases`, `category`, `voice`.
  The fields apply only when `channel="aliceSkill"`; passing them with
  `channel="smartHome"` is silently ignored (not used by the smart_home payload).
- **OAuth params now optional.** `oauth_authorize_url`, `oauth_token_url`,
  `oauth_client_id`, `oauth_client_secret` are now `str | None = None`.
  Validation rules (raise `ValueError`):
    - `channel="smartHome"` requires all four (Smart Home skills always
      need account-linking).
    - `channel="aliceSkill"` accepts both — provide all four to attach an
      OAuth app, or omit them all to skip account-linking entirely.
    - Mixed (some set, some not) is a programmer error.
- **`logo_bytes` now defaults to `None`** in `auto_create_skill`, falling
  back to `load_default_logo_bytes()`. Previously required.
- The `auto_create_skill` pipeline raises `ValueError` (synchronously) on
  invalid OAuth-arg combinations rather than returning `state=FAILED`.

### Added

- **`Channel` type alias** — `Literal["smartHome", "aliceSkill"]`, matching
  Yandex API wire values exactly. Use directly or via the
  `SMART_HOME_CHANNEL` / `DIALOG_CHANNEL` constants.
- **`DialogsSkillCreator.delete_skill(csrf, skill_id)`** —
  `DELETE /apps/{id}?channel=...`. Verified empirically: returns HTTP 200
  with empty body on success. Note: orphaned OAuth apps are not removed.
- **Typed error hierarchy** (replaces ad-hoc message-substring checks):
    - `DialogsAuthError` — HTTP 401/403, HTML auth-wall body, or 30x
      redirect from `GET /developer` (anonymous request).
    - `DialogsValidationError(message, fields)` — domain-level field
      validation rejection (`{error: {message, code, fields}}` shape) **and**
      Spring `dispatcherServlet` 4xx errors.
    - `DialogsSkillNotFoundError(skill_id)` — Spring 404 with
      `"Skill not found with id: <uuid>"`. The UUID is parsed out into the
      `skill_id` attribute.
    - `parse_error_body(body, *, http_status, step)` — best-effort parser
      returning the most specific exception type derivable from a 4xx/5xx
      body across the four formats Yandex emits.
- **`description` validation.** `auto_create_skill(channel="aliceSkill", ...)`
  and `auto_rename_dialog_skill` reject empty or whitespace-only descriptions
  (`description.strip() == ""`) before any network call, returning
  `state=FAILED` with a clear `last_error`.
- 28 new tests covering: optional OAuth combinations, `description.strip()`,
  the four error-body shapes, `delete_skill` channel routing, `fetch_csrf`
  redirect handling, typed errors. **75 tests total** (up from 47); coverage
  85 % (`errors.py` 88 %, `state.py` 100 %, `api_client.py` 83 %).

### Changed

- **`fetch_csrf` no longer follows redirects.** An anonymous request to
  `GET /developer` results in a 30x to `passport.yandex.ru`; previously the
  default redirect-follower would render Passport's HTML at the regex,
  producing a confusing `DialogsCsrfError`. Now surfaces as
  `DialogsAuthError(http_status=302)` immediately.
- **Pipeline step helpers extracted** (`_step_create_app`,
  `_step_upload_logo_and_update_draft`, `_step_create_oauth_app`,
  `_step_attach_oauth`, `_step_checkpoint_deploy_requested`,
  `_step_request_deploy`). `auto_create_skill` is a thin orchestrator that
  composes these helpers and skips OAuth steps when `oauth_*` params are
  not provided.
- The CSRF-fetch + recovery harness (`_run_with_recovery`, renamed from
  `_run_pipeline_with_recovery`) is now generalised over a `_PipelineExecutor`
  callable, decoupling it from any specific channel/OAuth shape.

### Removed

- **`auto_create_dialog_skill`** — superseded by
  `auto_create_skill(channel="aliceSkill", ...)`. The OAuth-free pipeline
  is selected automatically when no `oauth_*` params are passed for an
  `aliceSkill` skill.
- **`SkillType`** literal alias — use `Channel` instead.

## [1.0.0] — 2026-05-06

### Added

Initial public release. Async Yandex Dialogs Developer API client extracted
from `ma-provider-yandex-smarthome/provider/auto_skill.py` and made
framework-agnostic.

**Public API:**

- `auto_create_skill(...)` — full skill-creation pipeline (CSRF/cookie session
  → POST `/apps` → upload logo → PATCH draft → POST OAuth app → attach OAuth
  → request deploy). Handles both `skill_type="smart_home"` and
  `skill_type="dialog"` channels. Resumable via `SkillCreationArtifacts`.
- `auto_rename_dialog_skill(...)` — patches a dialog skill draft name and
  re-deploys.
- `DialogsSkillCreator` — low-level dev-console client (one method per
  pipeline step).
- `SkillCreationArtifacts` / `SkillCreationState` — incremental state
  machine; callers persist between calls so transient failures resume from
  the last completed step.
- Payload builders: `build_smart_home_draft_payload`,
  `build_dialog_draft_payload`, `build_oauth_app_payload`.
- `load_default_logo_bytes()` — bundled fallback skill logo.
- `SecretStr` re-exported from `ya-passport-auth` for token redaction.
- Typed errors: `DialogsApiError`, `DialogsCsrfError`,
  `DialogsDuplicateSkillError`.

**Architecture:**

The library is **framework-agnostic**. Authentication is the caller's
responsibility — pass a no-arg async-context-manager factory (`AuthenticatorCM`)
that yields an authenticated `aiohttp.ClientSession`. Typically the caller
wraps `ya_passport_auth.PassportClient.login_device_code` and any UX surface
they want around the Device Flow user-code prompt (CLI, web page, Telegram,
Music Assistant config-flow, etc.).

All URLs are pre-computed by the caller (`backend_uri`, `oauth_authorize_url`,
`oauth_token_url`, `oauth_client_id`, `oauth_client_secret`) — the lib has
no opinion on connection-type or hosting.

Failures are values, not exceptions: pipeline errors land as
`artifacts.state=FAILED` with `last_error` set. Truly unexpected exceptions
still propagate.

**Testing:**

- 47 tests across `DialogsSkillCreator` methods, payload builders,
  state-machine serialisation, orchestrator happy paths, resume from each
  state, and failure recovery.
- `mypy --strict` clean.
- 86% coverage (state.py 100%, api_client.py 83%).

**Runtime dependencies:**

- `aiohttp >= 3.10, < 4`
- `ya-passport-auth >= 1.3.0`
