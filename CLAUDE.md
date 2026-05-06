# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project Overview

`ya-dialogs-api` is a generic, framework-agnostic Python client for the **Yandex
Dialogs Developer API** — the meta-API at `dialogs.yandex.ru/developer-api/v2/`
that the developer console uses for programmatic skill creation, draft
management, and publication.

This library is unique in its niche: every other Yandex Alice library on PyPI
(aliceio, aioalice, alice_types, dialogic, alice-scripts) handles the *runtime*
side — incoming webhook requests from end users. This one handles the
*provisioning* side — sign in via Yandex Passport, create a skill, upload a
logo, set the webhook backend URL, publish a draft.

The API is undocumented and private. Endpoint shapes, payload structures, and
the CSRF extraction pattern were captured from Chrome DevTools HAR files
during interactive sessions with the dev console. The library is a robotic
re-play of that recorded behavior.

## Architecture

```
caller (any Python project)
    │
    ├── builds an aiohttp.ClientSession with Yandex Passport cookies
    │   (typically via ya-passport-auth.PassportClient.login_device_code)
    │
    ├── wraps it in an `authenticator` async context-manager factory
    │
    └── calls auto_create_skill(authenticator=..., backend_uri=..., ...)
                          │
                          ▼
              ┌─────────────────────────────┐
              │   auto_create_skill (orch)  │
              │  - Manages SkillCreation-   │
              │    Artifacts state machine  │
              │  - Skips completed steps    │
              │  - Persists progress via    │
              │    progress_cb              │
              └──────────────┬──────────────┘
                             │
                             ▼
                ┌────────────────────────┐
                │  DialogsSkillCreator   │
                │  - fetch_csrf          │
                │  - create_app          │
                │  - upload_logo         │
                │  - update_draft        │
                │  - create_oauth_app    │
                │  - attach_oauth        │
                │  - request_deploy      │
                └────────────────────────┘
```

### Key design decisions

- **Authentication is the caller's problem.** The lib accepts a no-arg
  async-context-manager factory yielding an authenticated `aiohttp.ClientSession`.
  This decouples the lib from any specific Device Flow UX (web pages, CLI
  prompts, Telegram bots, etc.).
- **All URLs are pre-computed by the caller.** The lib doesn't know about
  cloud-relay services, direct-mode webservers, or any specific backend host.
  Caller passes `backend_uri`, `oauth_authorize_url`, `oauth_token_url`,
  `oauth_client_id`, `oauth_client_secret` as plain strings.
- **State machine is caller-managed.** `SkillCreationArtifacts` is a frozen
  dataclass; pass it in, get an updated copy back. Persist between calls.
  `progress_cb` fires on every state transition for incremental persistence.
- **Failures are values, not exceptions.** `auto_create_skill` and
  `auto_rename_dialog_skill` never raise on Yandex API errors — they return
  artifacts with `state=FAILED` and `last_error` set. Truly unexpected
  exceptions still propagate.

## Module structure

```
src/ya_dialogs_api/
├── __init__.py        — public re-exports (24 symbols)
├── api_client.py      — DialogsSkillCreator + orchestrator + payload builders
├── state.py           — SkillCreationArtifacts dataclass + dump/load JSON
├── py.typed           — PEP 561 marker
└── assets/
    └── default_logo.png   — bundled fallback skill logo (1024x1024 PNG)

tests/
├── test_api_client.py — DialogsSkillCreator method tests + orchestrator tests
└── test_state.py      — state machine serialization roundtrips
```

## Development setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q              # 47 tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## Code standards

- **Python 3.12+** (CI matrix: 3.12, 3.13, 3.14).
- **`mypy --strict`** clean. `disallow_any_explicit = false` because the lib
  parses arbitrary JSON from an unofficial API; `dict[str, Any]` is the
  cleanest representation.
- **`ruff`** with the full select set from ya-passport-auth.
- **No MA imports.** This lib is generic. Anything Music Assistant-specific
  belongs in the consuming provider, not here.
- **Async/await throughout** (aiohttp).
- **Commits**: `type(scope): description` — feat, fix, docs, style,
  refactor, test, chore.

## Release process

VERSION-file driven (matches Music Assistant provider repos):

```bash
# 1. Pre-flight: TestPyPI
echo "1.0.0rc1" > VERSION
git commit -am "chore: prepare 1.0.0rc1"
git push origin main
git tag v1.0.0rc1 && git push --tags
# release.yml: VERSION-vs-tag check → uv build → Sigstore → TestPyPI

# 2. Stable
echo "1.0.0" > VERSION
git commit -am "chore: bump VERSION to 1.0.0"
git push origin main
git tag v1.0.0 && git push --tags
# release.yml → PyPI publish via OIDC
```

The release workflow asserts `cat VERSION == ${GITHUB_REF_NAME#v}` before
publishing — guards against tag/VERSION drift.

## Gotchas

- **The Yandex dev-console API can change without notice.** If a pipeline step
  starts returning unexpected responses, dump the response body in the error
  and run the consoles in DevTools mode to capture the new shape.
- **CSRF token format**: extracted via regex `"secretkey":"([^"]+)"` from the
  developer page HTML. Captured 2026-04-24; if Yandex re-renders, the regex
  misses and `fetch_csrf` raises `DialogsCsrfError`.
- **Channel parameter**: `"smartHome"` for Smart Home skills, `"aliceSkill"`
  for custom dialog skills. The wrong channel returns 400 with empty body.
- **Dialog draft `structuredExamples` shape**: each entry must be
  `{"marker": ..., "activationPhrase": ..., "request": ..., "is_valid": True}`.
  Wrong shape (`{"phrase": ...}`) returns silent 400 + empty body.
- **Deploy is async on Yandex's side.** `request_deploy` returns 2xx as soon
  as the request is queued; smart_home publishes within seconds, dialog
  skills go through moderation (5-15 min typical). The caller surfaces the
  in-console URL so users can watch the on-air indicator.
