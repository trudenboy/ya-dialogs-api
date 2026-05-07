# Research: Yandex Dialogs Developer API — глубинное исследование платформы

**Цель:** провести систематическую разведку платформы `dialogs.yandex.ru/developer`,
её приватного API `developer-api/v2`, обнаружить новые/недостающие эндпоинты,
выявить риски в текущей имплементации `ya-dialogs-api` и сформировать
приоритизированный backlog доработок.

**Скоуп:** только developer-api/v2 (provisioning). Runtime-API (входящие webhook'и
от Алисы) — за рамками исследования.

**Метод:** четыре фазы.

| Фаза | Описание | Артефакт | Статус |
|------|----------|----------|--------|
| 1 | Публичная разведка: документация, OSS, baseline покрытия | секции 1–3 | ⏳ in progress |
| 2 | Подготовка Playwright-сценария | секция 4 | ☐ pending |
| 3 | Авторизованная Playwright-сессия с захватом сетевого трафика | секция 5 | ☐ pending |
| 4 | Синтез + приоритизированный backlog | секция 6 | ☐ pending |

**Дата начала:** 2026-05-06.
**Версия библиотеки на момент старта:** 1.0.0.

---

## 1. Baseline: что покрыто в `ya-dialogs-api` 1.0.0

Источник — `src/ya_dialogs_api/api_client.py` на коммите `a42d571`.

### 1.1. Покрытые эндпоинты

| # | Метод | Путь | Метод-обёртка | Назначение |
|---|-------|------|---------------|------------|
| 1 | GET   | `/developer` (HTML) | `fetch_csrf` | Извлечение CSRF (`secretkey`) regex'ом |
| 2 | GET   | `/developer/app-store-api/snapshot` | `list_existing_skills` | Список навыков пользователя |
| 3 | POST  | `/developer/app-store-api/apps` | `create_app` | Создание навыка (channel: `smartHome` / `aliceSkill`) |
| 4 | POST  | `/developer/app-store-api/apps/{id}/draft/upload-logo` | `upload_logo` | Загрузка PNG-логотипа (multipart) |
| 5 | PATCH | `/developer/app-store-api/apps/{id}/draft/update` | `update_draft` | Обновление черновика |
| 6 | POST  | `/developer/app-store-api/oauth/apps` | `create_oauth_app` | Создание OAuth-приложения для account-linking |
| 7 | POST  | `/developer/app-store-api/apps/{id}/oauthApp` | `attach_oauth` | Привязка OAuth-приложения к навыку |
| 8 | POST  | `/developer/app-store-api/apps/{id}/draft/request-deploy` | `request_deploy` | Запрос публикации/модерации |

### 1.2. Захардкоженные значения payload'ов (для проверки в Phase 3)

В `build_smart_home_draft_payload`:
- `voice = "shitova.us"`
- `category = "smart_home"` (внутри `publishingSettings.category`)
- `enableAllAvailableRegions = True`
- `selectedRegions = []`
- `skillAccess = "private"`
- `hideInStore = False`
- `multilingualSettings.ru.{name, secondaryTitle, externalSettingsDescription, supportedUnitsDescription}` — все равны `skill_name`

В `build_dialog_draft_payload`:
- `voice = "good_oksana"` (default)
- `category = "music_audio"` (default)
- `hideInStore = True` (default)
- `skillAccess = "private"`
- `useStateStorage = False`
- `requiredInterfaces / exactSurfaces / surfaceWhitelist / surfaceBlacklist = []`
- `appMetricaApiKey = ""`
- `rsyPlatformId = ""`
- Дефолтный `structuredExamples`: один элемент `{"marker": "попроси", "activationPhrase": skill_name, "request": "включи что-нибудь", "is_valid": True}`
- Дефолтный `activationPhrases = [skill_name]`

В `build_oauth_app_payload`:
- `scope = ""`
- `yandexClientId = ""`
- `refreshTokenUrl = token_url` (тот же URL)

### 1.3. Что НЕ покрыто (предварительный список — уточним после Playwright)

**Жизненный цикл навыка:**
- 🟥 GET текущего черновика / опубликованной версии (`GET /apps/{id}/draft`?)
- 🟥 GET статуса модерации / on-air (`GET /apps/{id}/release/status`?)
- 🟥 Удаление навыка (`DELETE /apps/{id}`?)
- 🟥 Отзыв с публикации / снятие с витрины (`unpublish` / `withdraw`?)
- 🟥 Откат к предыдущей версии (rollback)
- 🟥 Список версий навыка / история деплоев

**OAuth-приложения:**
- 🟥 GET существующих OAuth-приложений
- 🟥 Обновление OAuth-приложения (`PATCH /oauth/apps/{id}`?)
- 🟥 Удаление OAuth-приложения
- 🟥 Отвязка OAuth от навыка

**Тестирование/отладка:**
- 🟥 Тестовый запрос к webhook (есть встроенный «Тестировать» в консоли)
- 🟥 Просмотр последних запросов / логов
- 🟥 Запросы с указанной поверхности (Алиса/Станция/мобильное)

**Smart Home специфика:**
- 🟥 Список зарегистрированных устройств тестового аккаунта
- 🟥 Discovery-запрос на лету
- 🟥 Логи команд от Алисы

**Аналитика:**
- 🟥 Статистика показов / запусков
- 🟥 Рейтинги и отзывы

**Контент:**
- 🟥 Загрузка демо-видео/скриншотов (картинок Visual UI)
- 🟥 Управление фразами активации (CRUD отдельно от draft.update)
- 🟥 Управление NLU-интентами / слотами (для dialog)
- 🟥 Бета-тестеры / список доступа (`skillAccess = "private"` — кто в whitelist?)

**Метаданные платформы:**
- 🟥 Список доступных голосов TTS
- 🟥 Список категорий каталога
- 🟥 Список поддерживаемых поверхностей (surfaces)
- 🟥 Список интерфейсов (`requiredInterfaces`)

### 1.4. Робастность / DX-замечания (из чтения кода)

| # | Замечание | Файл/строка | Серьёзность |
|---|-----------|-------------|-------------|
| R1 | Нет ретраев на 5xx / network errors | `_send_json`, `_get_json` | Средняя |
| R2 | Нет per-request timeout — полагаемся на session-level | весь модуль | Низкая |
| R3 | CSRF regex `"secretkey":"([^"]+)"` — единственная точка отказа | `DIALOGS_CSRF_REGEX` | Высокая |
| R4 | Нет rate-limit handling (только логируем `X-RateLimit-*`) | `_send_json` | Средняя |
| R5 | Нет проверки фактического завершения деплоя (fire-and-forget) | `request_deploy` | Средняя |
| R6 | `update_draft` принимает `Mapping[str, Any]` — нет типобезопасности payload'а | `update_draft` | Низкая |
| R7 | Нет частичных обновлений черновика — нужно всегда формировать полный payload | `build_*_draft_payload` | Средняя |
| R8 | Нет способа прочитать текущий черновик перед обновлением | — | Высокая |
| R9 | `_looks_like_duplicate` — эвристика по строкам в body | `_looks_like_duplicate` | Низкая |
| R10 | Хардкод `voice = "shitova.us"` для smart_home — если голос задепрекают, библиотека сломается | `build_smart_home_draft_payload` | Низкая |
| R11 | Нет машинно-читаемого `error_code` enum — только string | `DialogsApiError.yandex_error` | Низкая |
| R12 | `progress_cb` принимает только `SkillCreationArtifacts` — нет тонкой гранулярности (например, % прогресса загрузки логотипа) | `auto_create_skill` | Низкая |

---

## 2. Публичная документация Яндекс.Диалогов

### 2.1. Что официально задокументировано (по состоянию на 2026-05)

