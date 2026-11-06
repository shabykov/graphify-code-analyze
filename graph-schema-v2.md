# Схема графа для анализа импакта изменений (v2)

Граф отвечает на три вопроса агента, которому дали описание задачи (тикет, ADR, свободный текст) и имя репозитория:

1. **Где в коде это изменение?** — от слов тикета к файлам, функциям, эндпоинтам, таблицам.
2. **Что внутри репозитория зависит от этих мест?** — вызовы, реализации интерфейсов, типы, тесты, данные.
3. **Какие другие сервисы задеты?** — через контракты (HTTP, SSE, схемы DTO), общие данные (БД, Redis), события.

Агент пишет Cypher сам через тонкие tools. Поэтому схема оптимизирована под агента, а не под компактность хранения: один граф, один ключ `key` у каждого узла, небольшой словарь меток и типов рёбер, у каждого узла кода — файл и строки, у каждого ребра — ссылка на источник.

Документ заменяет `graph-schema.md` (межсервисный граф Kafka/gRPC для Go-монорепы). Что сохранено, что изменено — в разделе 11. Примеры ниже — на условной паре сервисов: `orders-api` (Go, chi, clean architecture, OpenAPI) и `orders-web` (SvelteKit, TypeScript), который ходит в этот API. Имена репозиториев, файлов и сущностей в примерах иллюстративны.

---

## 1. Уровни

```
L3  Знания        Adr  Issue  PullRequest  DocChunk  Concept
                   │ MENTIONS / CITES / ABOUT
                   ▼
L2  Контракты     HttpEndpoint  Schema  Field  SseEvent  UiRoute        ← шов между репами
    и данные      Table  Column  CacheKey  Channel  Event  ExternalSystem  ConfigKey
                   ▲ HANDLES / CALLS_ENDPOINT / READS / WRITES / PRODUCES / CONSUMES …
                   │
L1  Код           Package  File  Symbol  ── CALLS / IMPLEMENTS / REFERENCES / IMPORTS / TESTS
                   ▲ CONTAINS
                   │
L0  Репозитории   Repo  Component
```

| Уровень | Что это | Откуда берётся | Какой вопрос закрывает |
|---|---|---|---|
| L0 | репозиторий и его деплоящиеся приложения | git, `cmd/*`, `package.json` | границы сервиса, какой коммит проиндексирован |
| L1 | структура кода: пакеты, файлы, символы и связи между ними | краулер на родном тулчейне языка: `crawler-go` (`go/packages`, `go/types`) для Go; TS-краулер на TypeScript compiler API — планируется | вопрос 2: что зависит от чего внутри репы |
| L2 | «швы»: контракты, точки интеграции, хранилища | роутер, OpenAPI, SQL в коде, миграции, Redis-ключи, pub/sub, SSE, HTTP-клиент веба | вопрос 3: как изменение выходит наружу |
| L3 | решения: ADR, issues, PR, фрагменты документов, понятия домена | `docs/adr`, GitHub (`gh`) | вопрос 1: от текста тикета к коду |

Уровни L1–L2 детерминированы (`confidence: 1.0`). L3 частично: ссылки из документов на код резолвятся по точному совпадению; эмбеддинги и понятия — вероятностные, с `confidence < 1`.

---

## 2. Принципы

**Один граф на все репозитории.** FalkorDB не умеет запросы между графами (проверено), а импакт между сервисами — это один путь через узел-шов. Имя графа — `impact`. Ориентир по объёму для пары сервисов среднего размера: ~6 тыс. символов, ~700 файлов, ~60 тыс. рёбер — для FalkorDB это секунды.

**`key` у каждого узла.** Уникален в пределах метки, читаем человеком, воспроизводим экстрактором. Агенту достаточно `MATCH (n {key: $k})` для любого узла. Формат ключа — в таблицах ниже.

**`repo` у каждого узла и ребра.** Какая репа произвела этот факт. По нему делается переиндексация одной репы. Узлы-швы (L2) принадлежат владельцу (`owner_repo`), но на них ссылаются рёбра из других реп.

**Канонические ключи швов одинаковы из обеих реп.** `GET /api/v1/orders/{}` получается и из `router.go` (`/orders/{id}` под `r.Route("/api/v1")`), и из `api.ts` (`` `/orders/${id}` `` с `BASE=/api/v1`), и из `order-api.yaml`, и из Playwright `page.route('**/api/v1/orders/*')`. Поэтому `MERGE` из разных экстракторов попадает в один узел. Правила нормализации — в разделе 3.3.

**Ребро — свидетельство.** На каждом ребре: `source_ref` (`<repo>@<sha>:<path>:L<n>`), `extracted_by`, `confidence`, `repo`, `claim_id` (хэш от `type + from.key + to.key + source_ref`). Два разных свидетельства одной связи — два ребра. Уникальность `claim_id` гарантирует FalkorDB (constraint на свойство ребра, проверено).

**Расположение у каждого узла кода.** `file`, `line_start`, `line_end` на `Symbol`; `source_ref` на остальных. Tool `code.read(source_ref)` открывает место без поиска.

**Маленький словарь.** 24 метки, 34 типа рёбер, у каждого типа одна семантика. Словарь лежит в самом графе (метка `Meta`), первый запрос агента — `MATCH (m:Meta) RETURN m`.

**Детерминированное первым.** Всё, что можно вывести компилятором или парсером, выводится им. LLM и эмбеддинги добавляют связи с `confidence < 1` и `extracted_by: 'embedding'` или `'agent:<prompt-id>'`, никогда не перезаписывают детерминированные.

**Нерезолвленное хранится.** Если документ упоминает путь, функцию или эндпоинт, которых в графе нет, — создаётся `Unresolved {name, kind}`. Агент видит «ADR ссылается на то, чего уже нет» — это само по себе находка.

---

## 3. Узлы

Общие свойства всех узлов: `key`, `repo` (для швов — `owner_repo`), `name` (короткое имя для вывода), `source_ref`.

### 3.1 L0 — репозитории

| Метка | `key` | Свойства | Источник |
|---|---|---|---|
| `Repo` | имя репы: `orders-api` | `lang` (`go`/`ts`), `indexed_commit`, `indexed_at`, `default_branch`, `remote` | `git` |
| `Component` | `<repo>/<app>`: `orders-api/api`, `orders-web/web` | `kind` (`api`/`web`/`consumer`/`cron`), `entry` (`cmd/api/main.go`, `svelte.config.js`), `runtime` | Go: каждый `package main` под `cmd/`; web: один компонент на приложение |

### 3.2 L1 — код

