# Changelog

All notable changes to this project will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.3.0] — 2026-05-09

### Added

- **`ya_dialogs_api.manifest` — declarative skill description format.**
  A new TOML-based manifest schema lets clients ship a Yandex skill's
  intents and custom entities as pure data instead of hard-coded
  Python. Each intent's ``grammar`` field carries the Granet
  ``sourceText`` byte-for-byte, so it is copy-paste-compatible with
  the dev-console editor at
  ``https://dialogs.yandex.ru/developer/skills/<id>/draft/settings/intents``.
  Field names mirror Yandex API names in snake_case (``form_name`` ↔
  ``formName``, ``human_readable_name`` ↔ ``humanReadableName``, etc.)
  so the cognitive load matches the dev-console UI.

  Public API:

  - ``SkillManifest`` / ``ManifestIntent`` / ``ManifestEntities`` —
    immutable parsed view.
  - ``parse_manifest(raw_dict)`` / ``parse_manifest_text(toml_str)`` —
    loaders with structural validation (schema_version,
    duplicate-form-name detection, required field checks).
  - ``SkillManifest.to_intent_drafts()`` /
    ``SkillManifest.to_entity_drafts()`` — feed the result of
    ``set_intents`` / ``set_entities`` directly.
  - ``intent_to_draft(intent)`` — single-intent conversion helper.
  - ``entities_to_drafts(text)`` — Granet ``customEntities`` DSL
    parser; round-trips with ``EntityDraft.to_dsl()``.
  - ``SkillManifestError`` — typed validation error with concrete
    field path in the message.
  - ``SUPPORTED_SCHEMA_VERSION`` — current schema version (``1``);
    loader rejects newer ``schema_version`` so a forward-compat
    user override doesn't get silently parsed.

  The module is intentionally agnostic about runtime semantics
  (i.e. how a client maps a matched intent to its own command
  dataclass). That belongs to the calling application.

- **`ya_dialogs_api.nlu` — typed accessors over the webhook payload.**
  Encapsulates the *shape* of ``request.nlu.intents.<form_name>``:

  - ``IntentMatch(form_name, payload)`` — one matched intent with
    typed slot accessors: ``slot_int(name) -> int | None``,
    ``slot_str(name) -> str | None``, ``slot_value(name) -> Any``,
    ``raw_slot(name) -> Mapping | None``, ``has_slot(name) -> bool``.
    Numeric accessor coerces ``int`` and ``float`` (Yandex sends
    either depending on the spoken number); booleans are explicitly
    rejected because ``isinstance(True, int)`` is true.
  - ``iter_intent_matches(nlu_intents)`` — yield one ``IntentMatch``
    per ``form_name`` in dict insertion order. Tolerates ``None`` /
    non-mapping inputs without raising.

  This is the layer that absorbs Yandex format changes — if Yandex
  renames ``slots[name].value`` or moves it under a different key,
  only this module changes; downstream providers keep the same
  calling pattern.

### Added

- **`SlotDeclaration` + `IntentDraft.slots`** — typed authoring of
  Yandex custom-intent slots. Pass a tuple of `SlotDeclaration(name,
  type, source)` to `IntentDraft` and the library composes the
  matching `slots:` block of DSL into `sourceText` automatically at
  serialise time (via the new `IntentDraft.rendered_source_text`
  property). Callers no longer have to hand-roll the block and keep
  it in sync with a separate dataclass — the structured definition is
  the single source of truth. Round-tripping through `from_api_dict`
  keeps the rendered DSL in `source_text` and leaves the structured
  `slots` tuple empty (server-side `sourceText` is authoritative for
  diff comparisons).
- **`set_intents` diffs against `rendered_source_text`** — the server
  always returns the rendered DSL in `sourceText`, so comparing local
  raw `source_text` would treat every slot-bearing intent as drifted
  and force a useless PATCH on every sync. Idempotency is now
  preserved when callers migrate hand-rolled grammars to structured
  `slots`.