| URL | Что покрывает | Auth | Связано с нашим pipeline? |
|-----|---------------|------|---------------------------|
| `yandex.ru/dev/dialogs/` | Лендинг — три ветки: Alice / Smart Home / TV | — | Нет |
| `/dev/dialogs/alice/doc/ru/protocol` | **Runtime** webhook protocol (запрос/ответ от Алисы) | webhook | Нет — другая ветка API |
| `/dev/dialogs/alice/doc/ru/resource-upload` | Public REST: `dialogs.yandex.net/api/v1/skills/{id}/{images,sounds}` — загрузка assets | OAuth-token | Частично — мы грузим logo через cookie/CSRF на другой хост |
| `/dev/dialogs/alice/doc/ru/appmetrica` | Интеграция с AppMetrica через поле скилла | — | Нет |
| `/dev/dialogs/alice/doc/ru/publication` | Manual UI workflow Draft→Moderate→Publish | — | Нет (наш `request_deploy` через UI-API) |
| `/dev/dialogs/smart-home/.../callback/state\|discovery` | Notification service для smart-home провайдера | OAuth-token | Нет (это runtime для провайдера) |
| `/dev/dialogs/smart-home/.../reference/resources` | Smart-home cloud-to-cloud protocol | OAuth-token | Нет |

### 2.2. Что НЕ задокументировано

`dialogs.yandex.ru/developer-api/v2/*` — **не упоминается нигде** в публичной документации Яндекса. Нет OpenAPI/Swagger, нет changelog, нет news-feed. Анонсы платформы только в Telegram-канале и YouTube. Все наши endpoint'ы (POST `/api/apps`, PATCH `/api/apps/{id}/draft`, POST `/api/apps/{id}/release`, POST `/api/oauth/apps`, POST `/api/apps/{id}/oauth/{oauth_id}`) — полностью реверс-инженерены из HAR.

Не задокументированы также: статус модерации, on-air флаг, статистика (showings, ratings).

### 2.3. Главный вывод по докам

Наш клиент покрывает **полностью приватный, недокументированный** срез API. Единственные документированные REST-эндпоинты на смежных хостах:
- `dialogs.yandex.net/api/v1/skills/{id}/images|sounds` — assets (OAuth-token)
- `dialogs.yandex.net/api/v1/skills/{id}/callback/state|discovery` — smart-home notifications (OAuth-token)

> **Кандидат на рефакторинг:** мигрировать `upload_logo` с `dialogs.yandex.ru/.../draft/upload-logo` (cookie/CSRF) на документированный `dialogs.yandex.net/api/v1/skills/{id}/images` (OAuth) — стабильнее.

---

## 3. OSS-проекты: конкуренты, смежники, паттерны

### 3.1. Прямые конкуренты (developer-api/v2 / provisioning)

**Никого нет.** GitHub-поиск по `"developer-api/v2"`, `"secretkey"`, `"/api/apps"`, `"aliceSkill smartHome"` не даёт ни одного хита. `ya-dialogs-api` — пионер в нише.

- **Risk:** community-знаний о стабильности API нет — мы первые столкнёмся с любыми breaking changes.
- **Reward:** уникальное позиционирование. Стоит явно отметить в README как USP с честным дисклеймером.

### 3.2. Смежники (документированные assets API)

| Repo | Язык | ★ | Покрытие | Дельта vs нас |
|------|------|---|----------|---------------|
| `vitalets/alice-asset-manager` | Node.js | 3 | `dialogs.yandex.net/api/v1/skills/{id}/{images,sounds}` через OAuth — quota, upload, list, delete, getUrl, getTts | Покрывает official assets API, который мы НЕ покрываем. НЕ покрывает create/draft/publish |
| `fletcherist/yandex-dialogs-sdk` | TS | 124 | Runtime + та же v1 assets API | Стагнирует с 2018 |

### 3.3. Runtime-библиотеки (для контекста и паттернов)

| Repo | Язык | ★ | Активность | Заметки |
|------|------|---|-----------|---------|
| `K1rL3s/aliceio` | Python | 63 | jan 2025 | aiogram-v3-вдохновлён, dispatcher+middleware+FSM, mypy-friendly — **главный референс** |
| `mahenzon/aioalice` | Python | 92 | apr 2022 | aiohttp-based, dataclass payloads |
| `avidale/dialogic` | Python | 29 | редко | Multi-platform (Alice/Salute/TG/VK/FB) |
| `azzzak/alice` | Go | 40 | живой | "упор на скорость" |
| `alicedotnet/yandex.alice.sdk` | C# | 30+ | живой | ASP.NET Core, Smart Home |
| `vitalets/alice-types` | TS | низко | — | TypeScript-typings для protocol |

### 3.4. Smart-home consumers (контекст)

`glebsterx/yandex_smart_home_plus`, `AlexxIT/YandexDialogs` (★185), `dext0r/yandex_smart_home` — все используют callback/state и callback/discovery.

### 3.5. Паттерны для заимствования

Из анализа `aliceio` (самый свежий и качественный):

| Паттерн | У нас? | Стоит ли заимствовать |
|---------|--------|------------------------|
| `mypy --strict` + ruff full select | ✅ есть | — |
| FSM/state-machine для multi-step | ✅ есть (`SkillCreationArtifacts`) | — |
| Failures as values | ✅ есть | — |
| dataclass/typed payloads | 🟡 частично | **Hybrid**: typed wrappers вокруг известных полей + `extra: dict[str, Any]` |
| Middleware для api-calls | ❌ нет | **Да** — добавить `request_middleware: Callable | None` для логирования/ретраев |
| Per-request `request_timeout` | ❌ нет | **Да** — graceful-degradation в `last_error` |
| Retry с exponential backoff на 5xx/network | ❌ нет | **Да** — лёгкий retry-wrapper, max 3, jitter |
| pydantic schema-validation | ❌ нет | **Нет** — оверкилл для меняющегося приватного API |
| Декораторы регистрации хендлеров | n/a | Не релевантно — у нас pipeline, не event loop |

---

## 3.6. Выводы публичной разведки → точки фокуса для Phase 3

Дополнительные приоритеты, выявленные в Phase 1 (войдут в backlog):

1. **Phase 3 обязательно записать в HAR:**
   - GET `/api/apps/{id}` — статус публикации (`state.publishingState`: DRAFT / IN_REVIEW / PUBLISHED / REJECTED)
   - GET `/api/apps` — список скиллов (для CI: найти по имени)
   - DELETE `/api/apps/{id}` — для очистки в integration-тестах
   - GET `/api/apps/{id}/draft` — прочитать черновик перед PATCH (избежать частичной перезаписи)
   - GET статистики, snapshots/versions, test-runner endpoint
   - Refresh CSRF: один секрет на сессию или короткий TTL?
   - Smart-home vs aliceSkill — отличия в shape draft

2. **Hybrid auth path** (HIGH):
   - Текущий cookie/CSRF — для приватного dev-API
   - Параллельно — OAuth-token для документированного `dialogs.yandex.net/api/v1/...` (assets)
   - Пользователь выбирает по сценарию

3. **README USP**: явно указать "единственный публичный provisioning-клиент Yandex Dialogs" + честный дисклеймер про "private undocumented API, may break without notice".

---

## 4. Phase 2 — План Playwright-сценария

### 4.1. Технические правила сессии

- **Бровзер с UI** через Playwright MCP. Окно видно пользователю — он логинится сам в Yandex Passport, я пальцами в окне НЕ ввожу пароль/2FA.
- **Захват сетевого трафика**: после каждого значимого действия запрашиваем
  `browser_network_requests(filter="/developer/app-store-api/")`,
  записываем в `research/har/{phase}-{step}.json`.