| Метка | `key` | Свойства | Источник |
|---|---|---|---|
| `Package` | `<repo>:<путь директории от корня>`: `orders-api:internal/usecase/order/patch`, `orders-web:src/lib/stores` | `path`, `import_path` (Go), `layer`, `doc` (package comment) | `crawler-go` (`go/packages`); директории `src/**` |
| `File` | `<repo>:<path>`: `orders-api:internal/delivery/http/router.go` | `path`, `lang`, `kind` (`source`/`test`/`migration`/`spec`/`doc`/`config`/`style`), `lines`, `hash` | обход дерева |
| `Symbol` | `<repo>:<fqn>` — Go в нотации SSA, путь пакета от корня репы: функция `orders-api:internal/domain/order.New`, метод `orders-api:(*internal/delivery/http/handler/order/managing.Handler).GetByID`, интерфейсный метод `orders-api:(internal/usecase/adapters/storage.OrderStatusUpdater).UpdateStatus`; TS: `orders-web:src/lib/api.ts#api.getOrder`, Svelte: `orders-web:src/lib/components/order/OrderRow.svelte` | `kind`, `fqn`, `file`, `line_start`, `line_end`, `exported`, `signature`, `doc` (комментарий), `tokens` (имя, разбитое на слова: `get by id`), `layer`, `summary` (фаза 2, LLM), `emb` (фаза 2) | `crawler-go` (AST + типы); TS-краулер — планируется |

`layer` выводится из пути и даёт агенту «архитектурную» фильтрацию без знания структуры: для Go-API в стиле clean architecture — `delivery`, `usecase`, `domain`, `repository`, `pkg`, `cmd`; для SvelteKit — `routes`, `components`, `stores`, `utils`, `server`, `tests`.

`Symbol.kind` по языкам:

| Go | TS / Svelte |
|---|---|
| `func`, `method`, `type` (struct), `interface`, `interface_method`, `const`, `var`, `test` (`Test*`, `Benchmark*`) | `function`, `method`, `class`, `type` (type/interface), `const`, `store` (экспорт из `stores/*`), `api_method` (ключ объекта `api` в `api.ts`), `component` (`.svelte`), `page` (`+page.svelte`, `+layout.svelte`), `load` (`+page.ts`, `+page.server.ts`), `test` (`*.test.ts`, `*.spec.ts`) |

Интерфейсные методы — отдельные символы (`kind: interface_method`). Это важно для clean architecture: use case'ы зависят от портов (`internal/usecase/adapters/storage/order.go` → `OrderUpdater`), реализации живут в `internal/repository/storage/order/postgresql/`. Вызов `uc.storage.UpdateStatus(...)` статически ведёт в интерфейсный метод; к реализации ведёт `IMPLEMENTS` (раздел 4).

### 3.3 L2 — контракты, точки интеграции, данные

| Метка | `key` | Свойства | Источник |
|---|---|---|---|
| `HttpEndpoint` | `<METHOD> <path_pattern>`, параметры заменены на `{}`: `GET /api/v1/orders/{}/status` | `method`, `path` (с именами параметров из OpenAPI: `/api/v1/orders/{id}/status`), `path_pattern`, `operation_id`, `summary`, `description`, `tags[]`, `auth` (`bearer`/`sse_token`/`none`), `rate_limit` (`write`/`upload`/`retry`/`otp_request`/…), `middlewares[]`, `spec_ref` (`orders-api@<sha>:api/order-api.yaml:L<n>`), `owner_repo` | `router.go` (chi: `r.Get/Post/…`, префиксы `r.Route`, `r.With(...)` → `middlewares`), `api/*.yaml` (`paths`), `api.ts` (клиент), Playwright `page.route` |
| `Schema` | `<owner_repo>:<name>`: `orders-api:Order` | `name`, `spec_file`, `spec_ref`, `kind` (`object`/`enum`/`array`), `description`, `enum_values[]` | `components.schemas` в `api/*.yaml` |
| `Field` | `<owner_repo>:<Schema>.<field>`: `orders-api:Order.merchant_name` | `name`, `type`, `format`, `required`, `nullable`, `description`, `enum_values[]` | `properties` схемы |
| `SseEvent` | `<endpoint_key>#<event>`: `GET /api/v1/orders/{}/events#status_change` | `name`, `description`, `spec_ref` | `fmt.Fprintf(w, "event: %s…")` + `Name()` типов событий; описание SSE в yaml |
| `UiRoute` | `<repo>:<route>`: `orders-web:/orders/[id]` | `path`, `params[]`, `kind` (`page`/`layout`), `title` | `src/routes/**/+page.svelte` |
| `Table` | `<owner_repo>.<table>`: `orders-api.orders` | `name`, `db` (`postgresql`), `migration_ref` (где создана) | миграции `migrations/*.up.sql` (итоговое состояние после применения всех) |
| `Column` | `<owner_repo>.<table>.<column>`: `orders-api.orders.merchant_name` | `name`, `type`, `nullable`, `default`, `migration_ref` | миграции |
| `CacheKey` | `<owner_repo>:<pattern>`: `orders-api:cart:{}` | `pattern`, `store` (`redis`), `ttl`, `builder` (fqn функции-строителя ключа, если есть: `otpKey`) | `rueidis` builder `.Key(...)`: конкатенации и `fmt.Sprintf`, переменные части → `{}` |
| `Channel` | `<owner_repo>:<transport>:<pattern>`: `orders-api:redis:order:{}` | `pattern`, `transport` (`redis_pubsub`/`kafka`/`nats`) | `.Publish().Channel(...)`, `.Subscribe().Channel(...)` |
| `Event` | `<owner_repo>:<fqn типа>`: `orders-api:events.StatusChange` | `name`, `fqn`, `fields[]` | типы, которые передаются в `Dispatch(ctx, event)` и принимаются `Consume(ctx, handler)` |
| `ExternalSystem` | имя: `postgresql`, `redis`, `openai`, `stripe`, `sendgrid`, `google_oauth` | `kind` (`llm`/`mail`/`db`/`cache`/`oauth`/`payments`), `sdk` (`github.com/openai/openai-go/v3`) | импорты SDK в `go.mod`/`package.json` + таблица соответствия модуль → система (конфиг экстрактора) |
| `ConfigKey` | `<owner_repo>:<ENV_NAME>`: `orders-api:OPENAI_API_KEY` | `name`, `default`, `doc` (из `.env.example`) | `os.Getenv`, `godotenv`, `import.meta.env.VITE_*`, `.env.example` |
| `Unresolved` | `<repo>:<kind>:<name>` | `name`, `kind` (`path`/`symbol`/`endpoint`/`table`/`issue`/`adr`), `reason` | любой экстрактор, не нашедший цель |