- **`EntityDraft` + `EntityValue` + `set_entities` /
  `set_entities_source`** — typed authoring of Yandex custom entities
  for the `aliceSkill` channel. Compose a list of `EntityDraft(name,
  values=(EntityValue(name, phrases), ...))` and call
  `creator.set_entities(csrf, skill_id, entities)`; the library
  renders the Granet DSL and PUTs it via the
  `/apps/{id}/drafts/entities?channel=aliceSkill` endpoint —
  single-shot replace, idempotent server-side. Granet validation
  failures (HTTP 400 with `"Granet grammar validation error."`) are
  surfaced as the new typed `DialogsEntitiesValidationError` (subclass
  of `DialogsValidationError`). The endpoint shape was probed live
  on 2026-05-09 — see `RESEARCH.md` §7.6.
- **`entities` parameter in `auto_create_skill` / `auto_update_skill`**
  — declarative custom-entities sync sits in the same pipeline as the
  existing `intents` parameter, but runs **before** it so intent
  grammars referencing entity types pass Granet validation.
  `entities=None` leaves the server alone; `entities=[]` clears all
  custom entities. Smart-home channel ignores the parameter
  (entities are an `aliceSkill`-only feature).

## [2.1.2] — 2026-05-09

### Fixed

- **`list_intents` now returns full per-intent payload** (form_name +
  source_text + tests). Yandex's bulk listing endpoint
  (`GET /apps/{id}/intents/drafts`) only includes `id`,
  `humanReadableName`, `status` and `isActivation` — `formName` and
  `sourceText` are stripped. Since `set_intents` matches existing
  entries by `form_name`, the listing alone left every server-side
  intent invisible to the diff/upsert protocol: `set_intents` would
  treat each desired entry as new, POST a fresh shell, then PATCH the
  shell with a `formName` that another (already-formNamed) intent had
  reserved → `DialogsApiError: Form name already exists`. `list_intents`
  now fans out to `get_intent` per id (in parallel via `asyncio.gather`)
  so the returned drafts carry the data the diff actually needs. The
  rename / drift-sync flow (`auto_update_skill`) is finally idempotent
  on a populated skill.

## [2.1.1] — 2026-05-08

### Changed

