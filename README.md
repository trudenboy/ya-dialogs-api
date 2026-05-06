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
upload a logo, set the webhook backend URL, publish a draft, poll for
publication state.

It exists because Yandex doesn't publish a developer-API SDK and the only
known Python implementation lived inside the Music Assistant
`ma-provider-yandex-smarthome` plugin. This library is that code, extracted
and made generic.

## Features

- **Full skill auto-creation pipeline** — Device Flow OAuth → CSRF/cookie
  session → POST `/apps` → POST `/skills/{id}/draft` → publication polling →
  callback URL extraction.
- **Two channel types** — `skill_type="smart_home"` for Yandex Smart Home
  skills, `skill_type="dialog"` for Alice custom dialog (`aliceSkill`) skills.
- **Incremental state machine** — `SkillCreationArtifacts` snapshots progress
  after every step. On transient failure, retry resumes from the last
  completed step instead of restarting.
- **Framework-agnostic** — caller provides a `WebserverAdapter` Protocol
  implementation for hosting the short-lived Device Code activation page.
  Works with aiohttp, FastAPI, Starlette, or any ASGI/aiohttp-compatible
  webserver.
- **Strictly typed** — `mypy --strict` clean, PEP 561 `py.typed` marker.
- **Security-aware** — `SecretStr` redacts tokens in repr/str/format/tracebacks
  (re-exported from `ya-passport-auth`).

## Installation

```bash
pip install ya-dialogs-api
```

## Quick start

```python
import asyncio
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    WebserverAdapter,
    auto_create_skill,
    load_default_logo_bytes,
)

# 1. Implement WebserverAdapter against your HTTP framework.
#    See docs for an aiohttp reference implementation.
my_webserver: WebserverAdapter = ...

# 2. Drive the pipeline. Persist artifacts between calls — the next
#    invocation will resume from the last completed step.
async def main() -> None:
    artifacts = SkillCreationArtifacts(state=SkillCreationState.PENDING)
    result = await auto_create_skill(
        webserver=my_webserver,
        connection_type="direct",
        skill_name="My Smart Home",
        artifacts=artifacts,
        cloud_instance_id="...",
        direct_client_secret="...",
        logo_bytes=load_default_logo_bytes(),
        session_id="cfg-flow-1",
        skill_type="smart_home",
    )
    if result.state == SkillCreationState.DONE:
        print(f"Skill created: skill_id={result.skill_id}")
    else:
        print(f"Failed at step {result.state}: {result.last_error}")

asyncio.run(main())
```

## Status

**Beta.** The API is stable enough to depend on, but the underlying Yandex dev-
console endpoints are unofficial (reverse-engineered from DevTools traces) and
may break without notice. The library is actively maintained against the
current production endpoint behavior.

## See also

- [`ya-passport-auth`](https://github.com/trudenboy/ya-passport-auth) — Yandex
  Passport authentication library. Required runtime dependency for the OAuth
  Device Flow used by this library.
- [`ma-provider-yandex-smarthome`](https://github.com/trudenboy/ma-provider-yandex-smarthome)
  and [`ma-provider-yandex-alice`](https://github.com/trudenboy/ma-provider-yandex-alice)
  — Music Assistant providers that consume this library.

## License

[MIT](LICENSE)