**Нормализация пути эндпоинта.** Канон — OpenAPI. `path_pattern` получается заменой любого параметра на `{}`: chi `{id}`/`{order_id}`, OpenAPI `{id}`, TS `${id}`, Playwright `*` или `${ORDER_ID}` → `{}`. Префикс: chi — сумма вложенных `r.Route`; TS — значение `BASE` (`/api/v1`) или `AUTH_BASE` (`/api/auth`), которое экстрактор берёт из констант `api.ts`; Playwright — хвост glob'а после `**`. Квери-строка отбрасывается. Если OpenAPI не содержит эндпоинт, который есть в роутере (или наоборот), узел всё равно создаётся с флагом `in_spec: false` / `in_code: false` — это расхождение контракта и кода, агент его увидит.

### 3.4 L3 — знания

| Метка | `key` | Свойства | Источник |
|---|---|---|---|
| `Adr` | `<repo>:ADR-<n>`: `orders-api:ADR-012` | `number`, `title`, `status` (строка `**Статус:**` дословно), `date`, `file`, `summary` (первый абзац/цитата), `emb` (фаза 2) | `docs/adr/*.md`, индекс `docs/adr/README.md` |
| `Issue` | `<repo>#<n>`: `orders-api#40`, `orders-web#21` | `number`, `title`, `state`, `labels[]`, `url`, `body` (усечённое), `created_at`, `closed_at`, `sections[]` | `gh issue list --state all --json …` или API трекера; разделы тела по заголовкам («Указатели в коде», «Контракт», «Зависимости» и т. п.) парсятся из него |
| `PullRequest` | `<repo>!<n>`: `orders-api!80` | `number`, `title`, `merged_at`, `branch` | `gh pr list --state all --json …` |
| `DocChunk` | `<doc_key>#<n>`: `orders-api:ADR-012#3` | `heading`, `text`, `line_start`, `line_end`, `emb` | разбиение ADR, issue, README, `CLAUDE.md`, `info.description` в OpenAPI по заголовкам |
| `Concept` | `concept:<slug>`: `concept:refund` | `name`, `aliases[]` (`возврат`, `refund`, `chargeback`), `description` | фаза 1: теги OpenAPI, имена доменных пакетов (`domain/order/refund`), таблицы; фаза 2: LLM по `summary` символов |

### 3.5 Meta — самоописание

| Метка | `key` | Свойства |
|---|---|---|
| `Meta` | `label:Symbol`, `rel:CALLS`, `example:<n>` | `kind` (`label`/`rel`/`example`), `name`, `description`, `key_format`, `properties[]`, `from[]`, `to[]`, `implemented` (есть ли экстрактор), `cypher` (у примеров) |

Содержимое `Meta` — это таблицы разделов 3 и 4, загруженные в граф из `scripts/graphify/meta.py` при каждой индексации. `graph.py schema` печатает их; агент не держит схему в промпте и не отстаёт от неё, а по `implemented` видит, какие метки и рёбра в графе пока отсутствуют.

---

## 4. Рёбра

Общие свойства: `claim_id`, `repo`, `commit`, `source_ref`, `extracted_by`, `confidence`. У рёбер из кода — `line`.

### 4.1 Структура

| Тип | from → to | Свойства | Источник |
|---|---|---|---|
| `CONTAINS` | Repo → Component · Repo → Package · Package → File · File → Symbol · Symbol(type) → Symbol(method) | | компилятор |
| `IMPORTS` | Package → Package (Go) · File → File (TS) | `names[]` | компилятор |
| `DEFINES` | File(migration) → Table \| Column | `op` (`create`/`alter`/`drop`), `line` | миграции |

### 4.2 Код

| Тип | from → to | Свойства | Источник |
|---|---|---|---|
| `CALLS` | Symbol → Symbol | `line`, `via` (`static` — прямой вызов; `interface` — вызов интерфейсного метода; `value` — функция передана как значение (callback, регистрация обработчика); `derived` — вычислено через `IMPLEMENTS` после загрузки, `confidence: 0.8`) | Go: `crawler-go` по `TypesInfo.Uses` (вызовы внутри замыканий относятся к объемлющей функции); TS: краулер на TypeScript compiler API |
| `IMPLEMENTS` | Symbol(type) → Symbol(interface) · Symbol(method) → Symbol(interface_method) | | `crawler-go`: `types.Implements` для каждой пары тип × интерфейс репозитория (моки входят, помечены `is_mock`); для TS — `implements`/структурная совместимость экспортируемых типов |
| `REFERENCES` | Symbol → Symbol(type) · Schema → Schema | `line`, `how` (`param`/`return`/`field`/`embeds`/`instantiates`/`uses`) | `crawler-go`: использования типов в сигнатурах, полях и телах; `$ref` в OpenAPI |
| `TESTS` | Symbol(test) → Symbol | `how` (`calls` — тест вызывает символ; `names` — имя теста содержит имя символа: `TestOrder_MerchantName` → `Order.MerchantName`) | исходящие вызовы теста + эвристика по имени |
| `MOCKS` | Symbol(test) → HttpEndpoint | `line`, `pattern` | Playwright `page.route(...)`, `msw`, `httptest` |
| `VISITS` | Symbol(test) → UiRoute | `line` | `page.goto('/orders/all')` |

Как агент проходит через порт: `(uc)-[:CALLS {via:'interface'}]->(im:Symbol {kind:'interface_method'})<-[:IMPLEMENTS]-(impl)`. Чтобы не писать это каждый раз, после загрузки вычисляется производное ребро `CALLS {via:'derived', confidence: 0.8}` напрямую `uc → impl` для каждой реализации интерфейса, кроме моков (`is_mock`) и типов из тестовых файлов. Агент пишет `-[:CALLS*1..6]->` и получает оба мира.

### 4.3 Привязка кода к контрактам

