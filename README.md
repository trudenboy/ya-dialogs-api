# ya-dialogs-api

> Async Yandex Dialogs Developer API client — programmatic skill creation, draft management, OAuth Device Flow.

[![CI](https://github.com/trudenboy/ya-dialogs-api/actions/workflows/ci.yml/badge.svg)](https://github.com/trudenboy/ya-dialogs-api/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ya-dialogs-api)](https://pypi.org/project/ya-dialogs-api/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## What this is

A framework-agnostic Python library that drives the **Yandex Dialogs Developer
API** — the meta-API at `dialogs.yandex.ru/developer-api/v2/` used to
programmatically create and manage Alice skills (Smart Home and custom dialog
skills). Where every other Yandex Alice library on PyPI handles the *runtime*
side (incoming webhook requests from end users), this one handles the
*provisioning* side: sign in via Yandex Passport Device Flow, create a skill,
upload a logo, set the webhook backend URL, publish a draft.

It exists because Yandex doesn't publish a developer-API SDK and the only
known Python implementation lived inside the Music Assistant
`ma-provider-yandex-smarthome` plugin. This library is that code, extracted
and made generic.

## Features

- **Full skill auto-creation pipeline** — CSRF/cookie session → `POST /apps`
  → upload logo → `PATCH` draft → optional OAuth app creation → `POST` deploy.
- **Two channel types** — `channel="smartHome"` for Yandex Smart Home skills
  (always requires OAuth account-linking), `channel="aliceSkill"` for Alice
  custom dialog skills (OAuth-free or OAuth-attached).
- **Optional OAuth** — `aliceSkill` skills can be created without account-linking
  by omitting `oauth_*` params entirely. `smartHome` always requires all four.
- **Incremental state machine** — `SkillCreationArtifacts` snapshots progress
  after every step. On transient failure, retry resumes from the last
  completed step instead of restarting.
- **Typed errors** — `DialogsAuthError`, `DialogsValidationError`,
  `DialogsSkillNotFoundError`, `DialogsDuplicateSkillError` with structured
  field info where available.
- **Framework-agnostic** — caller produces an authenticated `aiohttp.ClientSession`
  (typically via `ya-passport-auth`) and wraps it in an `AuthenticatorCM`
  factory. No opinion on Device Flow UX.
- **Strictly typed** — `mypy --strict` clean, PEP 561 `py.typed` marker.
- **Security-aware** — `SecretStr` redacts tokens in repr/str/format/tracebacks
  (re-exported from `ya-passport-auth`).

## Installation

```bash
pip install ya-dialogs-api
```

## Quick start

### Smart Home skill (OAuth required)

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
import aiohttp
from ya_dialogs_api import (
    AuthenticatorCM,
    SMART_HOME_CHANNEL,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
)

# Wrap your authenticated session in an async context-manager factory.
@asynccontextmanager
async def my_authenticator() -> AsyncIterator[aiohttp.ClientSession]:
    # Typically: ya_passport_auth.PassportClient.login_device_code(...)
    async with aiohttp.ClientSession(cookies={"Session_id": "..."}) as session:
        yield session

async def main() -> None:
    artifacts = SkillCreationArtifacts(state=SkillCreationState.PENDING)
    result = await auto_create_skill(
        authenticator=my_authenticator,
        channel=SMART_HOME_CHANNEL,       # "smartHome"
        skill_name="My Smart Home",
        artifacts=artifacts,
        backend_uri="https://my-backend.example.com/yandex",
        oauth_authorize_url="https://my-backend.example.com/oauth/authorize",
        oauth_token_url="https://my-backend.example.com/oauth/token",
        oauth_client_id="my-client-id",
        oauth_client_secret="my-client-secret",
    )
    if result.state == SkillCreationState.DONE:
        print(f"Skill created: skill_id={result.skill_id}")
    else:
        print(f"Failed at step {result.state}: {result.last_error}")

asyncio.run(main())
```

### Alice dialog skill (OAuth-free)

```python
from ya_dialogs_api import DIALOG_CHANNEL, auto_create_skill

result = await auto_create_skill(
    authenticator=my_authenticator,
    channel=DIALOG_CHANNEL,               # "aliceSkill"
    skill_name="My Dialog Skill",
    artifacts=artifacts,
    backend_uri="https://my-backend.example.com/alice",
    description="A skill that does something useful",
    # oauth_* params omitted → no account-linking
)
```

### Alice dialog skill with OAuth

Pass all four `oauth_*` params with `channel="aliceSkill"` to attach an OAuth
application (account-linking). Partial sets raise `ValueError` synchronously.

## Public API

| Symbol | Description |
|--------|-------------|
| `auto_create_skill(...)` | Full pipeline orchestrator. Resumable via `SkillCreationArtifacts`. |
| `auto_rename_dialog_skill(...)` | Patch dialog skill draft name and re-deploy. |
| `DialogsSkillCreator` | Low-level client (one method per pipeline step, including `delete_skill`). |
| `Channel` | `Literal["smartHome", "aliceSkill"]` — Yandex wire values. |
| `SMART_HOME_CHANNEL` / `DIALOG_CHANNEL` | Typed constants for the two channels. |
| `SkillCreationArtifacts` / `SkillCreationState` | State machine; serialize with `dump_artifacts` / `load_artifacts`. |
| `AuthenticatorCM` | Protocol: no-arg async context-manager factory yielding `aiohttp.ClientSession`. |
| `DialogsApiError` (base) | All library exceptions derive from this. |
| `DialogsAuthError` | HTTP 401/403, HTML auth-wall, or 30x redirect to Passport. |
| `DialogsCsrfError` | CSRF token not found in developer console HTML. |
| `DialogsValidationError` | Domain-level field validation or Spring 4xx rejection. |
| `DialogsSkillNotFoundError` | Spring 404 "Skill not found"; `skill_id` attribute set. |
| `DialogsDuplicateSkillError` | Skill with this name already exists. |
| `SecretStr` | Re-exported from `ya-passport-auth` for token redaction. |
| `load_default_logo_bytes()` | Bundled 1024×1024 fallback skill logo. |

## Status

**Stable (2.0.0).** The public API is stable. The underlying Yandex dev-console
endpoints are unofficial (reverse-engineered from DevTools traces) and may
break without notice. The library is actively maintained against the current
production endpoint behavior.

## See also

- [`ya-passport-auth`](https://github.com/trudenboy/ya-passport-auth) — Yandex
  Passport authentication library. Required runtime dependency.
- [`ma-provider-yandex-smarthome`](https://github.com/trudenboy/ma-provider-yandex-smarthome)
  and [`ma-provider-yandex-alice`](https://github.com/trudenboy/ma-provider-yandex-alice)
  — Music Assistant providers that consume this library.

## License

[MIT](LICENSE)
