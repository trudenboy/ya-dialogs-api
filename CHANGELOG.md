# Changelog

All notable changes to this project will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-05-06

### Added

- `auto_create_dialog_skill(...)` — OAuth-free pipeline for custom Alice
  (``aliceSkill``) skills. Symmetric to `auto_rename_dialog_skill`. Runs only
  the four steps Yandex requires for dialog skills: ``create_app → upload_logo
  → update_draft → request_deploy``. Custom Alice skills don't need an
  attached OAuth application, so callers without an OAuth provider on their
  backend (typical for voice-skill use cases — e.g.
  `ma-provider-yandex-alice`) can use this entry point directly. Like
  `auto_create_skill`, it is resumable via `SkillCreationArtifacts` and
  surfaces all failures as `state=FAILED` with `last_error`.

  Default `logo_bytes=None` resolves to `load_default_logo_bytes()`.

### Changed

- Pipeline step helpers extracted (`_step_create_app`,
  `_step_upload_logo_and_update_draft`, `_step_create_oauth_app`,
  `_step_attach_oauth`, `_step_checkpoint_deploy_requested`,
  `_step_request_deploy`). `auto_create_skill` and `auto_create_dialog_skill`
  are now thin orchestrators that compose these helpers — no behaviour
  change for `auto_create_skill`. The CSRF-fetch + recovery harness
  (`_run_with_recovery`, formerly `_run_pipeline_with_recovery`) was
  generalised to take any pipeline executor.

  No breaking changes to the public API.

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