| Тип | from → to | Свойства | Источник |
|---|---|---|---|
| `HANDLES` | Symbol(handler) → HttpEndpoint · Symbol(page/load) → UiRoute | `line`, `middlewares[]` | `router.go`; `src/routes/**` |
| `CALLS_ENDPOINT` | Symbol → HttpEndpoint | `line`, `method`, `how` (`fetch`/`xhr`/`event_source`/`http_client`) | `request(...)` в `api.ts`, `new EventSource(url)`, Go `http.NewRequest` |
| `ACCEPTS` | HttpEndpoint → Schema | `in` (`body`/`query`/`path`/`multipart`) | `requestBody`, `parameters` |
| `RETURNS` | HttpEndpoint → Schema · SseEvent → Schema | `status`, `error_codes[]` (из примеров `error_code`) | `responses` |
| `HAS_FIELD` | Schema → Field | `required` | `properties` |
| `SERIALIZES` | Symbol(dto struct) → Schema | `match` (`name`/`json_tags`) | `handler/**/dto.go`: имя структуры = имя схемы, иначе совпадение набора json-тегов с полями схемы |
| `MIRRORS` | Symbol(TS type) → Schema | `match`, `drift[]` (поля схемы, которых нет в типе, и наоборот) | `src/lib/types.ts` |
| `USES_FIELD` | Symbol → Field | `line`, `access` (`read`/`write`) | TS type checker: доступ `order.merchant_name`, где тип выражения `MIRRORS` схему; Go: обращение к полю DTO-структуры |
| `EMITS` | HttpEndpoint → SseEvent | `line` | `fmt.Fprintf(w, "event: …")` в обработчике эндпоинта |
| `LISTENS` | Symbol → SseEvent | `line` | `es.addEventListener('status_change', …)` |
| `NAVIGATES_TO` | Symbol → UiRoute | `line` | `goto('/…')`, `href="/…"` |

### 4.4 Данные и внешние системы

| Тип | from → to | Свойства | Источник |
|---|---|---|---|
| `READS` / `WRITES` | Symbol → Table \| Column \| CacheKey | `line`, `sql` (усечённый текст запроса), `columns[]` (для Table, если Column-уровень не разрешён) | SQL-литералы в аргументах `QueryContext`/`ExecContext`/`QueryRowContext` (+ константы, склеенные через `+`): `FROM`/`JOIN` → READS, `INSERT`/`UPDATE`/`DELETE` → WRITES; Redis `Get/Hgetall` → READS, `Set/Hset/Del/Expire` → WRITES |
| `PRODUCES` / `CONSUMES` | Symbol → Event \| Channel | `line` | `Dispatch(ctx, e)` → PRODUCES Event; `Publish().Channel(c)` → PRODUCES Channel; `Consume(ctx, uc.Method)` → метод CONSUMES Event; `Subscribe().Channel(c)` → CONSUMES Channel |
| `CALLS_EXTERNAL` | Symbol → ExternalSystem | `line`, `operation` (`chat.completions`, `emails.send`) | вызовы методов SDK |
| `READS_CONFIG` | Symbol → ConfigKey | `line` | `os.Getenv("X")`, `import.meta.env.X` |

### 4.5 Производные

| Тип | from → to | Свойства | Как считается |
|---|---|---|---|
| `REACHES` | HttpEndpoint \| UiRoute \| Symbol(consumer) → Table \| Column \| CacheKey \| Channel \| Event \| ExternalSystem \| HttpEndpoint | `path[]` (fqn-цепочка, не длиннее 12), `path_len`, `derived: true` | от обработчика точки входа по `CALLS*` до любого ребра из 4.4 или `CALLS_ENDPOINT`; одно ребро на пару (кратчайший путь). Пересчитывается после каждой загрузки репы |

`REACHES` — это `FLOWS_TO` из старой схемы, но с явным путём. Он даёт ответ «что задевает `GET /api/v1/orders/{}`» за один хоп: таблицы `orders`, `order_items`, `payments`, ключ `order:{}`, внешних систем нет. В сервисах с общей шиной событий `REACHES` переаппроксимирует: из обработчика достижимы почти все исходящие точки. Там, где обработчики изолированы по use case'ам, ответ точный.

### 4.6 Знания

| Тип | from → to | Свойства | Источник |
|---|---|---|---|
| `MENTIONS` | Adr \| Issue \| PullRequest \| DocChunk → File \| Symbol \| HttpEndpoint \| Schema \| Field \| Table \| Column \| UiRoute \| ConfigKey \| Unresolved | `line`, `kind` (`path`/`symbol`/`endpoint`/`field`/`table`/`test`/`error_code`), `text` (как написано в документе) | регулярные выражения по тексту: пути в бэктиках (`internal/domain/order/type.go`), `METHOD /path`, `Type.Method`/`Func`, `Test*`, имена таблиц и полей (`merchant_name`), `error_code` в верхнем регистре. Резолв — точное совпадение с узлом той же репы (для web-issue, упоминающих эндпоинты, — с `owner_repo` API); иначе `Unresolved` |
| `CITES` | Adr \| Issue \| PullRequest \| DocChunk → Adr \| Issue \| PullRequest | `text` | `ADR-009`, `#40`, `orders-web#21` |
| `DEPENDS_ON` | Issue → Issue \| Adr | `text` | раздел зависимостей в issue клиента; «выкатывается после orders-web#21» в ADR |
| `SUPERSEDES` | Adr → Adr | `partial` (bool) | «частично переопределён ADR-009» в индексе ADR |
| `CHUNK_OF` | DocChunk → Adr \| Issue \| File | `order` | разбиение документа |
| `ABOUT` | Concept → Package \| Symbol \| HttpEndpoint \| Table \| Schema · DocChunk → Concept | `confidence` | фаза 1: детерминированно по тегам и именам; фаза 2: LLM |
| `ALIAS_OF` | Unresolved → любой узел | `confidence` | агент-резолвер; выход проверяется на существование цели |

---

## 5. Сквозной пример: ADR-012 «пустой `merchant_name`»

Тикет: *«API отдаёт пустую строку `merchant_name` для заказа с неизвестным продавцом вместо заглушки „Заказ от …“; подпись строит клиент».* `SERVICE = orders-api`.

Как выглядит граф вокруг этой задачи:

```
(:Adr {key:'orders-api:ADR-012', status:'Частично реализован…'})
  -[:MENTIONS {kind:'path'}]->   (:File {key:'orders-api:internal/domain/order/type.go'})
  -[:MENTIONS {kind:'symbol'}]-> (:Symbol {key:'orders-api:(*internal/domain/order.GeneralData).MerchantName'})
  -[:MENTIONS {kind:'symbol'}]-> (:Symbol {key:'orders-api:internal/delivery/http/handler/order.FromOrder'})
  -[:MENTIONS {kind:'field'}]->  (:Field  {key:'orders-api:Order.merchant_name'})
  -[:CITES]->                     (:Issue  {key:'orders-api#40'})
  -[:CITES]->                     (:Issue  {key:'orders-web#21'})
  -[:CITES]->                     (:PullRequest {key:'orders-api!57'})

(:Issue {key:'orders-web#21'})-[:DEPENDS_ON]->(:Issue {key:'orders-api#40'})
(:Issue {key:'orders-web#21'})-[:MENTIONS]->(:Symbol {key:'orders-web:src/lib/components/order/OrderRow.svelte'})

(:Schema {key:'orders-api:Order'})-[:HAS_FIELD]->(:Field {key:'orders-api:Order.merchant_name'})
(:HttpEndpoint {key:'GET /api/v1/orders/{}'})-[:RETURNS {status:200}]->(:Schema {key:'orders-api:Order'})
(:Symbol {key:'orders-api:(*internal/delivery/http/handler/order/managing.Handler).GetByID'})-[:HANDLES {line:148}]->(:HttpEndpoint {key:'GET /api/v1/orders/{}'})
(:Symbol {key:'orders-api:internal/delivery/http/handler/order.OrderDTO'})-[:SERIALIZES]->(:Schema {key:'orders-api:Order'})

(:Symbol {key:'orders-web:src/lib/api.ts#api.getOrder'})-[:CALLS_ENDPOINT {line:515}]->(:HttpEndpoint {key:'GET /api/v1/orders/{}'})
(:Symbol {key:'orders-web:src/lib/types.ts#Order'})-[:MIRRORS]->(:Schema {key:'orders-api:Order'})
(:Symbol {key:'orders-web:…/OrderRow.svelte'})-[:USES_FIELD {access:'read'}]->(:Field {key:'orders-api:Order.merchant_name'})

(:Symbol {key:'orders-api:(*internal/repository/storage/order/postgresql.Repository).Upsert'})-[:WRITES]->(:Column {key:'orders-api.orders.merchant_name'})
```

Шаги агента и запросы, которые он пишет:

**Шаг 1. Точки входа.** Тикет содержит идентификатор `merchant_name` — точное совпадение по полнотекстовому индексу, плюс семантический поиск по тексту тикета среди `DocChunk` (находит ADR-012 и issue #40 по словам «неизвестный продавец», «заглушка»):

```cypher
CALL db.idx.fulltext.queryNodes('Field', 'merchant_name') YIELD node AS f
MATCH (f)<-[:HAS_FIELD]-(s:Schema)
OPTIONAL MATCH (a)-[m:MENTIONS]->(f) WHERE a:Adr OR a:Issue
RETURN f.key, s.key, collect(DISTINCT a.key) AS docs
```

**Шаг 2. Scope в `orders-api`.** Кто формирует значение поля и кто его отдаёт наружу:

```cypher
MATCH (s:Schema {key:'orders-api:Order'})<-[:SERIALIZES]-(dto:Symbol)
MATCH (dto)<-[:REFERENCES|CALLS*1..3]-(h:Symbol {layer:'delivery'})-[:HANDLES]->(e:HttpEndpoint)
RETURN DISTINCT e.key, h.key, h.file, h.line_start
```

```cypher
MATCH (c:Column {key:'orders-api.orders.merchant_name'})<-[w:WRITES|READS]-(sym:Symbol)
RETURN type(w), sym.key, w.source_ref
```

**Шаг 3. Характер изменения.** Контракт не меняется структурно (поле остаётся строкой), меняется семантика значения. Это видно по `Field.nullable`/`type` — агент фиксирует: «семантика, не схема».

**Шаг 4. Распространение в `orders-web`.** Кто читает поле и на каких страницах:

```cypher
MATCH (f:Field {key:'orders-api:Order.merchant_name'})<-[u:USES_FIELD]-(sym:Symbol {repo:'orders-web'})
OPTIONAL MATCH (sym)<-[:CALLS|REFERENCES*0..4]-(p:Symbol {kind:'page'})-[:HANDLES]->(r:UiRoute)
RETURN sym.file, u.line, collect(DISTINCT r.key) AS pages
```

Если `USES_FIELD` не извлёкся (фаза 1), путь длиннее, но тоже за один запрос: эндпоинты, возвращающие схему → `CALLS_ENDPOINT` → вызывающие символы → страницы.

**Шаг 5. Тесты, которые закрепляют поведение:**

```cypher
MATCH (t:Symbol {kind:'test'})-[:TESTS]->(:Symbol {key:'orders-api:(*internal/domain/order.GeneralData).MerchantName'})
RETURN t.key, t.file
UNION
MATCH (t:Symbol {kind:'test', repo:'orders-web'})-[:MOCKS]->(:HttpEndpoint {key:'GET /api/v1/orders/{}'})
RETURN t.key, t.file
```

Отчёт агента по этим пяти запросам должен совпасть с тем, что в ADR написано вручную в разделе о влиянии на клиент: `OrderRow.svelte` решает по пустому имени, что заказ ещё обрабатывается; порядок выкатки — сначала веб (`orders-web#21`), потом бэкенд.

---

## 6. Поиск точек входа по тексту тикета

Язык тикета может не совпадать с языком кода: «неизвестный продавец», «возврат» в тексте против `merchant_name`, `refund` в коде. Три пути от текста к узлам, от точного к вероятностному:

| Путь | Что ищем | Где | Инструмент FalkorDB |
|---|---|---|---|
| 1. Идентификаторы в тексте | пути, `METHOD /path`, `Type.Method`, `error_code`, имена таблиц и полей, `ADR-nnn`, `#nn` | `key`, `name` | `MATCH (n {key:…})`, range-индекс |
| 2. Слова в именах и комментариях | `tokens`, `doc`, `summary`, `description` символов, эндпоинтов, схем, полей, таблиц | `Symbol`, `HttpEndpoint`, `Schema`, `Field`, `Table`, `Column`, `UiRoute` | `CREATE FULLTEXT INDEX FOR (n:Symbol) ON (n.name, n.tokens, n.doc)`; запрос с префиксами `ord*` |
| 3. Смысл | текст тикета против фрагментов ADR/issues и `summary` символов — мультиязычные эмбеддинги снимают разрыв русский/английский | `DocChunk.emb`, `Symbol.emb`, `HttpEndpoint.emb` | `CREATE VECTOR INDEX … OPTIONS {dimension: D, similarityFunction:'cosine'}`; `CALL db.idx.vector.queryNodes('DocChunk','emb', 10, vecf32($q))` → затем `MENTIONS` ведут к коду |

Tool `graph.search(text)` делает 1–3 за один вызов и возвращает ключи с оценкой и типом совпадения; агент дальше идёт по графу сам. Путь 3 — фаза 2: требует модель эмбеддингов и фиксированную размерность индекса (размерность задаётся при создании и не меняется).

---

## 7. Пайплайн индексации

Одна команда на репу: `.venv/bin/python scripts/indexer.py <path> --graph impact` (скилл `/graphify <repo> [graph]` запускает её и проверяет результат). Шаги идут в этом порядке, потому что поздние ссылаются на ключи ранних:

| # | Экстрактор | Вход | Выход | Чем |
|---|---|---|---|---|
| 1 | `repo` | `.git`, `cmd/*`, `package.json` | `Repo`, `Component` | `git rev-parse`, обход |
| 2 | `code-go` / `code-ts` | исходники | `Package`, `File`, `Symbol`, `CONTAINS`, `IMPORTS`, `CALLS`, `IMPLEMENTS`, `REFERENCES`, `TESTS` | Go: `crawler-go` (`go/packages` + `go/types`: `Defs`/`Uses`, `types.Implements`), печатает claims-JSON того же формата, что `scripts/graphify/model.py`; TS/Svelte: краулер на TypeScript compiler API + `svelte/compiler` для скрипта компонента — планируется |
| 3 | `openapi` | `api/*.yaml` | `HttpEndpoint`, `Schema`, `Field`, `SseEvent`, `ACCEPTS`, `RETURNS`, `HAS_FIELD`, `REFERENCES` | парсер OpenAPI 3 |
| 4 | `routes` | `router.go` (chi), `src/routes/**` | `HttpEndpoint` (MERGE к шагу 3), `UiRoute`, `HANDLES`, `middlewares` | `crawler-go/internal/routes`: chi (стек префиксов `r.Route`, `r.Group`, `r.Use` внутри групп, цепочки `r.With(...)`) и `http.ServeMux` (`"GET /path"`); обработчик резолвится через типы, иначе `Unresolved`; SvelteKit — из путей файлов |
| 5 | `dto` | `handler/**/dto.go`, `src/lib/types.ts` | `SERIALIZES`, `MIRRORS` (+`drift`), `USES_FIELD` | json-теги структур ↔ поля схем; TS type checker для доступа к полям |
| 6 | `http-client` | `src/lib/api.ts`, SSE-сторы в `src/lib/stores/` | `CALLS_ENDPOINT`, `LISTENS` | AST: шаблонные литералы + `method` в опциях; `new EventSource(url)` |
| 7 | `sql` | `migrations/*.sql`, SQL-литералы в `repository/storage/**` | `Table`, `Column`, `DEFINES`, `READS`, `WRITES` | применить миграции по порядку в памяти (`CREATE`/`ALTER`/`DROP`); SQL-литералы — токенизатор: `FROM`/`JOIN`/`INTO`/`UPDATE`/`DELETE FROM` + список колонок, где он явный |
| 8 | `markers` | `repository/cache/**`, `repository/bridge/**`, `pkg/bridge/**`, `usecase/adapters/dispatcher/**` | `CacheKey`, `Channel`, `Event`, `READS`/`WRITES`, `PRODUCES`/`CONSUMES`, `EMITS` | маркеры в YAML: `func` + индекс аргумента с идентичностью; например, для Redis-клиента и внутренней шины событий: `(rueidis.Builder).Key`, `.Channel`, `Dispatch`, `Consume`, `fmt.Fprintf(w, "event: …")` |
| 9 | `external` | `go.mod`, `package.json`, импорты | `ExternalSystem`, `CALLS_EXTERNAL`, `ConfigKey`, `READS_CONFIG` | таблица модуль → система; `os.Getenv`, `import.meta.env` |
| 10 | `tests` | Playwright `tests/e2e/**`, `httptest` | `MOCKS`, `VISITS` | AST: `page.route`, `page.goto` |
| 11 | `docs` | `docs/adr/*.md`, `README.md`, `CLAUDE.md`, `info.description` yaml | `Adr`, `DocChunk`, `CHUNK_OF`, `MENTIONS`, `CITES`, `SUPERSEDES`, `Unresolved` | Markdown по заголовкам; регулярные выражения раздела 4.6; резолв по графу |
| 12 | `issues` | `gh issue list --state all --json`, `gh pr list --state all --json` | `Issue`, `PullRequest`, `MENTIONS`, `CITES`, `DEPENDS_ON` | GitHub CLI или API трекера |
| 13 | `derive` | граф | `CALLS {via:'derived'}` к реализациям, `REACHES` | Cypher по графу после загрузки репы |
| 14 | `embed` (фаза 2) | `DocChunk.text`, `Symbol.doc+signature`, `HttpEndpoint.summary+description` | `emb`, `Concept`, `ABOUT` | модель эмбеддингов; LLM для `summary` и понятий |
| 15 | `meta` | таблицы этого документа | `Meta` | один раз на граф |

**Порядок загрузки реп.** Сначала владельцы контрактов (`orders-api`), потом клиенты (`orders-web`): клиентские экстракторы `MERGE` в уже существующие швы и получают `path` с именами параметров из OpenAPI. Обратный порядок тоже работает (MERGE по `key`), но тогда имена параметров подтянутся при загрузке владельца.

**Идемпотентность.** Узлы — `MERGE` по `key` отдельным оператором, рёбра — `MERGE` по `claim_id` и `SET` остальных свойств. Никогда не `MERGE` целого шаблона `(a)-[r]->(b)`: если ребра нет, FalkorDB создаёт и узлы заново (проверено, раздел 8).

**Переиндексация одной репы.** Все рёбра и узлы кода несут `repo`. Шаги: удалить рёбра `{repo: R}` (индекс на `repo` у каждого типа ребра даёт `Edge By Index Scan`), удалить узлы `{repo: R}` с метками L0/L1/L3, оставить швы L2 (на них ссылаются другие репы), загрузить заново, удалить швы без рёбер. Так и реализовано в `scripts/indexer.py`: если репа уже в графе, её прежние claims удаляются перед загрузкой (`--replace` включается сам). Полная пересборка — `--reset`; атомарная замена через `RENAME impact_next impact` остаётся запасным вариантом (проверено).

**Коммит в ключах.** `key` не содержит sha — иначе каждая переиндексация создаёт новый мир узлов. Sha живёт в `Repo.indexed_commit` и в `source_ref` каждого ребра.

---

## 8. FalkorDB: что проверено на запущенном экземпляре

FalkorDB 6.0.4 на Redis 8.10.2, `THREAD_COUNT 4`, AOF включён, данные в docker-томе. Всё ниже воспроизведено на этом экземпляре.

| Факт | Следствие для индексации |
|---|---|
| Запрос атомарен: ошибка в середине `UNWIND … CREATE` откатывает всё | пакет загрузки = один запрос; упавший пакет повторяется целиком |
| `MERGE (:Topic {name:'x'})` идемпотентен по узлу | ключи узлов — в шаблоне MERGE |
| `MERGE (a)-[:T {source_ref:'…@sha1'}]->(b)` сопоставляет **все** свойства шаблона: тот же факт с новым sha — второе ребро | MERGE рёбер только по `claim_id`; `source_ref` и остальное — через `SET` |
| `MERGE (a {…})-[:T]->(b {…})` целиком создаёт дубликаты узлов, если ребра нет | сначала `MERGE` узлов, потом `MERGE` ребра между найденными |
| Без индекса `MERGE` по 100 тыс. узлов — 27 мс на операцию (Node By Label Scan); с индексом — 3 мкс | range-индекс на `key` каждой метки **до** загрузки |
| Кэш планов (`CACHE_SIZE 25`) держит план, построенный до создания индекса, для той же строки запроса | индексы создаются первыми; загрузчик не переиспользует строки запросов, выполнявшихся до индекса |
| `GRAPH.CONSTRAINT CREATE impact UNIQUE NODE Symbol PROPERTIES 1 key` работает (нужен индекс; строится асинхронно: `PENDING` → `OPERATIONAL`); нарушение — ошибка запроса | защита от дубликатов ключей; ждать `OPERATIONAL` перед загрузкой |
| Уникальность по свойству ребра тоже работает (`UNIQUE RELATIONSHIP MENTIONS PROPERTIES 1 claim_id`) | дедупликация claims на стороне БД |
| Индекс на свойстве ребра используется: `MATCH ()-[r:PRODUCES {repo:'x'}]->() DELETE r` → Edge By Index Scan | удаление по репе без полного обхода |
| Свойства — скаляры и массивы скаляров; вложенные map запрещены; `SET x = null` удаляет свойство | `path[]`, `middlewares[]`, `aliases[]` — массивы; структуры разворачиваются в узлы |
| Параметры: `CYPHER rows=[{…}] UNWIND $rows AS r MERGE …` | загрузка пачками по 1–5 тыс. строк одним запросом |
| `CREATE FULLTEXT INDEX FOR (s:Symbol) ON (s.doc, s.fqn)`; `CALL db.idx.fulltext.queryNodes('Symbol','order upload') YIELD node, score`; префикс `ord*` работает; процедура `createNodeIndex` в этой версии принимает только один аргумент | полнотекст через `CREATE FULLTEXT INDEX`; идентификаторы дублируются в `tokens` словами |
| Полнотекстовый `CALL … YIELD node` совмещается с `MATCH` в одном запросе | поиск и обход в одном Cypher |
| `CREATE VECTOR INDEX FOR (s:Symbol) ON (s.emb) OPTIONS {dimension:4, similarityFunction:'cosine'}`; `SET s.emb = vecf32([…])`; `CALL db.idx.vector.queryNodes('Symbol','emb',k,vecf32([…])) YIELD node, score` | семантический поиск внутри FalkorDB, без внешнего векторного хранилища |
| Несколько меток на узле: `CREATE (:Meta:Label {…})` | можно, но схема обходится одной меткой |
| Пути переменной длины `[:CALLS*1..5]` работают; `shortestPath` — только в `WITH`/`RETURN` | `REACHES` считается через `shortestPath` в `RETURN` |
| `IN` по массиву на ребре: `WHERE 'c.D' IN r.path` | фильтр по цепочке `REACHES.path` |
| `GRAPH.COPY`, `RENAME`, `GRAPH.DELETE`; `MULTI`/`EXEC` для нескольких `GRAPH.QUERY` | атомарная замена графа; транзакция из нескольких запросов |
| Запросов между графами нет; чтение несуществующего графа создаёт пустой | один граф `impact`; аккуратно с опечатками в имени |
| `redis-cli` на хосте нет | `docker exec falkordb redis-cli GRAPH.QUERY impact "…"`; из кода — клиент FalkorDB по Redis-протоколу на `localhost:6379`; браузер на `http://localhost:3001` |

Индексы, которые создаются при инициализации графа:

```cypher
// range: по одному на метку
CREATE INDEX FOR (n:Symbol) ON (n.key)        // … и так для каждой из 24 меток
CREATE INDEX FOR (n:Symbol) ON (n.repo)
CREATE INDEX FOR (n:Symbol) ON (n.file)
// рёбра: для переиндексации и дедупликации
CREATE INDEX FOR ()-[r:CALLS]-() ON (r.repo)   // … для каждого типа ребра
CREATE INDEX FOR ()-[r:CALLS]-() ON (r.claim_id)
// полнотекст
CREATE FULLTEXT INDEX FOR (n:Symbol) ON (n.name, n.tokens, n.doc, n.summary)
CREATE FULLTEXT INDEX FOR (n:HttpEndpoint) ON (n.path, n.operation_id, n.summary, n.description)
CREATE FULLTEXT INDEX FOR (n:Field) ON (n.name, n.description)
CREATE FULLTEXT INDEX FOR (n:Schema) ON (n.name, n.description)
CREATE FULLTEXT INDEX FOR (n:Table) ON (n.name)
CREATE FULLTEXT INDEX FOR (n:Column) ON (n.name)
CREATE FULLTEXT INDEX FOR (n:UiRoute) ON (n.path, n.title)
CREATE FULLTEXT INDEX FOR (n:Adr) ON (n.title, n.summary)
CREATE FULLTEXT INDEX FOR (n:Issue) ON (n.title, n.body)
CREATE FULLTEXT INDEX FOR (n:DocChunk) ON (n.heading, n.text)
CREATE FULLTEXT INDEX FOR (n:Concept) ON (n.name, n.aliases)
// векторные (фаза 2; D — размерность выбранной модели)
CREATE VECTOR INDEX FOR (n:DocChunk) ON (n.emb) OPTIONS {dimension: D, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (n:Symbol) ON (n.emb) OPTIONS {dimension: D, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (n:HttpEndpoint) ON (n.emb) OPTIONS {dimension: D, similarityFunction: 'cosine'}
```

Constraints: `UNIQUE NODE <Label> PROPERTIES 1 key` для каждой метки; `UNIQUE RELATIONSHIP <Type> PROPERTIES 1 claim_id` для каждого типа.

---

## 9. Инструменты агента

Агент пишет Cypher сам; tools остаются тонкими, чтобы схема, а не обёртки, несла знание.

| Tool | Что делает |
|---|---|
| `graph.schema()` | `MATCH (m:Meta) RETURN m` — словарь меток, рёбер, форматов ключей, примеры запросов |
| `graph.query(cypher, params)` | `GRAPH.QUERY impact` с параметрами; возвращает строки и статистику; только чтение (`GRAPH.RO_QUERY`) |
| `graph.search(text, labels?, k?)` | гибрид путей 1–3 из раздела 6: точные ключи, полнотекст с префиксами, вектор (если есть `emb`); возвращает `key`, метку, оценку, сниппет |
| `code.read(source_ref | key, context?)` | открывает `<repo>@<sha>:<path>:L<n>` или `Symbol.file:line_start–line_end` с окрестностью |
| `graph.neighbors(key, depth?, types?)` | удобство: `MATCH (n {key:$k})-[r*1..d]-(m)` с фильтром типов; то же, что агент написал бы сам |

Реализация — `scripts/graph.py`: `schema`, `query "<cypher>" -p k=v`, `search "<text>"`, `read "<source_ref | key>"`, `repos`; `neighbors` агент пишет сам как Cypher. Процесс агента задаёт скилл `/impact <issue | adr.md | текст>`: вход → словарь (`schema`) → точки входа (`search`) → scope → характер изменения → распространение по швам → тесты → отчёт с уровнями достоверности `code` / `docs` / `unknown`.

---

## 10. Фазы и границы

**Фаза 1 — детерминированная.** Экстракторы 1–13 и 15. Без LLM и эмбеддингов. Уже закрывает: точный поиск по идентификаторам, полнотекст по именам и комментариям, весь импакт по коду и швам, тесты, ADR/issues по регулярным выражениям.

**Фаза 2 — семантика.** Эмбеддинги `DocChunk`/`Symbol`/`HttpEndpoint`, `summary` символов, `Concept` и `ABOUT`, агент-резолвер `ALIAS_OF` для `Unresolved`. Нужно решить: модель эмбеддингов (мультиязычная; размерность фиксируется в индексе) и где считать (`summary` для 6 тыс. символов — разовая стоимость).

### Состояние реализации

| Шаг пайплайна | Статус | Где |
|---|---|---|
| 1 `repo` | готово | `scripts/graphify/extract_repo.py` — `Repo`, `Component`, не-Go `File` |
| 2 `code-go` | готово | `crawler-go/internal/gocode` — `Package`, `File`, `Symbol`, `CONTAINS`, `IMPORTS`, `CALLS`, `IMPLEMENTS`, `REFERENCES`, `TESTS` |
| 2 `code-ts` | нет | TS/Svelte-краулер на Node; без него репозитории без `go.mod` получают только L0 и OpenAPI |
| 3 `openapi` | готово | `scripts/graphify/extract_openapi.py` — `HttpEndpoint`, `Schema`, `Field`, `ACCEPTS`, `RETURNS`, `HAS_FIELD`, `REFERENCES`; `SseEvent` — нет |
| 4 `routes` (Go) | готово | `crawler-go/internal/routes` — chi и `http.ServeMux`; `UiRoute` для SvelteKit — нет |
| 5 `dto` (Go) | готово | `scripts/graphify/extract_dto.py` — `SERIALIZES` с `missing_in_code`/`missing_in_spec`; `MIRRORS`, `USES_FIELD` — нет |
| 6 `http-client` | нет | часть TS-краулера |
| 7 `sql` | нет | |
| 8 `markers` | нет | |
| 9 `external` | нет | |
| 10 `tests` (e2e) | нет | часть TS-краулера |
| 11 `docs` | нет | |
| 12 `issues` | нет | |
| 13 `derive` | частично | `scripts/graphify/falkor.py` — `CALLS {via:'derived'}`, флаги `in_spec`/`in_code`; `REACHES` — нет |
| 14 `embed` | нет | фаза 2 |
| 15 `meta` | готово | `scripts/graphify/meta.py`; `implemented` у каждой метки и типа ребра |

Инструменты агента (раздел 9) и скиллы `/graphify`, `/impact` — готовы. Что не извлекается, скилл `/impact` велит проверять чтением кода и помечать как непокрытое графом.

**Что не извлекается и помечается как непокрытое:**

- Динамические вызовы Go (через значения функций, рефлексию) статически не разрешаются — `CALLS` неполный; передача функции как значения помечена `via: value`, агент видит это и может усомниться.
- SQL, собранный конкатенацией из переменных, — `READS`/`WRITES` до уровня таблицы, без колонок; текст запроса в `sql` усечён.
- Svelte-шаблоны: доступ к полям внутри разметки (`{order.merchant_name}`) извлекается через `svelte/compiler` только для типизированных выражений; остальное — `USES_FIELD` с `confidence < 1`.
- Внешние потребители API, которых нет среди проиндексированных реп (например, мобильный клиент) — граница индексации: отсутствие потребителя в графе означает «неизвестно», агент идёт в документацию.
- Конфигурация окружений (`.env.prod`) не индексируется как значения — только имена ключей.

---

## 11. Отличия от `graph-schema.md`

| Было | Стало | Почему |
|---|---|---|
| Три графа: `global`, `code:<repo>@<commit>`, `markers` | один граф `impact` | нет межграфовых запросов; агенту нужен один путь от тикета до чужого сервиса |
| Коммит в имени графа кода | коммит в `Repo.indexed_commit` и в `source_ref` | ключи стабильны между переиндексациями |
| Ключи разные у разных меток (`name`, `fqn`, `pattern`, `id`) | `key` у всех + специфичные свойства | агент пишет один шаблон запроса |
| Точки интеграции только Kafka/gRPC/SQL/Redis | + `HttpEndpoint`, `Schema`, `Field`, `SseEvent`, `UiRoute`, `Channel`, `Event`, `ExternalSystem`, `ConfigKey` | типичная связка — HTTP/SSE между API и веб-клиентом; контракт на уровне полей нужен для вопроса «что сломается у клиента» |
| Код-граф без файлов и пакетов | `Package`, `File`, `Symbol` с `layer` | тикеты и ADR ссылаются на файлы; слои дают фильтрацию без знания проекта |
| Doc-claims из PlantUML (`c4-parser`, `seq-parser`) | `Adr`, `Issue`, `PullRequest`, `DocChunk` с `MENTIONS`/`CITES` | у многих команд знания лежат в Markdown-ADR и issues, а не в C4 |
| `FLOWS_TO` без пути | `REACHES {path[]}` | агент обосновывает цепочку строками кода |
| `Unresolved`, `ALIAS_OF`, провенанс на рёбрах, маркеры в YAML | сохранено | работает; маркеры обобщены на Redis-клиенты, внутреннюю шину событий, SSE |
| Сверка doc-claims с code-claims по гранулярности | заменена на `MIRRORS.drift`, `HttpEndpoint.in_spec/in_code`, `Unresolved` из документов | расхождения контракта и кода ловятся там, где они возникают |
