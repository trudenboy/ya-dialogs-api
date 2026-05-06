# Security Policy

## Reporting a vulnerability

If you discover a security issue, **please do not open a public issue.**

Instead, email the maintainer directly or use GitHub's private vulnerability
reporting feature at:
https://github.com/trudenboy/ya-dialogs-api/security/advisories/new

You will receive an acknowledgement within 72 hours and a resolution timeline
within 7 days.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |

## Threat model summary

This library is a relatively thin HTTP client over the Yandex Dialogs developer
API. The hot security perimeter (Yandex Passport credentials, OAuth tokens,
session cookies) is owned by `ya-passport-auth`, which has its own threat
model. The threats specific to this library are:

| # | Threat | Mitigation |
|---|--------|------------|
| D1 | Skill credentials leak via logs (skill_id is sensitive enough that an attacker who learns it can call dialogs.yandex.net/api/v1/skills/{id}/callback/state if they also have skill_token) | Library-side: never logs full credentials; only logs `step` + status code + first 200 chars of error body, which never contain skill_token |
| D2 | DoS via unbounded HTML response from dev-console | 2 MiB hard cap on developer page HTML (`_MAX_HTML_RESPONSE_BYTES`) |
| D3 | ReDoS on CSRF regex | Single explicit character class, non-greedy. The regex matches inside a 2 MiB cap |
| D4 | Spoofed dev-console responses (e.g. on a malicious proxy) | TLS verification is the caller's responsibility on the supplied `aiohttp.ClientSession` (this library does not own the session) |
| D5 | CSRF token reuse across attempts | `fetch_csrf` is called once per attempt; orchestrator does not cache the token across `auto_create_skill` calls |
| D6 | Pickled `SkillCreationArtifacts` carrying secrets | Artifacts dataclass is frozen but does not currently carry secrets — only IDs (skill_id, logo_id, oauth_app_id). The actual `skill_token` is owned by the caller and never enters the artifacts object |
| D7 | Supply-chain compromise via this library | Hash-pinned `uv.lock`, `pip-audit` in CI, Dependabot enabled, OpenSSF Scorecard published |
| D8 | Real Yandex tokens in test fixtures | All test data uses synthetic tokens (`csrf-token`, `test-secret`, `skill-id-123`); no production credentials in source tree |

For the security of the Passport authentication flow, OAuth Device Flow,
SecretStr handling, host allow-list, and TLS verification, see the threat model
of [`ya-passport-auth`](https://github.com/trudenboy/ya-passport-auth) — this
library uses it as a runtime dependency.

## Out of scope

- Host compromise / memory scraping
- Side-channel timing attacks
- Encrypted at-rest storage of artifacts (caller responsibility)
- The undocumented Yandex API itself — if Yandex changes endpoint shapes or
  auth requirements, this library's release cadence is the mitigation
- CAPTCHA / anti-bot bypass