- **Снэпшоты UI**: используем `browser_snapshot` (accessibility tree) вместо
  скриншотов — он машинно-читаемый, дёшев в контексте, и не утекает PII.
- **Никаких логов с куками/токенами в RESEARCH.md.** UUID создаваемых
  ресурсов (skill_id, oauth_id) можно — они уже не секрет после удаления.
- **Очистка**: создаваемые в ходе сессии навыки и OAuth-приложения удаляем
  по итогам Phase 3 (если в API нашёлся метод удаления — через него; иначе
  оставляем себе TODO на ручную чистку через UI и фиксируем в backlog как
  «нужен endpoint удаления»).
- **Lockdown**: при первой подозрительной странице (CAPTCHA, странный
  редирект, доп.подтверждение) останавливаемся и спрашиваем пользователя.

### 4.2. Чек-лист сценариев

Каждый пункт — отдельный snapshot + сетевой захват.

#### A. Преамбула
- A1. Открыть `https://dialogs.yandex.ru/developer` без авторизации — увидеть
  редирект на Passport, зафиксировать форму CSRF в HTML (validate regex).
- A2. После логина — `browser_snapshot` главной страницы, `browser_network_requests`
  для всего, что подгрузилось (выявить `/snapshot`, возможно `/me`, `/categories`,
  `/voices` — то, что мы сейчас не вызываем).
- A3. Прочитать `<head>` страницы целиком: какие meta, какие JSON-блобы embedded,
  есть ли API-версия, фичефлаги, переменные окружения.

#### B. Smart Home: создание навыка с нуля
- B1. Нажать «Создать навык» → выбрать «Умный дом». Захватить network: какой
  POST, какой payload, какой ответ. Сравнить с нашим `create_app`.
- B2. Перейти в созданный черновик. **Получить GET /apps/{id}/draft** —
  зафиксировать shape (этого метода у нас нет).
- B3. Заполнить все поля формы (название, бренд, ссылки, OAuth, регионы):
  по одному, отправляя save после каждого. Захватить, какие PATCH'и
  идут — частичные или полные. Сверить с `build_smart_home_draft_payload`.
- B4. Загрузить логотип через UI (drag-n-drop) — сравнить multipart с нашим.
- B5. Открыть «Тестирование» — захватить endpoint test-call, payload,
  ответ. **Кандидат на новый метод**.
- B6. «На модерацию» → захватить request-deploy. Открыть страницу со статусом —
  захватить GET статуса (наверняка polling). **Кандидат: `get_release_status`**.
- B7. Снять с публикации (если будет такой UI) → захватить.
- B8. Удалить навык целиком → захватить DELETE-эндпоинт.

#### C. Dialog (aliceSkill): создание навыка с нуля
- C1. «Создать навык» → «Навык для Алисы» (custom dialog). Захватить
  `create_app` с `channel=aliceSkill`.
- C2. GET черновика — какие поля отличаются от smart_home shape?
- C3. Активационные фразы — отдельная страница в UI. Они отдельный endpoint
  или часть `update_draft`? Захватить.
- C4. Структурированные примеры — как UI их редактирует? Полный список
  per-PATCH или есть `POST /examples`?
- C5. Выбор поверхностей (`requiredInterfaces`, `surfaceWhitelist/Blacklist`):
  есть ли отдельные endpoint'ы со списком доступных значений?
- C6. Голос: открыть селект — какой список приходит? Захватить (**кандидат:
  `list_voices`**).
- C7. Категория: захватить список категорий (**кандидат: `list_categories`**).
- C8. AppMetrica integration — захватить, как привязывается ключ.
- C9. State storage (cloud-state) — какой endpoint включает.
- C10. Surfaces test runner — есть ли тестовый чат прямо в консоли?
  Захватить request shape.

#### D. OAuth Apps (account-linking)
- D1. Перейти в раздел OAuth-приложений. GET-список. Захватить shape
  (**кандидат: `list_oauth_apps`**).
- D2. Создать OAuth-приложение через UI — сверить payload с
  `build_oauth_app_payload` (особенно `scope`, `yandexClientId`).
- D3. Редактировать существующее — какой PATCH/PUT? (**кандидат: `update_oauth_app`**)
- D4. Удалить — какой DELETE? (**кандидат: `delete_oauth_app`**)
- D5. Привязка/отвязка OAuth от навыка — оба ли направления есть, какой
  endpoint для отвязки?

#### E. Эксплуатационные сценарии
- E1. Истекшая CSRF: подождать или принудительно обнулить через DevTools,
  попробовать действие. Какой код возврата? Как Yandex её регенерирует?
- E2. Rate limit: повторить идемпотентный GET 50 раз быстро — зафиксировать
  заголовки `X-RateLimit-*`, поведение при превышении.
- E3. Конкурентные запросы: открыть две вкладки, в обеих сохранить разные
  правки черновика. Кто выигрывает? Есть ли версионирование `If-Match`?
- E4. Большой логотип: загрузить PNG > 1 MB — какой ответ?
- E5. Невалидный payload: отправить мусор через DevTools — посмотреть формат
  ошибки (нужно для классификации в `_extract_error_code`).

#### F. Платформенные API (за кулисами)
- F1. Любой endpoint, который консоль вызывает на старте, но мы не используем:
  `/me`, `/profile`, `/notifications`, `/tasks`, `/quota`, `/regions`,
  `/categories`, `/voices`, `/surfaces`, `/interfaces`. Зафиксировать все.
- F2. WebSocket / SSE: есть ли live-обновления статуса публикации? Захватить
  upgrade-запрос, тип сообщений.
- F3. Версионирование API: смотреть на header `X-API-Version` или путь
  `/v2` vs `/v3` — есть ли намёки на эволюцию.

### 4.3. Артефакты, которые соберём

```
research/
├── network/
│   ├── A-preamble.json
│   ├── B-smarthome-create.json
│   ├── B-smarthome-draft-update.json
│   ├── B-smarthome-deploy.json
│   ├── C-dialog-create.json
│   ├── C-dialog-draft-update.json
│   ├── C-dialog-activation.json
│   ├── D-oauth-list.json
│   ├── E-edge-cases.json
│   └── F-bg-traffic.json
├── snapshots/
│   ├── home-page.md
│   ├── smarthome-form.md
│   ├── dialog-form.md
│   └── oauth-form.md
└── findings.md       (промежуточные заметки, мерджатся в RESEARCH.md секцию 5)
```

### 4.4. Точки остановки (gates)

После каждой группы (A, B, C, D, E, F) — короткое резюме в чате,
ожидание подтверждения от пользователя на продолжение. Если попадаем в
«серую зону» (странные ошибки, незнакомый UI), стопаемся и обсуждаем.

---

## 5. Phase 3 — Результаты Playwright-сессии

Дата: 2026-05-06.
Браузер: Playwright MCP (Chromium).

### 5.1. Преамбула — авторизация

**A1. Анонимный заход на `https://dialogs.yandex.ru/developer`**

Поведение: моментальный 30x-редирект на
`https://passport.yandex.ru/pwl-yandex/auth/add?retpath=https%3A%2F%2Fdialogs.yandex.ru%2Fdeveloper&cause=auth&process_uuid=…`

Наблюдения:
- На приватном API-эндпоинте (через нашу либу) при отсутствии cookies возвращается `HTTP 401`.
  В UI же — цепочка редиректов до Passport. Это значит, что наша либа НЕ должна следовать
  редиректам автоматически — иначе вместо `401` мы упрёмся в HTML-страницу логина и regex
  на `secretkey` не сработает.
- Это уже косвенно подтверждает: текущая `fetch_csrf` правильно обрабатывает 401 как
  «нужна авторизация», без попытки парсить HTML Passport'а.

### 5.2. Преамбула — авторизованный дашборд (A2)