- **`DialogsIntentValidationError.__str__` now includes `form_name` and
  the error position.** Previously the orchestrator's `auto-create
  failed at update_intent: <text>` log line surfaced only the
  human-readable message ("Некорректный аргумент",
  "Неизвестный элемент 'root'", …) which on its own does not identify
  which intent in a multi-grammar `set_intents()` call broke. Errors
  now render as `[<form_name>] <text> (line=N offset=N len=N)
  [<errorCode>]` so a single log line points at the offending intent.
  Typed accessors (`error_code`, `line_number`, `char_offset`,
  `char_count`, `intent_id`, `form_name`) are unchanged.
- **`set_intents` logs per-intent progress** at INFO level: every
  create / patch step prints `[i/N] form_name=… (id=…)` so a failure
  on intent N can be located even without DEBUG enabled. Unchanged
  intents (matched by content) still log at DEBUG only.

### Added

- **DEBUG-level dump of the rejected request payload** in
  `update_intent` when a `validationError` block is returned. Helps
  diagnose payload-shape errors (vs grammar parse errors) by showing
  the exact body Yandex saw alongside the validationError block —
  enable with `--log-level=debug` on the host application or set the
  `ya_dialogs_api.api_client` logger to DEBUG.

## [2.1.0] — 2026-05-07

Adds programmatic management of Yandex Dialogs custom intents (NLU grammar)
on `aliceSkill` skills. Previously, intents could only be created and edited
via the dev-console UI; this release wraps the `/apps/{id}/intents` REST
surface so callers can declare grammars in code and sync them through the
existing `auto_create_skill` / `auto_update_skill` pipelines.

The endpoints and payload shape were derived from a Playwright probe of
the live dev console (`dialogs.yandex.ru`) on 2026-05-07; see
`RESEARCH.md` § 7 for the full network capture.

### Added

- **API:** `IntentDraft` dataclass — typed local representation of a
  custom-intent definition (`form_name`, `human_readable_name`,
  `source_text`, `positive_tests`, `negative_tests`, `is_activation`,
  `intent_id`, `status`). `from_api_dict` / `to_api_dict` for codec
  round-trips against the server payload.
- **API:** `DialogsIntentValidationError` — typed error for synchronous
  grammar-validation failures returned by Yandex (HTTP 200 + a
  `validationError` block inside the result). Carries `error_code`,
  `text`, and the `(char_count, char_offset, line_number)` triple Yandex
  uses to locate the error in `source_text`.
- **API:** Five new methods on `DialogsSkillCreator`:
    - `list_intents(csrf, skill_id)` — `GET /apps/{id}/intents/drafts`
    - `get_intent(csrf, skill_id, intent_id)` — `GET /apps/{id}/intents/drafts/{id}`
    - `create_intent(csrf, skill_id)` — `POST /apps/{id}/intents/draft`
      (empty body — server generates the UUID; pair with `update_intent`)
    - `update_intent(csrf, skill_id, intent: IntentDraft)` —
      `PATCH /apps/{id}/intents/{id}/draft`. Returns the saved intent on
      valid grammar; raises `DialogsIntentValidationError` on validation
      failure. The previously valid version remains effective at runtime.
    - `delete_intent(csrf, skill_id, intent_id)` — `DELETE` (note: this
      single endpoint omits the `?channel` query parameter, verified
      empirically).
- **API:** `DialogsSkillCreator.set_intents(csrf, skill_id, intents,
  delete_missing=True)` — declarative idempotent setter. Diffs the
  desired list against the live server state (matched by `form_name`),
  issuing the minimum POST/PATCH/DELETE sequence. Re-runnable safely.
- **Orchestrators:** `auto_create_skill` and `auto_update_skill` accept
  a new `intents: list[IntentDraft] | None = None` keyword argument.
  `None` leaves whatever's on the server alone; `[]` clears all custom
  intents. Sync runs between the draft update and `request_deploy` so
  intents land in the same moderation cycle as the rest of the draft —
  no second-publish round-trip required.
- **Pipeline:** New internal `_step_set_intents` step in `_execute_pipeline`,
  inserted between OAuth attach (or draft update for OAuth-free skills)
  and the deploy checkpoint. No-op for `channel="smartHome"` (custom
  intents are an `aliceSkill`-only feature).

### Notes

- Intent CRUD is hard-coded to `channel=aliceSkill` regardless of the
  `DialogsSkillCreator`'s configured channel — Yandex's API rejects the
  same routes on the smart-home channel, so we don't expose the choice.
- The `is_activation` field is included in the dataclass and round-tripped
  faithfully, but its server-side semantics (relationship to the legacy
  `activationPhrases` array on the main draft) are not yet documented.

## [2.0.0] — 2026-05-06

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
- **`auto_rename_dialog_skill` replaced by `auto_update_skill(channel=...)`.**
  The old name was misleading — the function patches the full draft, not
  just the name, and works for both channels. Migration:
    - `auto_rename_dialog_skill(new_name=..., description=...)` →
      `auto_update_skill(skill_name=..., channel="aliceSkill", description=...)`
    - For Smart Home: `auto_update_skill(skill_name=..., channel="smartHome")`
      (no `description` needed).

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
- **`auto_update_skill(channel=...)`** — universal draft-update + re-deploy
  for both channels. Empirically verified on 2026-05-06 that
  `PATCH /apps/{id}/draft/update` and
  `POST /apps/{id}/draft/request-deploy?channel=smartHome` succeed for
  Smart Home skills (HTTP 200 on both).
- **`description` validation.** `auto_create_skill(channel="aliceSkill", ...)`
  and `auto_update_skill(channel="aliceSkill", ...)` reject empty or
  whitespace-only descriptions (`description.strip() == ""`) before any
  network call, returning `state=FAILED` with a clear `last_error`.
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
- **`auto_rename_dialog_skill`** — superseded by `auto_update_skill`.
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
