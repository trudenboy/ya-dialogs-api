# Changelog

All notable changes to this project will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0rc1] — 2026-05-06

### Added

- Initial release-candidate. Yandex Dialogs Developer API client extracted from
  `ma-provider-yandex-smarthome/provider/auto_skill.py` and made framework-agnostic
  via the `WebserverAdapter` Protocol.
- `auto_create_skill(...)` — full pipeline (Device Flow OAuth → CSRF/cookie session →
  POST `/apps` → POST `/skills/{id}/draft` → publication polling → callback URL extraction).
  Handles both `skill_type="smart_home"` and `skill_type="dialog"` channels.
- `auto_rename_dialog_skill(...)` — patches a dialog skill draft and re-deploys.
- `SkillCreationArtifacts` / `SkillCreationState` — incremental state machine; callers
  persist between calls so transient failures resume from the last completed step.
- `WebserverAdapter` Protocol — caller provides their own HTTP route registration
  surface for the Device Code activation page. Reference adapters can be built
  against aiohttp, FastAPI, Starlette, Music Assistant's webserver, etc.
- `load_default_logo_bytes()` — loads the bundled default skill logo asset.
- Re-exports `SecretStr` for token redaction in repr/logs.