**Endpoint, который вызывает консоль на старте:**
- `GET /developer/app-store-api/snapshot` — единственный API-вызов на дашборде. Возвращает `200`.

**Подтверждение CSRF-механики:**
- Регекс `"secretkey":"([^"]+)"` находит токен в HTML. Длина токена — 33 символа (формат `u{hex32}`).
- НЕТ `<meta name="csrf-token">`, НЕТ `window.__INITIAL_STATE__`.
- Токен передаётся в заголовке `x-csrf-token` — наша реализация корректна.

**🔴 Главное открытие: `/snapshot` — super-endpoint.**

Один вызов возвращает payload, содержащий следующие поля верхнего уровня
(под `result.*`):

| Поле | Содержимое | Используем сейчас? | Потенциал |
|------|-----------|--------------------|-----------|
| `skills[]` | Полное состояние всех навыков пользователя (draft, publishingSettings, logo, oauthAppId, onAir, status) | 🟢 частично (`list_existing_skills`) | Расширить: вернуть typed модель с полным state |
| `categories[]` | Все 19 категорий каталога с `type`/`title`/`isDefault` | 🔴 нет | **Новый метод `list_categories()`** |
| `voices[]` | 9 TTS-голосов с публичными WAV-сэмплами | 🔴 нет | **Новый метод `list_voices()`** |
| `timezones[]` | ~85 таймзон с offset+title | 🔴 нет | Новый метод `list_timezones()` |
| `operations[]` | **AUDIT LOG**: deployRequested / deployCompleted / skillWithdrawn с timestamps, itemId, comment | 🔴 нет | **Новый метод `get_audit_log()` — критично для polling статуса публикации** |
| `maxSkillsPerUser` | Лимит = 100 | 🔴 нет | Новый метод `get_quota()` |
| `regionsWithSufficientLanguages[]` | Регионы и поддерживаемые языки | 🔴 нет | Новый метод `list_regions()` |

**Поля у каждого скилла, которые мы НЕ читаем сейчас:**

| Поле | Тип | Что значит |
|------|-----|------------|
| `id` (top-level) | UUID | Skill ID (мы знаем) |
| `draft.id` | UUID | **Отдельный ID черновика** — мы путали с skill_id |
| `draft.status` | `inDevelopment` / `onModeration` / `approved` / `rejected` | **Статус модерации** — критично для CI |
| `draft.isAllowedForDeploy` | bool | **Server-side флаг готовности к деплою** — должны проверять перед request_deploy |
| `onAir` | bool | Опубликован ли в каталоге |
| `firstPublishedAt` | datetime | Когда впервые опубликован |
| `slug` | string | URL-slug в каталоге (`29082999-muzykal-nyj-assistent`) |
| `botGuid` | UUID/null | ID бота (если есть) |
| `canSkipReview` | bool | **Может ли пользователь пропустить модерацию** |
| `safeForKids` | bool | Флаг безопасности для детей |
| `useZora` | bool | Внутренний флаг |
| `developerType` | `external` / `internal` | Тип разработчика |
| `isBanned` | bool | Забанен ли скилл |
| `surfaces[]` | `["mobile", "navigator", "station", "maps", "auto"]` | Поверхности, на которых работает |
| `allowedRegions[]` | `["RU", "KZ", "BY", "UZ", "AZ"]` | Разрешённые регионы (override `enableAllAvailableRegions`) |
| `notificationSettings` | object | Настройки email-уведомлений модерации |
| `donationSettings` | object/null | Настройки донатов |
| `images[]` | array | **Все картинки навыка**, не только logo (для visual UI Алисы) |
| `homepageBadgeTypes` | array/null | Бэйджи на главной |
| `editorName` | string/null | Имя редактора (для коллаб) |

**Поля юзера в `result.user`:**
- `id`, `name`, `isAdmin`, `isGlobalViewer`, `isBanned`, `hasSubscription`, `hasNewsSubscription`
- `featureFlags`: `allowSetAccountLinkingForAliceSkills`, `allowUploadSounds`, `allowUseAliceApps`, `allowAliceInternalAgentApp`

**Полный список категорий (исправление нашего хардкода `"music_audio"` дефолтом):**
```
smart_home, games_trivia_accessories, kids, shopping, food_drink,
education_reference, news, communication, health_fitness, travel_transportation,
business_finance, productivity, utilities (default), local, weather,
connected_car, movies_tv, music_audio, lifestyle
```

**Полный список TTS-голосов (наш дефолт `good_oksana` валиден; smart-home хардкод `shitova.us` тоже валиден):**
```
good_oksana (Оксана), jane (Джейн), zahar (Захар), ermil (Эрмил),
erkanyavas (Эркан Явас), shitova.us (Алиса), kostya.gpu (Костя),
valtz.gpu (Филипп), tatyana_abramova.gpu (Аня)
```

**🟡 Замечание по типизации `voice`:** наш Literal'ы можно сгенерировать из этого списка.

**🟡 Замечание по UI-вкладкам**: в навигации видны 4 категории — `tv-apps`, `smart-screen` (Дуо Макс), `skills` (Алиса/aliceSkill), `smart-home`. То есть **есть как минимум 4 channel-значения**, не 2. Значения для TV/Duo Max нужно реверсить в группе F.

### 5.3. Аудит существующих скиллов пользователя (3 шт.)

В `/snapshot` уже подтянулась полная инфа по всем нашим скиллам:

| Skill ID | Channel | Name | Status | Создан |
|----------|---------|------|--------|--------|
| `2c22b323-…` | aliceSkill | Музыкальный ассистент | onAir, publishingSettings.category=`music_audio` | 2026-05-05 |
| `2c6383ba-…` | smartHome | Music Assistant Direct | onAir, publishingSettings.category=`smart_home` | 2026-05-05 |
| `4a8d69a8-…` | smartHome | Home Assistant | onAir, publishingSettings.category=`smart_home` | 2021-10-23 |

Хорошие референсы для сравнения с нашими payload-builders. В частности у smart_home `Music Assistant Direct` уже сейчас есть `multilingualSettings`, `smartHome.deepLinks` — эти поля совпадают с нашим builder'ом.

### 5.4. Существующие подразделы навыка (sidebar в dev-консоли)

Каждый навык имеет 12 подразделов. Я собрал network на каждом и проверил
гипотезы прямыми GET-запросами.

| URL подраздела | API endpoint? | Статус |
|----------------|----------------|--------|
| `/settings/main` | `GET /apps/{id}` + `GET /apps/{id}/operations` | 🟢 покрыто частично |
| `/settings/intents` | `GET /apps/{id}/intents?channel=aliceSkill` | 🆕 NEW endpoint, требует channel |
| `/authorization` | `GET /oauth/apps/{oauth_id}` | 🆕 NEW endpoint |
| `/resources/` | `GET /api/public/v1/skills/{id}/images?page=1` (CSRF) | 🆕 NEW separate API path |
| `/test` | UI-only, реальный test-call идёт на webhook | 🚫 не пробовал (риск дёрнуть prod) |
| `/rating` | endpoint **отсутствует** (UI рендерит из snapshot) | ☐ — |
| `/promotion` | endpoint **отсутствует** | ☐ — |
| `/monitoring` | данные не подгружаются на load (вероятно on-demand или WebSocket) | ☐ — |
| `/donation` | endpoint **отсутствует** | ☐ — |
| `/sharing` | endpoint **отсутствует** | ☐ — |
| `/operations` | `GET /apps/{id}/operations` | 🆕 NEW endpoint |

Дополнительно для smart_home:
- `GET /apps/{id}/testing-date/status?channel=smartHome|aliceSkill` — **🆕 NEW**, поддерживает оба channel'а, возвращает `{status: "NOT_SPECIFIED", record: null}`. Используется для запланированных дат тестирования модерации.

