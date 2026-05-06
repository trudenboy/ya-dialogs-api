# Plan: Extract Yandex Dialogs voice skill into `ma-provider-yandex-alice`

## Context

**Problem.** `ma-provider-yandex-smarthome` has organically grown two distinct features:

1. **Smart Home device bridge** (~3 500 LOC) — registers MA players as Yandex IoT devices through Smart Home API; `device.py`, `handlers.py`, `direct.py`, `cloud.py`, `notifier.py`. Voice goes through Yandex's NLU; the plugin only sees normalised capability actions (`on_off`, `range:volume`, `toggle:pause`).
2. **Yandex Dialogs custom skill** (~3 100 LOC) — runs its own Russian NLU on raw user utterances captured by an Alice custom skill, dispatches to `play_media` / `set_shuffle` / `transfer_queue` / etc.; `dialogs.py`, `dialogs_control.py`, `dialogs_nlu.py`, `dialogs_player.py`. The two halves do not import from each other (verified during Phase 1 — zero cross-imports between `dialogs*.py` and `device.py / handlers.py / cloud.py / direct.py / notifier.py`).

The two features were bundled because both speak to Yandex Dialogs API and reuse the same auto-create-skill pipeline. But they are now large enough that:

- The smarthome repo's manifest, README, CHANGELOG, config schema mix two unrelated concerns.
- `__init__.py:get_config_entries()` is 229 LOC of intricate state-machine UI for two skill types (Smart Home + Dialog) and three connection modes — hostile to maintain.
- A user who wants only Smart Home is shown nine voice-only config entries; a user who wants only voice is shown the cloud-relay flow they don't need.
- The voice skill has reached v1.9.1 with a full command surface (play / pause / next / volume / shuffle / repeat / seek / transfer / now-playing / add-to-queue) — it stands on its own.

**Outcome.**

- New repository `trudenboy/ma-provider-yandex-alice` published on GitHub: a clean MA plugin provider (`domain: yandex_alice`) that handles ONLY the Dialogs custom skill (voice intent → MA control). First release `1.0.0`.
- New repository `trudenboy/ya-dialogs-api` published on GitHub and PyPI as `ya-dialogs-api`: a generic, **MA-independent** pip package implementing the Yandex Dialogs Developer API client (skill auto-creation, draft management, publication polling, OAuth Device Flow) + the artifact state machine + the default skill logo asset. Used by both providers as a manifest runtime dependency. First release `1.0.0`. Naming intentionally drops the `ma-` prefix — this is a generic Yandex Dialogs dev-API client, usable in any Python project (e.g. CLI tooling, other home-automation hubs, custom Alice skill bootstrap scripts).
- `ma-provider-yandex-smarthome` released as `2.0.0` with all `dialogs*.py` removed, voice-related config keys removed, voice-related auto-create flow removed. Depends on `ya-dialogs-api` from PyPI for the API client.
- `ma-provider-tools` (the dev/CI infrastructure repo) gains a new entry in `providers.yml` for `yandex_alice`. The shared lib is NOT a provider, so it does NOT get an entry there — it has its own minimal CI of two workflows.
- All three published in one atomic round, since the user picked atomic sequencing — no compatibility shim, no transitional version.

**Scope decisions (locked by user answers).**

- **Shared `auto_skill*.py` code** → standalone pip package `ya-dialogs-api` (repo `ya-dialogs-api`). Single source of truth, both providers depend on it via manifest `requirements`. Generic name — not coupled to Music Assistant.
- **Sequencing** → atomic. `ya-dialogs-api 1.0.0` ships first (only because the providers depend on it); `ma-provider-yandex-alice 1.0.0` and `ma-provider-yandex-smarthome 2.0.0` ship in the same release window. Migration note in smarthome 2.0.0 CHANGELOG explains how to install alice and re-run auto-create for the dialog skill.

**Prior-art research (PyPI + GitHub, May 2026).**

Searched for existing implementations of the Yandex Dialogs **Developer API** (programmatic skill creation, draft management, publication polling — i.e. the meta-API exposed at `dialogs.yandex.ru/developer/api/v2/`). Findings:

- **No existing equivalent on PyPI or GitHub.** Every Yandex Alice / Dialogs Python library found is a *skill-runtime* framework (handles incoming webhook requests from end users): [`aliceio`](https://pypi.org/project/aliceio/) (asyncio, Python 3.9+, actively maintained Sept 2025), [`aioalice`](https://github.com/mahenzon/aioalice), [`alice_types`](https://github.com/sameoldmadness/awesome-alice) (Pydantic-V2 envelope models), `dialogic`, `alice-scripts`. None of them touch the developer-console API.
- **Yandex itself does not publish an SDK** for the developer API. The endpoints are accessible only via cookie-authenticated CSRF-protected POSTs against the dev-console host — there is no documented public REST API for skill creation.
- **`auto_skill.py` is novel.** The current 1 630-LOC implementation (in smarthome's `provider/auto_skill.py`) reverse-engineers the dev-console flow (from DevTools traces) and drives it as a robotic UI client: Device Flow OAuth → CSRF/cookie session → POST `/apps` → POST `/skills/{id}/draft` (with smartHome OR aliceSkill channel payload) → publication polling → callback URL extraction. It's the only Python implementation of this pipeline that exists.
- **Conclusion.** Publishing `ya-dialogs-api` as a standalone PyPI package fills a real gap. There is no library to fork or reuse — but we will keep `ya-passport-auth` (already a runtime dep) for the OAuth Device Flow, and we will optionally consider `alice_types` for the alice provider's webhook *runtime* envelope models in a future refactor (out of scope for this extraction).
- **Name availability.** PyPI `ya-dialogs-api`, `ya-dialogs`, `ya-skills-api`, `ya-skills` all return 404 (free as of May 2026 — verified via `curl https://pypi.org/pypi/<name>/json`). Final names: PyPI `ya-dialogs-api`, GitHub repo `trudenboy/ya-dialogs-api`, Python module `ya_dialogs_api`. The `dialogs` term is more descriptive than `skills`: the API surface is the "Yandex Dialogs Developer API" hosted at `dialogs.yandex.ru/developer-api/v2/...`; the term matches Yandex's own product name.

**Out of scope.**

- Splitting `auto_skill_ui.py` (smart-home-only config-flow UI) — it stays in smarthome.
- Splitting `cloud.py` / `direct.py` — entirely smart-home; stay.
- Voice command additions (group/ungroup, like/dislike, sleep timer) — separate future PRs.
- Backwards-compat shim in smarthome 2.0.0 to keep dialogs*.py alive — explicitly rejected; users re-install in alice repo.

---

## Architecture (after extraction)

```
trudenboy/ya-dialogs-api                       (NEW — pip package on PyPI: ya-dialogs-api)
  src/ya_dialogs_api/
    __init__.py            # public re-exports
    api_client.py          # ← refactored from auto_skill.py:
                           #   - drop mass: MusicAssistant params (no MA imports at all)
                           #   - drop _default_authenticator (it hosts an MA-specific HTML
                           #     page; UX concern moves to provider). authenticator becomes
                           #     a REQUIRED parameter — caller produces the aiohttp session.
                           #   - drop _resolve_base_url, derive_backend_uri,
                           #     derive_auth_urls, derive_client_id (all need a base_url
                           #     resolver; provider computes these and passes pre-computed
                           #     strings to the lib).
                           #   - drop CLOUD_* / DIRECT_* / CONNECTION_TYPE_* constants
                           #     (these are MA/smarthome concepts, not Yandex Dialogs API).
                           #   Result: ~900 LOC (was 1 630). Pure dev-API client logic.
    state.py               # ← copied from auto_skill_state.py (122 LOC)
    _compat.py             # ← copied from provider/_compat.py (SecretStr wrapper)
    assets/
      default_logo.png     # ← copied from provider/auto_skill_logo.png
  tests/                   # ← extracted test_auto_skill.py + test_auto_skill_state.py
  pyproject.toml           # name = "ya-dialogs-api"; dynamic version; ZERO MA dependency
  CHANGELOG.md, VERSION (1.0.0), LICENSE, NOTICE, SECURITY.md, README.md
  .github/workflows/{ci,release,codeql,scorecard,secrets-nightly}.yml

trudenboy/ma-provider-yandex-alice            (NEW — registered in providers.yml)
  provider/
    __init__.py            # NEW (~200 LOC) — voice-only get_config_entries + setup
    plugin.py              # NEW (~150 LOC) — wires DialogsWebhookHandler only
    manifest.json          # domain=yandex_alice, type=plugin, requirements=[ya-dialogs-api>=1.0.0]
    constants.py           # NEW (~50 LOC) — only voice + auto-create-dialog keys
    dialogs.py             # ← copied verbatim (1 311 LOC)
    dialogs_control.py     # ← copied verbatim (468 LOC)
    dialogs_nlu.py         # ← copied verbatim (474 LOC)
    dialogs_player.py      # ← copied verbatim (313 LOC)
    dialog_config_ui.py    # NEW (~150 LOC) — voice-specific config-entry builder
                           #   (lifted from smarthome __init__.py dialog section)
    ma_authenticator.py    # NEW (~80 LOC) — provider-side Device Flow authenticator:
                           #   uses ya-passport-auth to get x_token, hosts the MA-specific
                           #   activation HTML page on mass.webserver, yields an authorized
                           #   aiohttp.ClientSession to ya_dialogs_api.auto_create_skill.
                           #   Identical copy in smarthome (the activation-page UX is
                           #   provider-specific but the same code works for both).
    playlists.py           # ← copied verbatim (57 LOC, used by player filter UI)
  tests/
    test_dialogs.py        # ← copied verbatim (1 676 LOC)
    test_dialogs_control.py
    test_dialogs_nlu.py
    test_dialogs_player.py
    test_basic.py          # NEW — sanity test for plugin load
    test_config_actions.py # NEW — voice-specific config-flow tests
  conftest.py              # ← adapted from smarthome (provider package alias renamed)
  CHANGELOG.md             # NEW — starts with [1.0.0] - YYYY-MM-DD
  VERSION                  # 1.0.0
  All wrappers (pyproject.toml, ruff.toml, .pre-commit-config.yaml, docker-compose.dev.yml,
   scripts/, .github/workflows/{test,pipeline,release,security,docs,backport,sync-*}.yml)
   are auto-generated by ma-provider-tools/distribute.yml on next push to main.

trudenboy/ma-provider-yandex-smarthome        (MODIFY → 2.0.0)
  provider/
    __init__.py            # voice section removed (~150 LOC dropped from get_config_entries)
    plugin.py              # DialogsWebhookHandler import + wiring removed
    constants.py           # CONF_DIALOG_*, DIALOG_* removed (~25 LOC dropped)
    dialogs*.py            # DELETED (4 files, 2 566 LOC)
    auto_skill.py          # DELETED (now imported from ya_dialogs_api.api_client)
    auto_skill_state.py    # DELETED (now imported from ya_dialogs_api.state)
    auto_skill_logo.png    # DELETED (now in ya_dialogs_api package data)
    _compat.py             # DELETED (now imported from ya_dialogs_api._compat)
    smarthome_config_ui.py # RENAMED from auto_skill_ui.py for clarity (was misleadingly
                           #   named — it's the Smart Home config-flow entry builder, not a
                           #   generic UI helper). Imports rewritten:
                           #     from .auto_skill_state import SkillCreationArtifacts, SkillCreationState
                           #       → from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState
                           #   (Note: it does NOT import from .auto_skill — verified by grep —
                           #    so no other rewrites needed beyond auto_skill_state.)
    device.py, handlers.py, direct.py, cloud.py, notifier.py, schema.py # unchanged
    ma_authenticator.py    # NEW — provider-side Device Flow + activation HTML page,
                           #   yields an authorized aiohttp session to ya_dialogs_api.
                           #   Identical to alice's copy. Hosts page via mass.webserver.
  tests/
    test_dialogs*.py       # DELETED (4 files)
    test_auto_skill*.py    # DELETED (move to ya-dialogs-api repo)
    everything else        # unchanged; updates imports to use ya_dialogs_api
  manifest.json            # requirements=["ya-passport-auth==1.3.0", "ya-dialogs-api>=1.0.0"]
  VERSION                  # 1.9.1 → 2.0.0
  CHANGELOG.md             # adds [2.0.0] with breaking-change + migration section

trudenboy/ma-provider-tools                   (MODIFY)
  providers.yml            # adds yandex_alice entry (line ~180, before yandex_ynison)
                           # smarthome entry's `features` list updated (drops voice item)
                           # smarthome entry adds runtime_dependency "ya-dialogs-api>=1.0.0"
```

---

## Phase A — `ya-dialogs-api` (pip package, ships first)

**Template repo: [`trudenboy/ya-passport-auth`](https://github.com/trudenboy/ya-passport-auth)** — a sister library by the same author, already on PyPI as `ya-passport-auth`. Mirror its conventions wherever they apply (build system, branch, CI, security tooling, dev workflow). Diverge only where the API client has different needs.

### A.1 Conventions inherited from `ya-passport-auth`

| Aspect | Choice (matches template) |
|---|---|
| Default branch | `main` (NOT `dev` — provider repos use `dev`, libraries here use `main`) |
| Build backend | `hatchling >= 1.25` (matches ya-passport-auth) but with version sourced from `VERSION` file via `[tool.hatch.version] path = "VERSION"` (regex source) — single bump-point, no `_version.py` codegen, no git-tag dependency at build time. Tag is created from VERSION at release time (CI gate verifies match). |
| Layout | `src/ya_dialogs_api/` (PEP 561 + `py.typed` marker) |
| Optional deps group | `dev` (not `test` — single combined group with bandit, pip-audit, cyclonedx-bom, liccheck, hypothesis, aioresponses, mypy, ruff, pytest, pytest-asyncio, pytest-cov, pre-commit) |
| Toolchain | `uv` everywhere (`uv sync --frozen --extra dev`, `uv build`, `uv publish`) |
| Lock file | `uv.lock` committed |
| Required root files | `LICENSE` (MIT), `NOTICE`, `SECURITY.md`, `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `PLAN.md` |
| CI workflows | `ci.yml` (lint+typecheck+test matrix) + `release.yml` (build+Sigstore+TestPyPI on rc + PyPI on stable) + `codeql.yml` + `scorecard.yml` + `secrets-nightly.yml` |
| GHA security | `step-security/harden-runner` (egress-policy: audit) on every job; all third-party action SHAs pinned with version comments; top-level + per-job `permissions: {}` minimum |
| Python support | 3.12 / 3.13 / 3.14 in test matrix |
| mypy | `strict = true` with `disallow_any_explicit`, `warn_unreachable`, files = `["src", "tests"]` |
| ruff | line-length 100, target-version py312, src=`["src", "tests"]`, full select set |
| Versioning | `VERSION` file at repo root (single line, e.g. `1.0.0` or `1.0.0rc1`). Single source of truth — hatchling reads it at build time. Git tag `v$(cat VERSION)` is created at release time and CI asserts the tag matches VERSION before publishing. Matches the provider repos' workflow. |

### A.2 Repo creation

- `gh repo create trudenboy/ya-dialogs-api --public --description "Yandex Dialogs Developer API client — programmatic skill creation, draft management, OAuth Device Flow"` (asks user before running).
- Local path: `/Users/renso/Projects/ya-dialogs-api`.
- Default branch: `main`.
- Description deliberately omits Music Assistant — the lib is a generic Yandex Dialogs dev-API client that any Python project can use.
- Initial bootstrap: clone `trudenboy/ya-passport-auth` to a temp dir, copy the skeleton (workflows, pyproject scaffolding, .gitignore, .pre-commit-config.yaml, ruff.toml, NOTICE, SECURITY.md), then rename / re-target identifiers (`ya_passport_auth` → `ya_dialogs_api`, package name, README, etc.).

### A.3 Package layout (mirrors ya-passport-auth)

```
ya-dialogs-api/
├── src/
│   └── ya_dialogs_api/
│       ├── __init__.py          # public API re-exports
│       ├── api_client.py        # ← refactored from provider/auto_skill.py (~900 LOC after MA-decoupling)
│       ├── state.py             # ← provider/auto_skill_state.py (122 LOC, verbatim)
│       ├── _compat.py           # ← provider/_compat.py (31 LOC)
│       ├── py.typed             # PEP 561 marker
│       └── assets/
│           ├── __init__.py
│           └── default_logo.png # ← provider/auto_skill_logo.png
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # fake authenticator (yields a pre-built aiohttp ClientSession);
│   │                            # aioresponses mocks for the dev-console endpoints
│   ├── test_api_client.py       # ← tests/test_auto_skill.py (renamed imports;
│   │                            #   replace MA-fixtures with fake authenticator)
│   └── test_state.py            # ← tests/test_auto_skill_state.py (renamed imports)
├── e2e_all_flows.py             # NEW — runnable E2E script (pattern from ya-passport-auth)
│                                #       drives the full pipeline against a throwaway Yandex Passport account
├── pyproject.toml
├── VERSION                      # 1.0.0 (single line, source of truth read by hatchling at build)
├── uv.lock                      # committed
├── CHANGELOG.md
├── LICENSE                      # MIT
├── NOTICE                       # third-party attributions (aiohttp, ya-passport-auth)
├── SECURITY.md                  # vulnerability reporting policy
├── README.md                    # badges + Features + Quick start (no MA references)
├── PLAN.md                      # this plan, copy of /Users/renso/.claude/plans/silly-rolling-key.md
├── CLAUDE.md                    # repo-specific Claude guidance
├── .pre-commit-config.yaml      # copy from ya-passport-auth
├── .gitignore                   # copy from ya-passport-auth (no _version.py needed)
└── .github/
    └── workflows/
        ├── ci.yml               # lint (ruff) + typecheck (mypy strict) + test matrix py3.12/3.13/3.14 × {ubuntu, macos}
        ├── release.yml          # tag-driven: uv build → Sigstore sign → TestPyPI (rc) → PyPI (stable) via OIDC
        ├── codeql.yml           # GitHub CodeQL security analysis
        ├── scorecard.yml        # OpenSSF Scorecard
        └── secrets-nightly.yml  # nightly secret scanning
```

### A.4 `pyproject.toml` essentials (hatchling + VERSION file)

```toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "ya-dialogs-api"
dynamic = ["version"]
description = "Async Yandex Dialogs Developer API client — programmatic skill creation, draft management, OAuth Device Flow"
readme = "README.md"
license = { file = "LICENSE" }
requires-python = ">=3.12"
authors = [{ name = "Mikhail Nevskiy" }]
keywords = ["yandex", "dialogs", "alice", "skill", "api-client", "oauth", "async", "aiohttp"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Framework :: AsyncIO",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Topic :: Internet :: WWW/HTTP",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "Typing :: Typed",
]
dependencies = [
    "aiohttp>=3.10,<4",
    "yarl>=1.12",
    "ya-passport-auth>=1.3.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5",
    "aioresponses>=0.7.6",
    "hypothesis>=6",
    "mypy>=1.11",
    "ruff>=0.6",
    "bandit[toml]>=1.7",
    "pip-audit>=2.7",
    "cyclonedx-bom>=4",
    "liccheck>=0.9",
    "pre-commit>=3.8",
]

[project.urls]
Homepage = "https://github.com/trudenboy/ya-dialogs-api"
Repository = "https://github.com/trudenboy/ya-dialogs-api"
Issues = "https://github.com/trudenboy/ya-dialogs-api/issues"
Changelog = "https://github.com/trudenboy/ya-dialogs-api/blob/main/CHANGELOG.md"

[tool.hatch.version]
path = "VERSION"
pattern = "^(?P<version>.+)$"

[tool.hatch.build.targets.wheel]
packages = ["src/ya_dialogs_api"]

[tool.hatch.build.targets.sdist]
include = [
    "src/ya_dialogs_api",
    "tests",
    "pyproject.toml",
    "VERSION",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "SECURITY.md",
]

[tool.mypy]
strict = true
python_version = "3.14"
files = ["src", "tests"]
warn_unreachable = true
warn_redundant_casts = true
warn_unused_ignores = true
disallow_any_explicit = true
enable_error_code = ["redundant-expr", "truthy-bool", "ignore-without-code", "possibly-undefined"]

[[tool.mypy.overrides]]
module = ["aioresponses", "aioresponses.*"]
ignore_missing_imports = true

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

# (full [tool.ruff.lint] config — copy verbatim from ya-passport-auth: select set,
#  isort sections, naming rules, per-file-ignores for tests, etc.)
```

Notably:

- **`version` is dynamic via `[tool.hatch.version]` reading the `VERSION` file** (regex pattern `^(?P<version>.+)$` matches any single-line PEP 440 version string). **`VERSION` is the single source of truth in this repo** — same convention as `ma-provider-yandex-smarthome` and other providers in this ecosystem. Bumping a release is `echo "1.0.1" > VERSION && git commit && git tag v1.0.1 && git push --tags`.
- `release.yml` asserts `[ "$(cat VERSION)" = "${GITHUB_REF_NAME#v}" ]` before publishing — guards against tag/VERSION drift.
- `dev` extra is the only optional-deps group (matches template — no `test` group).
- No `package-data` table needed — hatchling includes everything in `packages = ["src/ya_dialogs_api"]` automatically; `default_logo.png` is included via the `assets/` subpackage.
- No `_version.py` codegen, no `hatch-vcs` plugin (intentional simplification vs the ya-passport-auth template — the VERSION file matches what every provider repo here already uses).

### A.5 Public API (`src/ya_dialogs_api/__init__.py`)

```python
"""Yandex Dialogs Developer API client — programmatic skill creation, draft management.

Framework-agnostic: callers provide an authenticator (typically wrapping
``ya-passport-auth.PassportClient.login_device_code``) that yields an authorized
``aiohttp.ClientSession``. The library handles everything post-authentication:
CSRF/cookie session, POST /apps, draft management, publication polling.
"""
from .api_client import (
    DEVICE_FLOW_TIMEOUT_SECONDS,
    DialogsApiError,
    DialogsCsrfError,
    DialogsDuplicateSkillError,
    DialogsSkillCreator,
    SkillType,           # Literal["smart_home", "dialog"]
    auto_create_skill,
    auto_rename_dialog_skill,
    build_dialog_draft_payload,
    build_draft_payload,
    build_oauth_app_payload,
    load_default_logo_bytes,
)
from .state import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)
from ._compat import SecretStr

__all__ = [
    "DEVICE_FLOW_TIMEOUT_SECONDS",
    "DialogsApiError",
    "DialogsCsrfError",
    "DialogsDuplicateSkillError",
    "DialogsSkillCreator",
    "SecretStr",
    "SkillCreationArtifacts",
    "SkillCreationState",
    "SkillType",
    "auto_create_skill",
    "auto_rename_dialog_skill",
    "build_dialog_draft_payload",
    "build_draft_payload",
    "build_oauth_app_payload",
    "dump_artifacts",
    "load_artifacts",
    "load_default_logo_bytes",
]
```

### A.6 MA-decoupling refactor (the work that turns the copy into a library)

Reviewing the current `provider/auto_skill.py` honestly: only **one** function in it touches MA — `_default_authenticator`, which hosts an HTML activation page on `mass.webserver` during the OAuth Device Flow. The HTML page is **MA-specific UX**, not part of Device Flow protocol. Everything else (`DialogsSkillCreator`, payload builders, the pipeline, recovery logic, polling) is pure HTTP-against-Yandex-dev-console with no MA awareness.

The architecturally clean split:

- **Lib** = the post-authentication dev-console client. Accepts a caller-provided `authenticator` that yields a CSRF/cookie-set `aiohttp.ClientSession`. Knows nothing about Device Flow UX, webservers, or HTML pages.
- **Provider** = wraps `ya-passport-auth.login_device_code` (which already does Device Flow correctly via callback), hosts a custom MA-flavored activation page on `mass.webserver`, produces the session, hands it to the lib.

This matches the pattern the sister library `ya-passport-auth` already uses — Device Flow exposes a callback (`on_code(session)`) and lets the caller handle the UX.

#### Changes in api_client.py vs the original auto_skill.py

| Drop from lib | Why | Where it goes |
|---|---|---|
| `mass: MusicAssistant` parameter on every public function | Direct MA dep | Provider's authenticator handles `mass`-related concerns |
| `_default_authenticator` (~140 LOC) | Hosts MA-specific HTML page | Provider's `ma_authenticator.py` |
| `_resolve_base_url(mass, override)` | Reads `mass.webserver.base_url` | Provider computes its `backend_uri` itself |
| `derive_backend_uri`, `derive_auth_urls`, `derive_client_id` | All compute URLs from MA's connection_type+base_url | Provider; pre-computed strings passed to lib |
| `check_preconditions` | Validates HTTPS+base_url against MA state | Provider does its own preflight |
| `CONNECTION_TYPE_*`, `CLOUD_*`, `DIRECT_*` constants | MA/smarthome concepts (cloud-relay vs direct mode) | Stay in providers' constants.py |

#### Lib API after refactor

```python
# src/ya_dialogs_api/api_client.py
async def auto_create_skill(
    *,
    authenticator: AuthenticatorCM,                # REQUIRED — context manager yielding aiohttp.ClientSession
    skill_name: str,
    artifacts: SkillCreationArtifacts,
    backend_uri: str,                              # caller pre-computes
    auth_authorize_url: str,                       # caller pre-computes
    auth_token_url: str,                           # caller pre-computes
    oauth_client_id: str,                          # caller pre-computes
    oauth_client_secret: str,                      # caller pre-computes (per-install secret OR fixed cloud-protocol value)
    logo_bytes: bytes,
    skill_type: SkillType = "smart_home",          # Literal["smart_home", "dialog"]
    progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]] | None = None,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    timeout: float = DEVICE_FLOW_TIMEOUT_SECONDS,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts: ...

# AuthenticatorCM = Callable[[], AbstractAsyncContextManager[aiohttp.ClientSession]]
# i.e. a no-arg async context manager factory. Caller manages auth lifecycle.

async def auto_rename_dialog_skill(
    *,
    authenticator: AuthenticatorCM,
    artifacts: SkillCreationArtifacts,
    new_name: str,
    backend_uri: str,
    timeout: float = DEVICE_FLOW_TIMEOUT_SECONDS,
    creator_factory: Callable[[aiohttp.ClientSession], DialogsSkillCreator] | None = None,
    developer_name: str = "Skill creator",
) -> SkillCreationArtifacts: ...
```

Both functions return `SkillCreationArtifacts` — verified V1 in audit, the lib does not introduce a separate result type.

#### Provider-side authenticator (`ma_authenticator.py`, ~80 LOC, identical in alice + smarthome)

```python
# Sketch
@asynccontextmanager
async def make_authenticator(
    *,
    mass: MusicAssistant,
    session_id: str,
    timeout: float,
    cached_x_token: str | None,
    on_token_obtained: Callable[[str], None] | None = None,
) -> AsyncIterator[aiohttp.ClientSession]:
    # 1. If cached_x_token is fresh, build session from it directly.
    # 2. Otherwise, kick off ya-passport-auth.PassportClient.login_device_code
    #    with on_code callback that registers an MA-webserver route showing
    #    the activation page (HTML rendering stays here, not in the lib).
    # 3. Yield an aiohttp.ClientSession with passport cookies + CSRF set up.
    # 4. On exit, unregister the route.
```

Net effect: lib has **zero MA imports**, the entire MA-coupling lives in the provider's `ma_authenticator.py` (~80 LOC).

### A.7 Source migration mechanics

- Refactor `provider/auto_skill.py` → `src/ya_dialogs_api/api_client.py`:
  - Remove `from music_assistant.mass import MusicAssistant` import; remove `mass: MusicAssistant` parameter from every public function.
  - Delete `_default_authenticator` (~140 LOC) — the activation-page HTML rendering moves to the provider's `ma_authenticator.py`.
  - Delete `_resolve_base_url`, `derive_backend_uri`, `derive_auth_urls`, `derive_client_id`, `check_preconditions` (caller pre-computes these and passes them in).
  - Delete `_build_authenticator_cm` indirection — `authenticator` becomes a required positional / keyword-only arg, no default.
  - Delete imports of `CONNECTION_TYPE_*`, `CLOUD_*`, `DIRECT_*` constants from smarthome's constants.py (these never enter the lib).
  - Update `auto_create_skill` and `auto_rename_dialog_skill` signatures to accept pre-computed `backend_uri`, `auth_authorize_url`, `auth_token_url`, `oauth_client_id`, `oauth_client_secret` (replacing what `derive_*` used to compute from `connection_type`+`mass`).
  - Replace logo path from `pathlib.Path(__file__).parent / "auto_skill_logo.png"` to `importlib.resources.files("ya_dialogs_api.assets") / "default_logo.png"` (in `load_default_logo_bytes`).
  - Net: ~700 LOC dropped from the original 1 630, file shrinks to ~900 LOC of pure dev-console API client.
- Copy `provider/auto_skill_state.py` → `src/ya_dialogs_api/state.py`. Verbatim — pure dataclasses + JSON serialization.
- Copy `provider/_compat.py` → `src/ya_dialogs_api/_compat.py`. Verbatim.
- Copy `provider/auto_skill_logo.png` → `src/ya_dialogs_api/assets/default_logo.png`.
- Copy `tests/test_auto_skill.py` and `tests/test_auto_skill_state.py`: rewrite imports (`provider.auto_skill` → `ya_dialogs_api.api_client` etc.). Replace MA-fixtures and `mass.webserver.register_dynamic_route` mocks with a fake authenticator that yields a pre-built `aiohttp.ClientSession` populated by `aioresponses`. Drop conftest MA stubs entirely — the lib has zero MA imports.
- Write a new `README.md` showing a minimal usage example with `ya-passport-auth.PassportClient` building the authenticator directly (no MA, no webserver), to make the framework-agnostic positioning clear.

### A.8 CI workflows (copy verbatim from ya-passport-auth, retarget identifiers)

All five workflow files are taken from `trudenboy/ya-passport-auth/.github/workflows/` with the package/repo name retargeted from `ya-passport-auth` / `ya_passport_auth` to `ya-dialogs-api` / `ya_dialogs_api`. No structural changes needed; the template is mature.

| File | Trigger | What it does |
|---|---|---|
| `ci.yml` | push to `main`, PR | `lint` (ruff check + ruff format --check), `typecheck` (mypy --strict on src+tests), `test` matrix (py3.12/3.13/3.14 × ubuntu/macos, pytest + coverage upload). All jobs use `step-security/harden-runner` egress audit + pinned action SHAs + minimum permissions. |
| `release.yml` | push tag `v*` | **NEW guard step** (not in template — added because we use VERSION file): assert `[ "$(cat VERSION)" = "${GITHUB_REF_NAME#v}" ]` else fail. Then `build` (uv build → wheel + sdist) → `sign` (Sigstore via `sigstore/gh-action-sigstore-python` with `id-token: write`) → `publish-testpypi` (only when tag contains `rc`, OIDC → test.pypi.org) → `publish-pypi` (stable tags only, OIDC → pypi.org). Each environment is a separate GitHub Environment for OIDC scoping. |
| `codeql.yml` | weekly + push | GitHub CodeQL static security analysis (Python). |
| `scorecard.yml` | weekly | OpenSSF Scorecard — supply-chain hygiene metrics, results sent to GitHub Security tab. |
| `secrets-nightly.yml` | nightly cron | secret scanning (matches the upstream template — name notwithstanding, this workflow only does GitGuardian/Trufflehog-style secret scan; `pip-audit` and `bandit` run inside `ci.yml` as separate steps). |

**Manual prerequisite before first release:**

1. Register `ya-dialogs-api` project on PyPI (and TestPyPI) — owner: `mnevskiy` (matches ya-passport-auth project owner). Reserve the name even if first publish is later.
2. In PyPI project settings → Publishing → Trusted Publishers → add: GitHub repo `trudenboy/ya-dialogs-api`, workflow filename `release.yml`, environment `pypi`. Repeat for TestPyPI with environment `testpypi`.
3. Create matching GitHub repository environments (`pypi`, `testpypi`) with deployment protection rules (e.g. require manual approval for `pypi`).
4. Confirm `id-token: write` permission is granted for the publish jobs (already declared in template).

### A.9 Release

Release procedure (mirrors the provider VERSION-bump flow):

```bash
# 1. Pre-flight against TestPyPI
echo "1.0.0rc1" > VERSION
git commit -am "chore: prepare 1.0.0rc1 release"
git push origin main
git tag v1.0.0rc1 && git push --tags
#    → release.yml asserts cat VERSION == 1.0.0rc1 → uv build → Sigstore sign → TestPyPI publish

# 2. Smoke-install in a clean venv
python3 -m venv /tmp/smoke && source /tmp/smoke/bin/activate
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple ya-dialogs-api==1.0.0rc1
python -c "import ya_dialogs_api; print(ya_dialogs_api.__version__)"

# 3. Stable release
echo "1.0.0" > VERSION
git commit -am "chore: bump VERSION to 1.0.0"
git push origin main
git tag v1.0.0 && git push --tags
#    → release.yml → PyPI publish via OIDC. ya-dialogs-api==1.0.0 is live.
```

This must happen BEFORE the alice/smarthome releases, since both depend on `ya-dialogs-api>=1.0.0` via manifest `requirements`.

---

## Phase B — `ma-provider-yandex-alice` (new provider repo)

### B.1 Repo creation

- `gh repo create trudenboy/ma-provider-yandex-alice --public --description "Yandex Dialogs (Alice voice skill) provider for Music Assistant"` (asks user).
- Local path: `/Users/renso/Projects/ma-provider-yandex-alice`.
- Default branch: `dev`.

### B.2 `provider/manifest.json`

```json
{
  "type": "plugin",
  "domain": "yandex_alice",
  "name": "Yandex Alice",
  "description": "Voice control of Music Assistant via a Yandex Dialogs custom skill (Russian NLU, full command surface).",
  "codeowners": ["@trudenboy"],
  "credits": [
    "[dext0r/yandex_smart_home](https://github.com/dext0r/yandex_smart_home)"
  ],
  "requirements": [
    "ya-passport-auth==1.3.0",
    "ya-dialogs-api>=1.0.0"
  ],
  "documentation": "https://github.com/trudenboy/ma-provider-yandex-alice",
  "stage": "beta",
  "multi_instance": false,
  "builtin": false
}
```

### B.3 `provider/__init__.py` (NEW, ~250 LOC)

Skeleton:

```python
"""Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant."""
from __future__ import annotations
# imports: ConfigEntry, ProviderFeature, ya_dialogs_api, .dialog_config_ui, .plugin, .constants

SUPPORTED_FEATURES: set[ProviderFeature] = set()

async def setup(mass, manifest, config) -> ProviderInstanceType:
    return YandexAlicePlugin(mass, manifest, config, SUPPORTED_FEATURES)

async def get_config_entries(mass, instance_id, action, values) -> tuple[ConfigEntry, ...]:
    # (1) Resolve Yandex Passport auth state from CONF_AUTH_X_TOKEN
    # (2) Handle action: auto_create_skill — calls ya_dialogs_api.auto_create_skill(skill_type="aliceSkill", ...)
    # (3) Handle action: rename_dialog_skill — calls ya_dialogs_api.auto_rename_dialog_skill(...)
    # (4) Build entries via dialog_config_ui.build_entries(...)
    # (5) Append player/playlist filter UI (from .playlists.fetch_playlist_options)
    return entries
```

The body is a stripped-down version of smarthome's `get_config_entries` keeping only the dialog-skill code paths. Roughly 60 % shorter than the smarthome original. Cross-reference reused helpers — re-implement nothing.

### B.4 `provider/plugin.py` (NEW, ~150 LOC)

```python
class YandexAlicePlugin(PluginProvider):
    _dialogs_handler: DialogsWebhookHandler | None = None
    _user_id: str = ""

    async def handle_async_init(self) -> None:
        # Load CONF_DIALOG_SKILL_ENABLED, CONF_DIALOG_SKILL_ID, CONF_DIALOG_SKILL_TOKEN,
        #      CONF_DIALOG_WEBHOOK_SECRET, CONF_EXPOSED_PLAYERS, CONF_EXPOSED_PLAYLISTS,
        #      CONF_EXTERNAL_BASE_URL.
        ...

    async def loaded_in_mass(self) -> None:
        if self._dialog_skill_enabled and self._dialog_webhook_secret:
            self._dialogs_handler = DialogsWebhookHandler(
                mass=self.mass,
                skill_id=self._dialog_skill_id,
                skill_token=self._dialog_skill_token,
                webhook_secret=self._dialog_webhook_secret,
                exposed_player_ids=self._exposed_player_ids,
            )
            self._dialogs_handler.register_routes()

    async def unload(self, is_removed: bool = False) -> None:
        if self._dialogs_handler:
            await self._dialogs_handler.unregister_routes()
```

No cloud-relay, no direct-mode HTTP routes for Smart Home, no state notifier — the voice skill is webhook-only, always direct-mode. (Cloud-relay was a convenience for smart-home device sync; the dialog skill in Yandex always points at a public webhook URL.)

### B.5 `provider/constants.py` (NEW, ~50 LOC)

Subset to keep — voice only:

```python
CONF_INSTANCE_NAME = "instance_name"
CONF_EXTERNAL_BASE_URL = "external_base_url"
CONF_EXPOSED_PLAYERS = "exposed_players"
CONF_EXPOSED_PLAYLISTS = "exposed_playlists"
CONF_AUTH_X_TOKEN = "auth_x_token"
CONF_DIALOG_SKILL_ENABLED = "dialog_skill_enabled"
CONF_DIALOG_SKILL_NAME = "dialog_skill_name"
CONF_DIALOG_SKILL_ID = "dialog_skill_id"
CONF_DIALOG_SKILL_TOKEN = "dialog_skill_token"
CONF_DIALOG_WEBHOOK_SECRET = "dialog_webhook_secret"
CONF_DIALOG_AUTO_CREATE_ARTIFACTS = "dialog_auto_create_artifacts"
CONF_DIALOG_AUTO_CREATE_SESSION_ID = "dialog_auto_create_session_id"
CONF_ACTION_AUTO_CREATE_DIALOG = "auto_create_dialog_skill"
CONF_ACTION_RENAME_DIALOG_SKILL = "rename_dialog_skill"

DIALOG_WEBHOOK_BASE_PATH = "/api/yandex_dialogs/webhook"
DIALOG_RESOLVE_TIMEOUT = 2.5
DIALOG_DEFAULT_NAME = "Music Assistant"
DIALOG_CHANNEL = os.environ.get("MA_YANDEX_DIALOG_CHANNEL", "aliceSkill")
DIALOG_NAME_MIN_LEN = 2
DIALOG_NAME_MAX_LEN = 64

YANDEX_DIALOGS_DEVELOPER_URL = "https://dialogs.yandex.ru/developer"
YANDEX_OAUTH_URL = "https://oauth.yandex.ru/authorize?response_type=token&client_id=c473ca268cd749d3a8371351a8f2bcbd"
```

Drops everything cloud-, direct-, capability-, OAuth-, and state-reporting-related (~120 LOC dropped).

### B.6 `provider/dialog_config_ui.py` (NEW, ~250 LOC)

Lifts the dialog-skill section out of smarthome's `__init__.py:get_config_entries()` into a dedicated module — `build_entries(values, mass, ...)` returns the tuple of `ConfigEntry` for the voice flow. Auto-create action uses `ya_dialogs_api.auto_create_skill(skill_type="aliceSkill", ...)`. Rename action uses `ya_dialogs_api.auto_rename_dialog_skill(...)`.

### B.7 Verbatim copies

| Source file in smarthome | Destination in alice | Lines |
|---|---|---|
| `provider/dialogs.py` | `provider/dialogs.py` | 1 311 |
| `provider/dialogs_control.py` | `provider/dialogs_control.py` | 468 |
| `provider/dialogs_nlu.py` | `provider/dialogs_nlu.py` | 474 |
| `provider/dialogs_player.py` | `provider/dialogs_player.py` | 313 |
| `provider/playlists.py` | `provider/playlists.py` | 57 |
| `tests/test_dialogs.py` | `tests/test_dialogs.py` | 1 676 |
| `tests/test_dialogs_control.py` | `tests/test_dialogs_control.py` | 484 |
| `tests/test_dialogs_nlu.py` | `tests/test_dialogs_nlu.py` | 292 |
| `tests/test_dialogs_player.py` | `tests/test_dialogs_player.py` | 191 |
| `docs/VOICE_COMMANDS.md` | `docs/VOICE_COMMANDS.md` | — |
| `docs/VOICE_UX_RESEARCH.md` | `docs/VOICE_UX_RESEARCH.md` | — |

Import-rewriting needed in copied files:
- `from ._compat import SecretStr` → `from ya_dialogs_api import SecretStr`
- `from .auto_skill import ...` (only in dialogs.py if any — verify) → `from ya_dialogs_api import ...`

Verify with a grep before copying: voice files import only from `.dialogs_nlu`, `.constants`, `music_assistant_models`, `music_assistant`. They should be near-verbatim safe.

### B.8 `conftest.py`

Copy smarthome's root `conftest.py` (215 LOC of MA stubs) and update one section: the package alias

```python
# Old
import provider as _provider_module
sys.modules.setdefault("music_assistant.providers.yandex_smarthome", _provider_module)
# New
import provider as _provider_module
sys.modules.setdefault("music_assistant.providers.yandex_alice", _provider_module)
```

The MA model stubs (RepeatMode, QueueOption, etc.) are needed unchanged.

### B.9 Skeleton wrappers — DEFERRED to ma-provider-tools

Don't hand-write `pyproject.toml`, `ruff.toml`, `.pre-commit-config.yaml`, `docker-compose.dev.yml`, `scripts/setup.sh`, `scripts/docker-init.sh`, `.github/workflows/{test,pipeline,release,security,docs,backport,sync-*}.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/contributing.md`, `docs/development.md`, `docs/testing.md`, `docs/dev-docker.md`, `docs/index.md`, `docs/known-issues.md`, `docs/configuration.md`, `docs-site/`. Once the entry is registered in `providers.yml` (Phase D), `distribute.yml` auto-creates a PR with all 40+ wrapper files. Manually merge that PR.

The only files we hand-write in this repo are: `provider/*`, `tests/*`, `conftest.py`, `CHANGELOG.md`, `VERSION`, `LICENSE`, `README.md`, `CLAUDE.md`. Wrappers come from the distribute pipeline.

### B.10 Initial release

After distribute PR is merged and CI green:
1. Ensure `VERSION = 1.0.0`.
2. Push to `dev`.
3. `pipeline.yml` reads VERSION, sees the `v1.0.0` tag does not exist, runs gate (test+lint), then triggers `reusable-release.yml` which creates the tag, GitHub release, and syncs to fork.
4. No PyPI publish for the provider itself (consistent with other providers — the provider package is loaded by MA via local symlink, not pip-installed).

---

## Phase C — `ma-provider-yandex-smarthome` cleanup → `2.0.0`

Single PR on `dev`. Changes:

### C.1 Delete

Source:
- `provider/dialogs.py`
- `provider/dialogs_control.py`
- `provider/dialogs_nlu.py`
- `provider/dialogs_player.py`
- `provider/auto_skill.py` (moved to lib)
- `provider/auto_skill_state.py` (moved to lib)
- `provider/auto_skill_logo.png` (moved to lib)
- `provider/_compat.py` (moved to lib)

Tests (explicit list — do NOT use a `test_auto_skill*.py` wildcard, that would also delete `test_auto_skill_ui.py` which stays):
- `tests/test_dialogs.py` (67 tests)
- `tests/test_dialogs_control.py` (35 tests)
- `tests/test_dialogs_nlu.py` (20 tests)
- `tests/test_dialogs_player.py` (15 tests)
- `tests/test_auto_skill.py` (58 tests — moved to lib)
- `tests/test_auto_skill_state.py` (12 tests — moved to lib)
- (`tests/test_auto_skill_ui.py` is RENAMED, not deleted — see C.2)

Docs:
- `docs/VOICE_COMMANDS.md`
- `docs/VOICE_UX_RESEARCH.md`

### C.2 Modify

- `provider/__init__.py` — drop the dialog-skill section of `get_config_entries` (~150 LOC), drop imports of `CONF_DIALOG_*`, `auto_skill_*`, `_compat`, drop `CONF_ACTION_AUTO_CREATE_DIALOG` / `CONF_ACTION_RENAME_DIALOG_SKILL` handler arms. Replace `from .auto_skill import ...` with `from ya_dialogs_api import ...`. Replace `from .auto_skill_state import ...` with `from ya_dialogs_api import ...`. Replace `from ._compat import SecretStr` with `from ya_dialogs_api import SecretStr`.
- `provider/plugin.py` — drop `_dialogs_handler`, drop `from .dialogs import DialogsWebhookHandler`, drop the dialog-handler init/teardown lines. Replace `from ._compat import SecretStr` with `from ya_dialogs_api import SecretStr`. Drop imports of `CONF_DIALOG_*` and `YANDEX_DIALOGS_CALLBACK_BASE`.
- `provider/auto_skill_ui.py` → **rename to `provider/smarthome_config_ui.py`** (verified by grep: file imports only from `.auto_skill_state` and `.constants`, not from `.auto_skill` itself, so the rewrite is one-line: `from .auto_skill_state import SkillCreationArtifacts, SkillCreationState` → `from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState`). Keeps `build_cloud_plus_entries` and `build_direct_entries` for smart-home flow.
- `provider/constants.py` — remove the entire `Dialog skill` section (lines 150-173, ~25 LOC: `CONF_DIALOG_*`, `DIALOG_*`, `CONF_ACTION_AUTO_CREATE_DIALOG`, `CONF_ACTION_RENAME_DIALOG_SKILL`).
- `provider/manifest.json` — `requirements: ["ya-passport-auth==1.3.0", "ya-dialogs-api>=1.0.0"]`.
- `conftest.py` — drop unused stub additions for `RepeatMode`, `QueueOption` (these were only used by voice tests).
- `tests/test_basic.py`, `tests/test_config_actions.py` — drop voice-specific assertions; replace `auto_skill` imports with `ya_dialogs_api`.
- `tests/test_auto_skill_ui.py` → **rename to `tests/test_smarthome_config_ui.py`** (mirrors source rename; 34 tests). Stays in this repo (smart-home flow tests).
- `VERSION` — `1.9.1` → `2.0.0`.
- `CHANGELOG.md` — new `## [2.0.0]` section with **Removed** + **Migration** subsections (template below).
- `README.md` — drop the voice-commands table; add a one-liner pointing to `ma-provider-yandex-alice` for voice control.
- `CLAUDE.md` — drop voice-skill mentions, drop dialogs gotchas, add cross-link to alice repo.

### C.3 CHANGELOG migration template

```markdown
## [2.0.0] — YYYY-MM-DD

### Removed (BREAKING)
- **Yandex Dialogs custom skill (voice)** moved to a new dedicated provider, `ma-provider-yandex-alice`.
  All `dialogs*.py` modules, voice config keys (`CONF_DIALOG_*`), and voice-related auto-create
  flow have been removed from this repository. This repository now focuses exclusively on the
  Yandex Smart Home device-bridge integration.

### Migration

If you previously used the **voice skill** (Dialogs custom skill, `включи джаз на кухне` etc.):

1. Install `ma-provider-yandex-alice` from <https://github.com/trudenboy/ma-provider-yandex-alice>.
2. Remove the `dialog_skill_*` block from your existing Yandex Smart Home provider configuration
   (the keys are gone in 2.0.0 and will be ignored on load).
3. Configure the new provider (Settings → Add Provider → Yandex Alice). Run the auto-create-skill
   action to register a new Dialogs custom skill against your Yandex Passport account, OR
   re-use your existing skill ID/token.
4. Re-expose the players you want voice-controlled (uses the same MA Player Filter UI).

If you only used the **Smart Home device bridge** (no voice skill), no action required — your
existing config is unaffected.

### Changed
- New runtime dependency: `ya-dialogs-api>=1.0.0` (PyPI). Internalises the Yandex Dialogs
  Developer API client previously vendored inline in `provider/auto_skill.py`. No user-visible
  behaviour change.
```

### C.4 Test impact

Counted via `grep -cE '^\s*(def |async def )test_' tests/*.py` (May 2026):

| Bucket | Files | Test funcs | Destination |
|---|---|---|---|
| Voice | `test_dialogs.py` + `test_dialogs_control.py` + `test_dialogs_nlu.py` + `test_dialogs_player.py` | 67 + 35 + 20 + 15 = **137** | move to alice repo |
| Auto-create lib | `test_auto_skill.py` + `test_auto_skill_state.py` | 58 + 12 = **70** | move to ya-dialogs-api repo |
| Smart-home flow UI | `test_auto_skill_ui.py` (renamed → `test_smarthome_config_ui.py`) | **34** | stays in smarthome |
| Smart-home rest | `test_basic.py`, `test_config_actions.py`, `test_device.py`, `test_handlers.py`, `test_direct.py`, `test_cloud.py`, `test_notifier.py`, `test_schema.py` | balance | stays in smarthome |

After cleanup smarthome retains 629 − 137 − 70 = **422 tests** (137 went to alice, 70 to lib). Note that test counts may differ slightly from parametrized expansion at runtime; this is the *source* test-function count.

Coverage on smart-home code is unaffected; the deleted tests cover only the deleted code.

---

## Phase D — `ma-provider-tools`

Single PR on `main`. Changes to `providers.yml`:

### D.1 Add `yandex_alice` entry

Insert between `yandex_smarthome` (ends line 178) and `yandex_ynison` (starts line 180):

```yaml
  - domain: yandex_alice
    display_name: Yandex Alice
    repo: trudenboy/ma-provider-yandex-alice
    default_branch: dev
    manifest_path: provider/manifest.json
    provider_path: provider/
    provider_type: plugin_provider
    locale: ru
    runtime_dependencies:
      - "ya-passport-auth>=1.3.0"
      - "ya-dialogs-api>=1.0.0"
    codespell_ignore_words: "hass,"
    service_url: https://dialogs.yandex.ru/developer
    auth_method: "Yandex Passport (Device Flow)"
    max_quality: "N/A (plugin — не предоставляет аудио)"
    features:
      - label: "Голосовое управление MA через кастомный скилл Алисы"
      - label: "Полная NLU на русском (включи / поставь на паузу / перемешай / повтор / перемотай / переведи / добавь)"
      - label: "Озвучивание сейчас играющего трека"
      - label: "Дисамбигуация колонок голосом (первая / вторая / третья)"
      - label: "Автосоздание Dialog-скилла через Yandex Passport"
    skip_wrappers:
      - sync-from-upstream.yml.j2
      - upstream-pr.yml.j2
      - rebuild-integration.yml.j2
      - sync-kion-from-yandex.yml.j2
```

### D.2 Update `yandex_smarthome` entry

Lines 169-173, replace the features block:

```yaml
    features:
      - label: "Регистрация MA-плееров как устройств в Яндекс УД"
      - label: "on_off / volume / mute / pause через Smart Home API"
      - label: "Автоматическая синхронизация состояния плееров"
      - label: "Cloud-relay через yaha-cloud.ru (без публичного URL)"
      - label: "Direct mode (через MA webserver на публичном URL)"
```

(Drop "Голосовое управление MA-плеерами через Алису" — that's now alice.)

Add to `runtime_dependencies` (line 163):

```yaml
    runtime_dependencies:
      - "ya-passport-auth>=1.2.3"
      - "ya-dialogs-api>=1.0.0"
```

### D.3 README.md update

`/Users/renso/Projects/ma-provider-tools/README.md` (lines 16-22) — add a row for Yandex Alice in the providers table. Update Yandex Smart Home row description to drop "voice".

### D.4 Trigger distribute

Push the PR. Merging to `main` triggers `.github/workflows/distribute.yml` which:
1. Detects `providers.yml` changed.
2. Runs `scripts/distribute.py` for each provider.
3. For `yandex_alice` (new repo): clones the new repo, renders all 40+ wrapper templates against the alice context, commits to `chore/update-workflow-wrappers` branch, force-pushes, opens a PR.

### D.5 First-time setup of new repo

After distribute PR appears in alice repo:
1. Set `FORK_SYNC_PAT` secret in `trudenboy/ma-provider-yandex-alice` repo settings (PAT with `contents:write` scope on `trudenboy/ma-server`).
2. Merge the distribute PR. CI workflows now exist.
3. Push the hand-written `provider/`, `tests/`, `conftest.py`, etc. to a feature branch on alice; open PR; CI runs; merge.

---

## Sequencing (atomic, but with hard ordering)

```
T-1 Manual: register ya-dialogs-api on PyPI + TestPyPI; configure Trusted Publisher
     mapping for trudenboy/ya-dialogs-api → release.yml; create GitHub Environments
     pypi + testpypi in the new repo.

T0  Create ya-dialogs-api repo (default branch: main). Make initial empty commit
     so `main` exists (required for PRs and CI).
T0  Bootstrap from ya-passport-auth template — copy skeleton, retarget identifiers
     (ya_passport_auth → ya_dialogs_api, package name in pyproject, README, etc.).
     Add the new VERSION-vs-tag guard step at the top of release.yml (template
     does not have it — template uses hatch-vcs). Set `requires` in pyproject
     to drop hatch-vcs.
T0  PR-A1 in ya-dialogs-api: lib source (api_client.py, state.py, _compat.py,
     assets/default_logo.png, py.typed), tests (with fake authenticator yielding
     an aiohttp session populated by aioresponses), README, LICENSE, NOTICE,
     SECURITY.md, CHANGELOG (with [1.0.0rc1] entry), CLAUDE.md, PLAN.md,
     pyproject.toml, VERSION=1.0.0rc1, .github/workflows/{ci,release,codeql,
     scorecard,secrets-nightly}.yml, .pre-commit-config.yaml, .gitignore, uv.lock.
T0  PR-A1 CI green (ci.yml lint+typecheck+test matrix). Merge to main.
T0  Tag v1.0.0rc1 → release.yml: VERSION-vs-tag guard passes → uv build → Sigstore
     sign → TestPyPI publish via OIDC (environment: testpypi). Smoke-install
     v1.0.0rc1 in a clean venv against TestPyPI.
T0  Bump VERSION to 1.0.0 in a follow-up commit on main; update CHANGELOG to
     [1.0.0]. Tag v1.0.0 → release.yml → PyPI publish via OIDC (environment: pypi).
     ya-dialogs-api==1.0.0 is live.
    [BLOCKING] alice and smarthome cannot release until this point is reached.

T1  Create ma-provider-yandex-alice repo (default branch: dev — provider convention).
     IMPORTANT: push an initial commit on `dev` (e.g. minimal README) — distribute.py
     opens a PR against `dev`, which fails if the branch doesn't exist.
T1  PR-B1 (skeleton): minimal provider/manifest.json (with requirements pointing at
     ya-dialogs-api>=1.0.0) + VERSION=1.0.0 + minimal README. CI is not yet present
     in this repo, so this PR can be merged without checks.
T1  Submit PR-D1 to ma-provider-tools: add yandex_alice entry + update yandex_smarthome
     features (drop voice items) + add ya-dialogs-api>=1.0.0 to smarthome runtime_dependencies.
T1  Merge PR-D1 → distribute.yml fires → opens chore/update-workflow-wrappers PR in alice repo.
T1  Set FORK_SYNC_PAT secret in alice repo (PAT with contents:write on trudenboy/ma-server).
     Merge the distribute PR. CI workflows are now present in alice repo.

T2  PR-B2 in alice: full provider/ source — copied dialogs*.py (with `from ._compat import
     SecretStr` rewritten to `from ya_dialogs_api import SecretStr`), written __init__.py /
     plugin.py / constants.py / dialog_config_ui.py / ma_authenticator.py, copied playlists.py
     and test_dialogs*.py; conftest.py adapted (alias yandex_smarthome → yandex_alice);
     CHANGELOG [1.0.0]; CLAUDE.md; README.md customized from distribute template.
T2  CI green (test.yml runs full pytest against the source — verifies ya-dialogs-api lib
     resolves correctly via pip from PyPI). Merge → pipeline.yml tags v1.0.0 → first
     release of ma-provider-yandex-alice.

T3  PR-C1 in smarthome: delete voice files + voice config + auto_skill.py / auto_skill_state.py
     / auto_skill_logo.png / _compat.py; RENAME auto_skill_ui.py → smarthome_config_ui.py
     (and tests/test_auto_skill_ui.py → tests/test_smarthome_config_ui.py); rewrite imports
     (from .auto_skill → from ya_dialogs_api, from .auto_skill_state → from ya_dialogs_api,
     from ._compat → from ya_dialogs_api); add ma_authenticator.py; bump VERSION
     to 2.0.0; CHANGELOG migration entry; manifest.json adds ya-dialogs-api>=1.0.0.
T3  CI green (422 tests) → merge → pipeline.yml tags v2.0.0 → release of
     ma-provider-yandex-smarthome 2.0.0.

T4  Manual: end-to-end probe both providers on real Alice (see verification).
T4  Manual: respond to user issues / PR comments if any.
```

The phases are atomic in the sense that all three published versions are coordinated and the CHANGELOG entries reference each other. They are sequenced because PyPI must publish first, alice must register before voice users have somewhere to migrate to, smarthome 2.0.0 only ships after alice is verified in production.

---

## Critical files

| Repo | File | Action | Notes |
|---|---|---|---|
| ya-dialogs-api | `pyproject.toml` | CREATE | hatchling+VCS; name=ya-dialogs-api; deps=aiohttp,yarl,ya-passport-auth; dev extras incl. bandit/pip-audit/cyclonedx/liccheck |
| ya-dialogs-api | `src/ya_dialogs_api/api_client.py` | COPY+REFACTOR from smarthome auto_skill.py | drop `mass: MusicAssistant`; delete `_default_authenticator` (moves to provider) + `_resolve_base_url` + `derive_*` + `check_preconditions`; require pre-computed URLs as kwargs; rewrite logo path to importlib.resources. ~700 LOC dropped |
| ya-dialogs-api | `src/ya_dialogs_api/state.py` | COPY from smarthome auto_skill_state.py | verbatim |
| ya-dialogs-api | `src/ya_dialogs_api/_compat.py` | COPY | verbatim (SecretStr) |
| ya-dialogs-api | `src/ya_dialogs_api/assets/default_logo.png` | COPY from auto_skill_logo.png | binary |
| ya-dialogs-api | `src/ya_dialogs_api/py.typed` | CREATE | empty PEP 561 marker |
| ya-dialogs-api | `tests/test_api_client.py`, `tests/test_state.py` | COPY + rewrite imports | replace MA mocks with fake authenticator + aioresponses; drop MA stubs entirely |
| ya-dialogs-api | `e2e_all_flows.py` | CREATE | runnable E2E script (pattern from ya-passport-auth) |
| ya-dialogs-api | `.github/workflows/{ci,release,codeql,scorecard,secrets-nightly}.yml` | COPY from ya-passport-auth | retarget package/repo names; pinned action SHAs unchanged |
| ya-dialogs-api | `LICENSE`, `NOTICE`, `SECURITY.md`, `.pre-commit-config.yaml`, `.gitignore` | COPY from ya-passport-auth | unchanged |
| ya-dialogs-api | `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `PLAN.md` | CREATE | repo-specific content |
| ya-dialogs-api | `uv.lock` | GENERATE | `uv sync --extra dev` then commit |
| ma-provider-yandex-alice | `provider/manifest.json` | CREATE | domain=yandex_alice, requirements include ya-dialogs-api>=1.0.0 |
| ma-provider-yandex-alice | `provider/__init__.py` | CREATE (~250 LOC) | voice-only get_config_entries |
| ma-provider-yandex-alice | `provider/plugin.py` | CREATE (~150 LOC) | wires DialogsWebhookHandler only |
| ma-provider-yandex-alice | `provider/constants.py` | CREATE (~50 LOC) | voice subset |
| ma-provider-yandex-alice | `provider/dialog_config_ui.py` | CREATE (~250 LOC) | lifted from smarthome __init__.py |
| ma-provider-yandex-alice | `provider/ma_authenticator.py` | CREATE (~80 LOC) | wraps ya-passport-auth Device Flow + hosts MA-flavored activation HTML page on mass.webserver; yields authorized aiohttp.ClientSession to ya_dialogs_api.auto_create_skill |
| ma-provider-yandex-alice | `provider/dialogs*.py` (4 files) | COPY VERBATIM | rewrite `from ._compat import SecretStr` → `from ya_dialogs_api import SecretStr` |
| ma-provider-yandex-alice | `provider/playlists.py` | COPY VERBATIM | |
| ma-provider-yandex-alice | `tests/test_dialogs*.py` (4 files) | COPY VERBATIM | rewrite SecretStr import |
| ma-provider-yandex-alice | `conftest.py` | COPY + adapt | rename package alias to yandex_alice |
| ma-provider-yandex-alice | `VERSION` | CREATE | 1.0.0 |
| ma-provider-yandex-alice | `CHANGELOG.md` | CREATE | starts with [1.0.0] |
| ma-provider-yandex-smarthome | `provider/dialogs.py`, `dialogs_control.py`, `dialogs_nlu.py`, `dialogs_player.py` | DELETE | 4 files (explicit, not wildcard) |
| ma-provider-yandex-smarthome | `provider/auto_skill.py`, `auto_skill_state.py`, `_compat.py`, `auto_skill_logo.png` | DELETE | moved to ya-dialogs-api lib |
| ma-provider-yandex-smarthome | `provider/auto_skill_ui.py` → `provider/smarthome_config_ui.py` | RENAME + EDIT | rewrite `from .auto_skill_state import ...` → `from ya_dialogs_api import ...` |
| ma-provider-yandex-smarthome | `provider/__init__.py` | EDIT | drop voice section + rewrite imports |
| ma-provider-yandex-smarthome | `provider/plugin.py` | EDIT | drop dialog handler + rewrite imports |
| ma-provider-yandex-smarthome | `provider/constants.py` | EDIT | drop dialog skill section |
| ma-provider-yandex-smarthome | `provider/ma_authenticator.py` | CREATE | identical to alice's copy (Device Flow + activation HTML page on mass.webserver) |
| ma-provider-yandex-smarthome | `provider/manifest.json` | EDIT | add ya-dialogs-api>=1.0.0 to requirements |
| ma-provider-yandex-smarthome | `tests/test_dialogs.py`, `test_dialogs_control.py`, `test_dialogs_nlu.py`, `test_dialogs_player.py` | DELETE | 4 voice test files (explicit list) |
| ma-provider-yandex-smarthome | `tests/test_auto_skill.py`, `test_auto_skill_state.py` | DELETE | moved to lib (NOT a wildcard — `test_auto_skill_ui.py` stays) |
| ma-provider-yandex-smarthome | `tests/test_auto_skill_ui.py` → `tests/test_smarthome_config_ui.py` | RENAME | mirrors source rename |
| ma-provider-yandex-smarthome | `VERSION` | EDIT | 1.9.1 → 2.0.0 |
| ma-provider-yandex-smarthome | `CHANGELOG.md` | EDIT | add [2.0.0] migration section |
| ma-provider-yandex-smarthome | `README.md`, `CLAUDE.md` | EDIT | drop voice mentions; cross-link alice |
| ma-provider-tools | `providers.yml` | EDIT | add yandex_alice entry; update yandex_smarthome features+deps |
| ma-provider-tools | `README.md` | EDIT | add row for Yandex Alice; update Yandex Smart Home row |

---

## Risks / open questions

1. **PyPI Trusted Publisher setup is one-time manual.** The user must register the project on PyPI (`ya-dialogs-api`) AND configure Trusted Publisher mapping for `trudenboy/ya-dialogs-api` + `release.yml` workflow before the first release. Document this as a checklist before executing Phase A. Fallback: `TWINE_API_TOKEN` secret, but Trusted Publisher is the modern recommendation.

2. **Package name collision.** `ya-dialogs-api` is the proposed PyPI name (verified free in research, May 2026). Sanity-check on the day of the release: `curl -fsS https://pypi.org/pypi/ya-dialogs-api/json` should still 404. If taken by then, fall back order: `ya-dialogs` → `yandex-dialogs-api` → `ya-dialogs-client`. Update manifest `requirements` in alice + smarthome accordingly.

3. **Existing voice users have a hard cutover.** Smarthome 2.0.0 deletes voice immediately. Users updating without reading CHANGELOG will see an empty config UI for the dialog skill section. Mitigation: bold migration block in CHANGELOG; potentially a one-shot deprecation log line in the smarthome `__init__.py:setup` for one minor before 2.0.0 — but the user picked atomic, so we accept the cutover.

4. **`auto_skill_ui.py` stays in smarthome — naming becomes weird.** It's the Smart Home config-flow entries builder. Rename to `smarthome_config_ui.py` in 2.0.0 to match the new structure (small refactor, mechanical).

5. **`conftest.py` MA stubs duplication.** Both alice and smarthome maintain near-identical conftest stubs (~215 LOC each). Could become its own shared dev-only package later. For now, accept the duplication — they evolve at different rates.

6. **Auto-create-skill API symmetry.** The shared lib's `auto_create_skill(skill_type=...)` keeps both branches (smartHome / aliceSkill). Consumers pass the right value. No coupling between branches at runtime.

7. **First-CI ordering on the new alice repo.** Distribute creates a PR that adds CI workflows but does NOT itself run those workflows. The first time provider CI actually runs is on PR-B2 (the source PR). Mitigation: set `FORK_SYNC_PAT` secret in alice repo settings AFTER merging the distribute PR but BEFORE opening PR-B2. The source PR's CI then has all secrets it needs (test+pipeline+release+sync). The distribute PR itself only triggers the basic `test.yml` after merge to `dev`, which doesn't need FORK_SYNC_PAT.

8. **MA's manifest `requirements` resolver.** Verify that MA's provider loader correctly installs the new `ya-dialogs-api` PyPI package alongside `ya-passport-auth`. If MA only supports a single requirement per provider, that's a blocker — but the smarthome manifest already has `["ya-passport-auth==1.3.0"]` and works fine, so multi-element `requirements` is supported.

9. **Domain name choice — `yandex_alice` vs `yandex_dialogs`.** The user said "yandex alice" / `ma-provider-yandex-alice`. Going with `yandex_alice`. (`yandex_dialogs` would be more accurate to the API name, but Alice is the user-visible product.)

10. **`docs-site/` is provider-internal.** Each provider has its own Astro docs site. Alice gets one auto-generated by distribute. No cross-repo docs work needed.

---

## Verification

### V.1 Lib release

```bash
# Local — match the CI commands exactly
cd /Users/renso/Projects/ya-dialogs-api
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q --cov=ya_dialogs_api --cov-report=term-missing
uv run python e2e_all_flows.py     # optional — uses a throwaway Yandex account

# Build sanity
uv build
uv run python -c "import ya_dialogs_api; print(ya_dialogs_api.__version__)"

# Pre-flight TestPyPI release
git tag v1.0.0rc1 && git push --tags
# CI release.yml → uv build → Sigstore sign → TestPyPI publish via OIDC
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple ya-dialogs-api==1.0.0rc1
# Smoke test the install in a fresh venv

# Stable release
git tag v1.0.0 && git push --tags
# CI release.yml → uv build → Sigstore sign → PyPI publish via OIDC
pip index versions ya-dialogs-api  # confirms 1.0.0 is published
```

### V.2 Alice provider — local smoke test

```bash
cd /Users/renso/Projects/ma-provider-yandex-alice
uv venv && source .venv/bin/activate
uv pip install -e .[test]
pytest -q   # 137 voice test funcs should pass (C.4)
ruff check provider tests
mypy --strict
docker compose -f docker-compose.dev.yml up
# UI: http://localhost:8095, add provider "Yandex Alice", run auto-create
```

### V.3 Smarthome 2.0.0 — local smoke test

```bash
cd /Users/renso/Projects/ma-provider-yandex-smarthome
git checkout dev && git pull
uv pip install -e .[test]
pytest -q   # ~422 smart-home tests (dropped from 629; see C.4)
ruff check provider tests
mypy --strict
# Manually load existing config that has dialog_skill_enabled=true; confirm graceful no-op
# (the config keys are removed; MA's config loader should ignore unknown keys, not crash)
```

### V.4 End-to-end on real Alice

After both providers ship and are loaded in user's MA instance:

- Smart Home (smarthome 2.0.0): «Алиса, поставь на паузу Кухню» → Smart Home capability action arrives → MA pauses player. Verify state reports back.
- Voice (alice 1.0.0): «Алиса, попроси Music Assistant включи джаз на кухне» → custom skill webhook arrives → MA plays jazz on Kitchen. Verify state cache, ordinal disambiguation, all six v1.9 commands (now_playing / shuffle / repeat / seek / transfer / add-to-queue).

### V.5 Distribute pipeline

```bash
cd /Users/renso/Projects/ma-provider-tools
git checkout main
# Verify alice entry present in providers.yml; smarthome entry updated
git log --oneline -5
# In trudenboy/ma-provider-yandex-alice: verify chore/update-workflow-wrappers PR exists, all 40+ files generated
gh pr list --repo trudenboy/ma-provider-yandex-alice
```

### V.6 Cross-link sanity

- Smarthome README mentions alice repo URL.
- Alice README mentions smarthome repo URL (for Smart Home device bridge users).
- Smarthome 2.0.0 CHANGELOG migration block links to alice install instructions.
- ma-provider-tools README table has both rows with correct service_url + features.

---

## Why this order works

- **Lib first** — providers' manifest `requirements` reference `ya-dialogs-api>=1.0.0`; PyPI must have this version published before pip can resolve it inside MA's container at provider load. Lib has no MA dependency itself, so its CI is fast and self-contained.
- **Alice second** — register in providers.yml + push source. Existing voice users haven't lost anything yet (smarthome still ships voice in 1.9.1).
- **Smarthome last** — only delete voice after alice is verified working in user's environment. CHANGELOG points migrating users at alice. If alice has a regression discovered post-merge, smarthome 1.9.1 is still installable as a rollback option until alice is patched.
- **Distribute PR auto-handles 90 % of skeleton** — pyproject, ruff, pre-commit, CI workflows, Docker, scripts come from `ma-provider-tools/wrappers/*.j2`. Hand-writing limited to the actual provider source + tests + conftest + CHANGELOG.

---

## Audit log (May 2026 self-review)

The following defects were found and fixed during a structured audit of an earlier draft of this plan. They are recorded here so the same mistakes are not re-introduced if the plan is rewritten.

| # | Defect | Where | Fix |
|---|---|---|---|
| 1 | `pyproject.toml` section header still said "hatchling+VCS, mirrors ya-passport-auth" after the user switched to VERSION-file versioning | A.4 header | Renamed header to "hatchling + VERSION file" |
| 2 | Risk-list fallback PyPI name was identical to the primary name (`ya-dialogs-api` → `ya-dialogs-api`) | Risk #2 | Replaced with proper fallback chain `ya-dialogs` → `yandex-dialogs-api` → `ya-dialogs-client` |
| 3 | Test-count math was wrong (290 + 50 ≠ 289 remaining) | C.4 | Recounted via grep on the current source: voice = 137 funcs, lib-bound = 70 funcs, smarthome retains **422** funcs |
| 4 | Delete list used wildcard `tests/test_auto_skill*.py` which would also delete `tests/test_auto_skill_ui.py` (which must STAY in smarthome) | C.1 | Replaced with explicit list; called out the rename of `test_auto_skill_ui.py` → `test_smarthome_config_ui.py` |
| 5 | Architecture tree omitted `provider/ma_authenticator.py` from alice — alice's auto_create_skill action needs the Device Flow authenticator just like smarthome does | Architecture diagram + Critical files | Added `ma_authenticator.py` to alice provider/ tree and to the Critical files table. (Originally was `ma_webserver.py` — superseded by the architectural revision dropping `WebserverAdapter`, see audit row #14.) |
| 6 | `auto_skill_ui.py` rename was inconsistent — risks section said "rename to smarthome_config_ui.py" but C.2 said "otherwise unchanged" | C.2 + Architecture | Committed to the rename in C.2 with explicit `provider/auto_skill_ui.py` → `provider/smarthome_config_ui.py` and matching test rename. Verified by grep that this file imports only from `.auto_skill_state` and `.constants` (not `.auto_skill`), so the rewrite is one line |
| 7 | `release.yml` was described as "copied verbatim from ya-passport-auth", but ya-passport-auth uses hatch-vcs (no VERSION/tag check needed). Our VERSION-vs-tag guard step is NEW and needs explicit authoring | A.8 + Sequencing T0 | Marked the guard step as **NEW (not in template)** in A.8 and noted it in T0 bootstrap |
| 8 | `secrets-nightly.yml` description claimed it does pip-audit + bandit + secret scanning. ya-passport-auth's actual workflow only does secret scanning; the other tools are in `ci.yml` | A.8 | Corrected description |
| 9 | `on_progress` and `on_artifacts_change` callbacks were typed `Callable[..., None]` (sync) — but the provider's persistence (`mass.config.set_provider_config_value`) is awaitable. Forcing sync would prevent the lib from awaiting writes | A.6 | Changed both callback types to `Callable[..., Awaitable[None]]` and added a comment about why |
| 10 | `SkillCreationArtifacts` (used by `auto_skill_ui.py` in smarthome) was missing from the lib's public API exports | A.5 `__init__.py` + `__all__` | Added it to both |
| 11 | Sequencing T1 didn't account for the fact that the alice repo must have an initial commit on `dev` before distribute.py can open a PR against it | Sequencing T1 | Added explicit "push initial commit on dev" step |
| 12 | LOC estimates for alice's `__init__.py` (250) + `dialog_config_ui.py` (250) double-counted what was originally ~150 LOC of dialog-skill flow in smarthome's `__init__.py` | Architecture + B.3 + B.6 | Reduced to 200 + 150 LOC |
| 13 | Verification step `pytest -q   # ~340 smart-home tests` contradicted the corrected 422-test count from C.4 | V.3 | Updated to "~422 smart-home tests" |
| 14 | **Architectural overreach: `WebserverAdapter` Protocol** — early plan introduced a Protocol so the lib could "register HTTP routes". This was unjustified: the lib only needed a webserver to host the Device Flow activation HTML page, which is **MA-specific UX**, not part of the Yandex Dialogs API. Sister library `ya-passport-auth` doesn't do this — it exposes Device Flow via an `on_code` callback and lets the caller render the UX. | A.5/A.6/A.7 + Architecture + Critical files | Removed `webserver.py` from the lib entirely. Lib's `auto_create_skill` now requires an `authenticator` context-manager parameter (no default) producing an authorized aiohttp session. Provider gains `ma_authenticator.py` (~80 LOC) that wraps `ya-passport-auth.login_device_code` + hosts the MA-flavored HTML page on `mass.webserver`. Net: lib stays in its actual niche (post-auth dev-console client), MA-specific UX stays where it belongs. |

The following defects were found in the audit but NOT fixed because they are intentional choices, not errors:

- **Risk #9 — domain `yandex_alice` vs `yandex_dialogs`.** The user explicitly said "yandex alice" and `ma-provider-yandex-alice`; we follow the directive even though `yandex_dialogs` matches the API more accurately.
- **Risk #5 — conftest.py MA-stubs duplication.** Acceptable for now; not blocking the split.
- **Risk #6 — auto_create_skill keeps both branches in the lib.** That's the whole point of having a generic library serving both providers.

The following items were verified during plan finalization (read-only, May 2026):

- **V1 ✓ RESOLVED**: `auto_create_skill` and `auto_rename_dialog_skill` both return `SkillCreationArtifacts` (the state-machine dataclass), NOT a separate result type. **Lib API change**: drop the proposed `SkillCreationResult` dataclass — re-use `SkillCreationArtifacts` as the return type. Caller reads `artifacts.skill_id`, `artifacts.skill_token`, `artifacts.webhook_secret` from the returned state. Functions never raise — failures land as `state=FAILED` + `last_error` on the returned artifacts.
- **V2 ✓ RESOLVED**: `auto_skill.py` does NOT import `yarl`. Drop `yarl>=1.12` from `[project.dependencies]`.
- **V3 — STILL PENDING**: smoke test on a fresh MA install with two-element `requirements` is prudent. Existing smarthome's `["ya-passport-auth==1.3.0"]` already proves single-element works; PyPI pip resolution of two PyPI packages is standard and should not pose issues.
- **V4 ✓ RESOLVED**: `auto_skill.py` does NOT call `mass.config.set_provider_config_value` or `mass.config.get_provider_config_value` directly. Persistence is already delegated through the `progress_cb: Callable[[SkillCreationArtifacts], Awaitable[None]]` callback; the caller (provider's `__init__.py`) handles MA config I/O. The lib's contract was already correctly async-callback-shaped — my "fix" to make it `Awaitable` was preserving an existing property, not changing it.

**Implication for refactor scope** (revised May 2026 after audit row #14): the cleaner architecture is to drop `_default_authenticator` and the `WebserverAdapter` Protocol entirely from the lib — the activation-page UX is MA-specific and belongs in the provider, not the dev-console API client. The MA-decoupling work for the lib is now ~700 LOC of *deletion* (move out, not move-and-keep). See A.6 for the post-revision API.

Note: the `Literal["smart_home", "dialog"]` (with underscore) matches the current code — NOT `"smartHome" / "aliceSkill"` as I originally wrote. The plan's `SkillType` Literal must be updated to match.