### 5.5. Создание навыка (E2E pipeline)

Захвачены полные request/response для обоих типов скиллов:

**aliceSkill** — `POST /apps`
```json
// Request:
{"channel":"aliceSkill","language":"ru","isYangoConsole":false,"appName":"Новый навык"}
// Response (200): возвращает полный объект скилла с:
//   - result.id (skill_id, UUID)
//   - result.draft.id (draft_id, отдельный UUID — мы это упускаем!)
//   - result.draft.status="inDevelopment"
//   - result.draft.isAllowedForDeploy=null (только true/false после заполнения)
//   - result.skillAccess="private" (default top-level)
//   - result.draft.skillAccess="public" ⚠️ inconsistency (UI показывает «публичный»)
//   - result.draft.oauthAppId="null" ⚠️ строка "null", не null!
```

**smartHome** — `POST /apps`
```json
// Request: identical shape, channel="smartHome"
{"channel":"smartHome","language":"ru","isYangoConsole":false,"appName":"Новый умный дом"}
```

✅ **Наш `create_app` payload корректен 1:1.** Не обнаружено отклонений от того,
что отправляет UI.

⚠️ Quirks для будущей доработки типизированных моделей:
- `draft.oauthAppId` сразу после создания — литерал `"null"` (строка), не `null`.
  Если будем парсить — нужен handler этой странности.
- `draft.skillAccess` и top-level `skillAccess` могут расходиться сразу после
  создания. UI отображает draft-значение.
- `isAllowedForDeploy` — `null` сначала, `true` после успешного PATCH с минимально
  валидным черновиком. Server-side флаг готовности к деплою.

### 5.6. DELETE endpoint (G3) — найден

```
DELETE /developer/app-store-api/apps/{skill_id}?channel={channel}
→ HTTP 200, body: "{}"
```

- Channel **обязателен** в query string (smartHome / aliceSkill).
- Body запроса — пустой.
- Ответ — пустой объект.
- Удаляет навык вместе со всеми связями (черновик, операции, OAuth-binding —
  но **не** удаляет связанный OAuth-app: он остаётся в `/oauth/apps`).

⚠️ Из этого следует: при удалении навыка через нашу либу нужно отдельно
**опционально** чистить OAuth-приложение (если оно было создано вместе с навыком).

### 5.7. OAuth apps API (новые endpoints)

| Метод | Путь | Назначение | Статус |
|-------|------|-----------|--------|
| GET   | `/oauth/apps` | Список всех OAuth-приложений пользователя | 🆕 200 |
| GET   | `/oauth/apps/{oauth_id}` | Детали одного OAuth-приложения | 🆕 200 |
| POST  | `/oauth/apps` | Создание (уже у нас) | 🟢 |

**NOT FOUND (404 при пробинге):**
- `PATCH /oauth/apps/{id}` — обновление (вероятно через UI = удалить + создать)
- `DELETE /oauth/apps/{id}` — пробить ещё надо (вынес в TODO)

### 5.8. Public API path (`/developer/api/public/v1/`)

Отдельный set эндпоинтов — на том же домене `dialogs.yandex.ru`, но **другой
префикс**, **тот же** CSRF-auth (не OAuth!). Используется dev-консолью для
ресурсов навыка:

| Endpoint | Назначение | Подтверждение |
|----------|-----------|---------------|
| `GET /developer/api/public/v1/skills/{id}/images?page=1` | Список картинок (paginated) | 🟢 200 `{images:[], total:0, limit:32}` |
| `GET /developer/api/public/v1/skills/{id}/sounds?page=1` | Список звуков (paginated) | 🟢 200 `{sounds:[], total:0, limit:32}` |
| `GET /developer/api/public/v1/status` | Квоты: 100MB images, 1GB sounds | 🟢 200 |
| `GET /developer/api/public/v1/skills/{id}` | Детали скилла на public path | ❌ 404 (не используется) |

⚠️ Это **не то же самое**, что документированный `dialogs.yandex.net/api/v1/skills/{id}/{images,sounds}` (OAuth-token). Тут CSRF/cookie. Public-path является **прокси** для тех же ресурсов изнутри dev-консоли.

### 5.9. Endpoint probing — что НЕ существует

Подтверждено `404` для следующих гипотез:
- `GET /apps` (есть только POST → 405 Method Not Allowed)
- `GET /apps/{id}/draft` (черновик читается через `/apps/{id}`, не отдельным endpoint)
- `GET /apps/{id}/release/status`, `/draft/status`, `/snapshots`, `/versions`
- `GET /apps/{id}/sharing`, `/donation`, `/promotion`, `/rating`, `/stats`, `/monitoring`
- `GET /apps/{id}/custom-entities`, `/grammars` (это поля внутри draft)
- Standalone resources: `/categories`, `/voices`, `/regions`, `/me`, `/profile`, `/quota`, `/notification-settings`

**Вывод:** **`/snapshot` — единственный source of truth** для всех справочников.
Никаких микро-эндпоинтов под `voices`/`categories` нет.

### 5.10. Edge cases (CSRF / методы)

🔴 **CSRF поведение неконсистентно по методам:**

| Сценарий | Поведение |
|----------|-----------|
| POST/PATCH/DELETE с правильным CSRF | 200 + JSON |
| POST/PATCH/DELETE с **невалидным** CSRF | **403 + HTML body** "`<!DOCTYPE html>...<pre>Forbidden</pre>`" |
| POST/PATCH/DELETE без CSRF | то же что и невалидный → 403 + HTML |
| GET с любым (или без) CSRF | 200 OK |

🟥 **Это критический баг в нашей либе:** `_send_json` падает на `_try_json(body)`
для HTML-ответа 403, потому что выкинет `DialogsApiError` с урезанным сообщением,
не подсказывающим истинную причину (CSRF expired / not sent). Нужен типизированный
`DialogsAuthError` для 403 с HTML body.

### 5.11. Внутренняя архитектура (утечка через 404)

При обращении к несуществующему endpoint Yandex возвращает **Spring
dispatcherServlet** error JSON:

```json
{
  "servlet": "dispatcherServlet",
  "message": "Skill not found with id: 00000000-0000-0000-0000-000000000000",
  "url": "/api/dev-console/v1/apps/00000000-0000-0000-0000-000000000000",
  "status": "404"
}
```

Что это даёт нам:
- 🔴 Реальный внутренний путь — `/api/dev-console/v1/...`. Текущий публичный
  префикс `/developer/app-store-api/` — **прокси-маппинг**, который теоретически
  может смениться. Внутренний путь стабильнее.
- 🟢 **Структурированный формат ошибок!** Можно писать типизированный парсер:
  - `"Skill not found with id: <uuid>"` → `DialogsSkillNotFoundError(skill_id=...)`
  - `"Required parameter '<name>' is not present."` → `DialogsValidationError(parameter=...)`
  - `"Method '<X>' is not supported."` → 405 (внутренняя регрессия)
  - `"No static resource <path>."` → endpoint удалён/переименован

Это **гораздо** лучше нашей текущей эвристики `_looks_like_duplicate` +
`_extract_error_code` (которая пытается достать `error/errorCode/message/code`).
Можно добавить отдельный парсер для Spring-формата.

### 5.12. Полная карта обнаруженных endpoints

| # | Метод | Путь | Channel? | Auth | Покрыто? | Назначение |
|---|-------|------|----------|------|----------|------------|
| 1 | GET | `/developer` (HTML) | — | cookie | 🟢 | Извлечение CSRF |
| 2 | GET | `/developer/app-store-api/snapshot` | — | CSRF | 🟢 (частично) | Super-endpoint: skills, categories, voices, timezones, operations, quota, regions |
| 3 | POST | `/developer/app-store-api/apps` | в payload | CSRF | 🟢 | Создание навыка |
| 4 | DELETE | `/developer/app-store-api/apps/{id}?channel=...` | query | CSRF | 🔴 | **NEW** — удаление |
| 5 | GET | `/developer/app-store-api/apps/{id}` | — | CSRF | 🔴 | **NEW** — детали навыка (включая draft) |
| 6 | POST | `/developer/app-store-api/apps/{id}/draft/upload-logo?channel=...` | query | CSRF | 🟢 | Загрузка логотипа |
| 7 | PATCH | `/developer/app-store-api/apps/{id}/draft/update` | в payload | CSRF | 🟢 | Обновление черновика |
| 8 | POST | `/developer/app-store-api/apps/{id}/draft/request-deploy?channel=...` | query | CSRF | 🟢 | Запрос модерации/деплоя |
| 9 | GET | `/developer/app-store-api/apps/{id}/operations` | optional | CSRF | 🔴 | **NEW** — audit log по навыку |
| 10 | GET | `/developer/app-store-api/apps/{id}/intents?channel=aliceSkill` | required | CSRF | 🔴 | **NEW** — список NLU-интентов dialog скилла |
| 11 | GET | `/developer/app-store-api/apps/{id}/testing-date/status?channel=...` | required | CSRF | 🔴 | **NEW** — даты тестирования модерации |
| 12 | POST | `/developer/app-store-api/oauth/apps` | — | CSRF | 🟢 | Создание OAuth-приложения |
| 13 | GET | `/developer/app-store-api/oauth/apps` | — | CSRF | 🔴 | **NEW** — список OAuth-приложений |
| 14 | GET | `/developer/app-store-api/oauth/apps/{oauth_id}` | — | CSRF | 🔴 | **NEW** — детали OAuth-приложения |
| 15 | POST | `/developer/app-store-api/apps/{id}/oauthApp?channel=...` | query | CSRF | 🟢 | Привязка OAuth |
| 16 | GET | `/developer/api/public/v1/skills/{id}/images?page=N` | — | CSRF | 🔴 | **NEW** — список картинок (paginated) |
| 17 | GET | `/developer/api/public/v1/skills/{id}/sounds?page=N` | — | CSRF | 🔴 | **NEW** — список звуков (paginated) |
| 18 | GET | `/developer/api/public/v1/status` | — | CSRF | 🔴 | **NEW** — квоты (100MB images, 1GB sounds) |

**Итог карты:** мы покрываем 7 из 18 идентифицированных endpoints (≈ 39 %).
11 новых — кандидаты на расширение.

---

---

## 6. Phase 4 — Приоритизированный backlog

Принципы:
- **Effort:** S (≤ 1 день), M (2–3 дня), L (неделя+).
- **Impact:** обоснован пользовательскими сценариями, найденными ошибками или
  «slow death» рисками (например, ломкий regex).
- **Семейство:** R (Robustness), C (Coverage), DX (Developer Experience),
  D (Documentation/Marketing), T (Testing).

Уровни приоритета: **P0** (релиз-блокер для 1.1.x), **P1** (плановый next minor),
**P2** (когда будет время), **P3** (nice-to-have / зависит от внешних факторов).

### 6.1. P0 — критические доработки (релиз-блокеры)

| # | Тема | Семейство | Effort | Обоснование |
|---|------|-----------|--------|-------------|
| **P0-1** | Корректная обработка 403 + HTML-body для невалидной/просроченной CSRF | R | S | Сейчас `_send_json` парсит HTML как JSON и теряет настоящую причину. Создать `DialogsAuthError` (subclass `DialogsApiError`) для статусов 401/403 с HTML body. Подсказывать пользователю «CSRF вышла, перелогиньтесь / обновите cookies». |
| **P0-2** | Парсинг Spring dispatcherServlet error JSON | R | S | 5.11 — у Yandex стабильный формат `{servlet, message, url, status}`. Извлечь в helper `_extract_spring_error()` и использовать в `_extract_error_code`. Map: `"Skill not found"` → `DialogsSkillNotFoundError`, `"Required parameter '...' is not present"` → `DialogsValidationError(parameter=...)`. |
| **P0-3** | Не следовать редиректам в `fetch_csrf` | R | S | Подтверждено: анонимный заход → 30x на Passport. Если aiohttp следует редиректу, regex отпарсит мусор. Явно ставить `allow_redirects=False`, и при 30x raise `DialogsApiError("not authenticated", http_status=302)`. Уже работает для 401, но 30x не покрыт. |
| **P0-4** | DELETE endpoint — `delete_skill(skill_id, channel)` | C | S | Найден в 5.6. Нужен для CI/integration-tests. Тривиальная реализация: `DELETE /apps/{id}?channel=...`. **Без него** наши пайплайны производят мусор в аккаунте Yandex и пользователи вынуждены чистить вручную. |

### 6.2. P1 — плановый next minor (1.1.0)

| # | Тема | Семейство | Effort | Обоснование |
|---|------|-----------|--------|-------------|
| **P1-1** | `get_skill(skill_id)` → typed `SkillState` | C | M | GET `/apps/{id}` (5.5). Вернуть TypedDict / dataclass с `state`, `draft`, `onAir`, `firstPublishedAt`, `slug`, `surfaces`, `images`. **Главный кандидат** на typed wrapper — это «снимок» состояния скилла. |
| **P1-2** | `get_operations(skill_id, channel=None)` + audit-log polling | C | M | GET `/apps/{id}/operations`. Типы операций: `deployRequested`, `deployCompleted`, `skillWithdrawn` (вероятно есть `moderationApproved`, `moderationRejected` — захватим в следующей сессии). Используется для polling после `request_deploy`. |
| **P1-3** | `wait_for_deploy_completion(skill_id, *, timeout=900, poll_interval=10)` | C | M | Высокоуровневый orchestrator: после `request_deploy` поллит `get_operations`, ждёт `deployCompleted` или `moderationRejected`. Возвращает финальный статус. Снимет огромную боль с CI: сейчас deploy fire-and-forget, пользователь не знает, прошло или нет. |
| **P1-4** | Retry middleware с exponential backoff | R | M | Текущая реализация падает на любой 5xx и сетевой ошибке. Добавить лёгкий wrapper в `_send_json` / `_get_json`: max 3 попытки, jitter, retry только на (5xx, ConnectionError, TimeoutError). Опционально — `aiohttp_retry` или ручная имплементация. |
| **P1-5** | Per-request timeout | R | S | Сейчас полагаемся на session-level. Добавить `request_timeout: float = 30.0` параметр в `DialogsSkillCreator.__init__`, прокидывать в каждый запрос как `aiohttp.ClientTimeout(total=...)`. |
| **P1-6** | `list_oauth_apps()` + `get_oauth_app(oauth_id)` | C | S | 5.7. Тривиальная пара GET'ов. Нужно для re-use OAuth-приложений между навыками (текущий `auto_create_skill` всегда создаёт новый — лишний ресурс). |
| **P1-7** | Channel как Literal/Enum + validation | DX | S | Есть только две валидные строки (`smartHome`, `aliceSkill`). Сейчас `channel: str`. Сделать `Channel = Literal["smartHome", "aliceSkill"]` и валидировать в `DialogsSkillCreator`. |
| **P1-8** | `Voice` + `Category` + `Region` Literal-типы | DX | S | Сгенерировать из `/snapshot` (см. 5.2) полные списки. Если поменяется — линтер подскажет. Для voice: 9 значений; для category: 19. |
| **P1-9** | Snapshot-tests на захваченных HAR | T | M | Сейчас тесты пишут payloads вручную. Добавить fixture `tests/har/*.json` с реальными ответами — гарантия, что мы парсим то, что реально отдаёт Yandex. **Прямо использовать наши `research/network/*.json` как seed.** |

### 6.3. P2 — когда будет время

| # | Тема | Семейство | Effort | Обоснование |
|---|------|-----------|--------|-------------|
| **P2-1** | `read_skill_draft(skill_id)` перед PATCH (merge-update) | C | M | Сейчас `update_draft` принимает целый payload и любое отсутствующее поле трёт значение в Yandex. После добавления `get_skill` (P1-1) можно реализовать `update_draft_partial(skill_id, **fields)` — сначала читаем, потом мержим, потом PATCH. |
| **P2-2** | Image/sound API — `upload_image`, `list_images`, `delete_image`, `get_quota` | C | M | 5.8. Endpoints `/api/public/v1/skills/{id}/{images,sounds}` уже на нашем хосте, та же CSRF-auth. Парность с `vitalets/alice-asset-manager` — ставит нас в один ряд по покрытию assets. |
| **P2-3** | `delete_oauth_app(oauth_id)` | C | S | Не подтвердили endpoint в Phase 3 (404 у нас не было — но и UI-кнопки не нашли). Доразведать в следующей сессии: похоже, OAuth-app удаляется автоматически при удалении последнего привязанного навыка. |
| **P2-4** | Парсинг Spring quirks payload'а | DX | S | `draft.oauthAppId == "null"` (строка) — нужен sanitizer. `draft.skillAccess` vs top-level `skillAccess` — какое читать. Документировать в docstring. |
| **P2-5** | Hybrid auth — OAuth-token путь для `dialogs.yandex.net/api/v1/...` | C | L | См. Phase 1 (assets API задокументирован, OAuth). Не критично, но даёт более стабильный путь для assets, чем CSRF/cookie. Может смешиваться с текущим путём через флаг. |
| **P2-6** | Проверка `draft.isAllowedForDeploy` перед `request_deploy` | R | S | Server-side флаг готовности (5.5). Если `false` — заранее raise `DialogsValidationError("draft not ready for deploy: <reason>")` без лишнего round-trip. |
| **P2-7** | Skip CSRF для GET-запросов | R | S | 5.10: GET-ам CSRF не нужен. Микро-оптимизация — экономит один запрос за `fetch_csrf` при чтении статуса. |
| **P2-8** | Middleware hook (`request_middleware: Callable | None`) | DX | M | По примеру aliceio. Позволит пользователям подключать логирование, ретраи, кастомные заголовки без форка либы. |
| **P2-9** | Dialog: NLU-интенты — `list_intents` / `update_intents` | C | M | 5.4: `GET /apps/{id}/intents?channel=aliceSkill`. Получили пустой массив. Создать тестовый intent в UI и реверсить shape — отдельная подзадача. |
| **P2-10** | Smart-home: `get_testing_date_status` / `set_testing_date` | C | S | `/testing-date/status` найден. Вероятно есть PUT для установки. Низкоприоритетно, узкий сценарий. |

### 6.4. P3 — nice-to-have

| # | Тема | Семейство | Effort | Обоснование |
|---|------|-----------|--------|-------------|
| **P3-1** | TV apps / Duo Max apps — реверс channel-значений | C | M | Phase 3 не покрыли (пользователь скипнул). Скорее всего channels: `tvApp`, `smartScreen` или похожие. Запасная фаза разведки. |
| **P3-2** | Test-runner endpoint | C | M | UI отправляет реальные сообщения через webhook — реверсить shape запроса на `/test`. Узкий сценарий (e2e-тесты дев-окружения). |
| **P3-3** | Pydantic-модели для draft payload | DX | L | Альтернатива гибридному подходу. Pro: строгая типизация. Contra: shape меняется молча, придётся часто обновлять. **Не рекомендуем** до 1.x. |
| **P3-4** | AppMetrica integration helpers | C | M | Отдельный layer для чтения метрик скилла. Вероятно лучше как отдельная под-либа `ya-dialogs-metrics`. |
| **P3-5** | Smart-home callback notifications (`POST /api/v1/skills/{id}/callback/state`) | C | M | Документированный OAuth API для smart-home провайдеров. Нишево (только smart-home cloud-to-cloud). |
| **P3-6** | Yandex.Cloud Function как backend | C | M | UI имеет radio "Webhook URL / Функция в Яндекс Облаке" (5.4). Сейчас `backendType` хардкод `webhook`. Расширить с `cloudFunction`, `functionId` параметром. |
| **P3-7** | README USP-redo + Honest-disclaimer | D | S | Из Phase 1: «единственный публичный provisioning-клиент». Подчеркнуть в hero-секции, добавить explicit warning про private API. |

### 6.5. Концептуальная карта будущего модуля

После реализации P0+P1 структура `src/ya_dialogs_api/` логично разрастается:

```
src/ya_dialogs_api/
├── __init__.py            — re-exports (~40 символов)
├── api_client.py          — DialogsSkillCreator + auto_create_skill (как сейчас)
├── client_meta.py         🆕 — list_voices, list_categories, list_regions, get_quota
│                              (всё из /snapshot — кэшируется per-instance)
├── client_skills.py       🆕 — get_skill, delete_skill, list_skills (через /snapshot)
├── client_oauth.py        🆕 — list_oauth_apps, get_oauth_app
├── client_assets.py       🆕 — upload_image, list_images, get_image_quota (P2)
├── client_operations.py   🆕 — get_operations, wait_for_deploy_completion
├── errors.py              🆕 — типизированные ошибки (P0-2): DialogsAuthError,
│                              DialogsSkillNotFoundError, DialogsValidationError,
│                              DialogsRateLimitError, DialogsServerError
├── retry.py               🆕 — exponential backoff middleware (P1-4)
├── state.py               — без изменений (state machine осталась актуальной)
├── types.py               🆕 — Channel, Voice, Category, Region literals (P1-7/8)
└── assets/default_logo.png
```

Тесты соответственно разбиваются на `test_client_skills.py`, `test_client_oauth.py`, и т. д.

### 6.6. Quick wins для ближайшей PR (1.1.0-rc1)

Если отбирать **минимальный пакет, дающий максимум value за 1–2 дня**:

1. **P0-2** (Spring error parser) — 1–2 часа.
2. **P0-3** (no follow_redirects) — 30 минут.
3. **P0-4** (`delete_skill`) — 1 час.
4. **P1-6** (`list_oauth_apps`, `get_oauth_app`) — 1 час.
5. **P1-7** (Channel Literal) — 30 минут.

Итого ~5 часов, ~150 строк нового кода + ~100 строк тестов. Закрывает основные
risk-flags и снимает claim «pioneer» на чуть более твёрдый фундамент.

### 6.7. Найденные баги в текущей версии 1.0.0

1. **`_send_json` падает на 403 + HTML body** (5.10). `_try_json` вернёт `None`,
   но `_extract_error_code` тоже вернёт `None`, и пользователь получит сообщение
   «HTTP 403: <!DOCTYPE html>...» с обрезанным HTML вместо понятной CSRF-ошибки.
2. **CSRF regex `"secretkey":"([^"]+)"` остаётся хрупким** (5.2). Подтверждено:
   токен есть, длина 33 символа, формат `u{hex32}`. Регекс работает, но
   рекомендую усилить ассертом длины и формата (опционально `re.match(r"^u[0-9a-f]{32}$")`).
3. **`auto_rename_dialog_skill` не учитывает уже опубликованный slug**.
   Из `/snapshot.skills[].slug` видно, что Yandex генерирует SEO-slug при
   первой публикации (`29082999-muzykal-nyj-assistent`). При переименовании slug
   **не меняется** — наш rename-orchestrator об этом не предупреждает.
4. **`oauthAppId == "null"` (строка)** в свежесозданном черновике.
   `build_*_draft_payload` ставит `oauthAppId: None` (тип-корректно), но если
   читать через `get_skill`, придёт строка `"null"` — нужен sanitizer при будущей
   реализации `get_skill`.

### 6.8. Что НЕ поменялось бы

Ряд решений, проверенных Phase 3, остаются правильными:
- ✅ Pre-computed URLs от вызывающего — гибко.
- ✅ `Failures as values` для оркестратора — правильнее exceptions для CI.
- ✅ `SkillCreationArtifacts` как frozen dataclass + `progress_cb` — пайплайн
  легко возобновляется. Подтверждено сценариями 5.5, 5.6.
- ✅ Authentication как ответственность caller'а — гибко, decoupled.
- ✅ Текущий payload `create_app` совпадает с UI 1:1 (5.5).

---

## История изменений документа

- 2026-05-06: создан скелет, заполнен раздел 1 (baseline покрытия и аудит кода).
- 2026-05-06: Phase 1 закрыта — секции 2 (документация) и 3 (OSS).
- 2026-05-06: Phase 2 — план Playwright-сценария.
- 2026-05-06: Phase 3 завершена — разделы 5.1–5.12. Карта 18 endpoints,
  7 покрыто, 11 кандидатов на расширение.
- 2026-05-06: Phase 4 завершена — приоритизированный backlog (P0-P3),
  концептуальная карта модуля, quick-wins для 1.1.0-rc1.
- 2026-05-07: Phase 5 — Playwright-probe NLU-интентов (раздел 7 ниже).

---

## 7. Phase 5 — NLU-интенты (Playwright-probe 2026-05-07)

**Цель:** найти точные endpoints + payload-shape для управления custom-интентами
(grammar DSL Yandex Dialogs) программно. Раньше предполагалось, что интенты
лежат внутри `/draft/update` payload как поле `grammar` / `nluSettings`. Probe
показал — это **отдельный API**, ровно 5 endpoints, индивидуальные интенты
не часть main draft.

### 7.1. Endpoints

| # | Method | URL | Channel param | Описание |
|---|---|---|---|---|
| 1 | GET | `/developer/app-store-api/apps/{id}/intents/drafts?channel=aliceSkill` | required | Список черновиков интентов навыка |
| 2 | GET | `/developer/app-store-api/apps/{id}/intents/drafts/{intent_id}?channel=aliceSkill` | required | Один черновик интента |
| 3 | POST | `/developer/app-store-api/apps/{id}/intents/draft?channel=aliceSkill` | required | Создание нового интента (UUID генерится сервером) |
| 4 | PATCH | `/developer/app-store-api/apps/{id}/intents/{intent_id}/draft?channel=aliceSkill` | required | Обновление интента |
| 5 | DELETE | `/developer/app-store-api/apps/{id}/intents/{intent_id}/draft` | **NOT used** | Удаление интента |

**Внимание на разницу URL:**
- Список и GET одного: `/intents/drafts` (множественное число)
- POST/PATCH/DELETE: `/intents/[{id}/]draft` (единственное число)
- DELETE — единственный endpoint без `?channel` параметра

### 7.2. Payload-shape

**Все CRUD-методы** работают с этой моделью:

```json
{
    "id": "c5245aef-0fe0-4a9f-bbde-b12364f623d4",
    "humanReadableName": "test_probe_intent",
    "formName": "test.probe",
    "sourceText": "",
    "positiveTests": "",
    "negativeTests": "",
    "isActivation": false,
    "status": "NEW"
}
```

| Поле | Тип | Назначение | Кто пишет |
|---|---|---|---|
| `id` | string (UUID) | Уникальный идентификатор | server (на create), client (на update) |
| `humanReadableName` | string | Имя для UI дев-консоли | client |
| `formName` | string | Идентификатор интента в коде грамматики (например, `play.specific`) | client |
| `sourceText` | string | Исходник грамматики DSL | client |
| `positiveTests` | string | Тестовые фразы (одна на строку) | client |
| `negativeTests` | string | Анти-тесты | client |
| `isActivation` | bool | Маркер активирующего интента (TBD) | client |
| `status` | enum | `NEW` / `INVALID_GRAMMAR` / другие | server |

### 7.3. POST (create) — пустой запрос → shell-интент

POST с пустым телом `{}` создаёт shell-интент:
```json
{
    "result": {
        "id": "c5245aef-...",
        "humanReadableName": "",
        "status": "NEW",
        "isActivation": false,
        "formName": "",
        "sourceText": "",
        "positiveTests": "",
        "negativeTests": ""
    }
}
```

UUID генерится сервером, нужен для последующего PATCH. Паттерн «two-phase
create»: пустой POST → PATCH с реальными данными.

### 7.4. PATCH — синхронная валидация грамматики

Запрос — полный объект с полями выше. Ответ при невалидной грамматике
(HTTP 200, ошибка внутри payload):

```json
{
    "result": {
        "intent": {
            "id": "...",
            "humanReadableName": "test_probe_intent",
            "status": "INVALID_GRAMMAR",
            "isActivation": false,
            "formName": "test.probe",
            "sourceText": "",
            "positiveTests": "",
            "negativeTests": ""
        },
        "validationError": {
            "errorCode": "VALIDATION_ERROR",
            "errorBounds": {
                "charCount": 52,
                "charOffset": -4,
                "lineNumber": -1
            },
            "text": "Неизвестный элемент \"root\""
        }
    }
}
```

**Важно:**
- HTTP 200 даже при невалидной грамматике — статус ошибки внутри payload.
- `validationError` присутствует только при ошибке; на успехе обёртка `{result: {intent: {...}}}` без него.
- `errorBounds` — позиция ошибки в `sourceText` для UI-подсветки.

### 7.5. Архитектурные импликации для библиотеки

1. **Интенты — отдельный жизненный цикл от main draft.** НЕ нужно «два цикла модерации» (как опасались в плане ма-провайдера). Можно создавать/обновлять интенты ДО `request_deploy` — они уйдут в одну модерацию вместе с draft.

2. **Создание интента — два запроса.** POST (создаёт shell с UUID) → PATCH (заполняет содержимое). Библиотека должна инкапсулировать это в одну операцию `create_intent(...)`.

3. **Идемпотентность через `formName`.** Серверный `id` рандомен на каждый POST, поэтому декларативный API (`set_intents([...])`) должен матчить существующие интенты по `formName` — единственная сторона уникальности под контролем разработчика.

4. **Diff-стратегия для idempotent `set_intents`:**
   - `list_intents()` → текущее состояние.
   - Для каждого желаемого интента: если `formName` существует — PATCH, иначе POST+PATCH.
   - Удалить интенты, чьего `formName` нет в desired set.

5. **Валидация — server-side, но синхронная.** Можно вернуть caller-у `IntentValidationError` из PATCH-результата без отдельного `validate` endpoint.

6. **Заголовки CSRF и cookies** — те же, что у других endpoints (используем существующий механизм `_patch_json` etc.).

### 7.6. Не охвачено probe-ом (TODO)

- Endpoint(ы) для **сущностей** (`entity Player: values: ...`) — на странице Интенты есть Monaco-редактор «Сущности», но мы не успели сохранить там что-то и поймать сетевой запрос. Вероятная схема — `/apps/{id}/custom-entities/draft` или похожая.
- `isActivation: true` — назначение поля. Гипотеза: интент-маркер для активационных фраз, заменяющий entries в `activationPhrases`.
- Endpoint списка всех опубликованных (не draft) интентов: `/intents` (без `/drafts`)? — UI его не дёргал.
- Запрос `/intents/{id}/test` — кнопка «Протестировать» в UI; стоит probe-нуть отдельно для validation API.

---

## История изменений документа

- 2026-05-06: создан скелет, заполнен раздел 1 (baseline покрытия и аудит кода).
