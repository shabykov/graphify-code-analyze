# graphify code and analyze

Идея в двух шагах: **проиндексировать исходный код в граф**, а потом **анализировать его с помощью AI** по разным направлениям.

**1. Индексация.** Репозитории детерминированно — компилятором и парсерами, без LLM — раскладываются в один граф FalkorDB `impact`: пакеты, файлы, функции и типы, вызовы и реализации интерфейсов, тесты, HTTP-эндпоинты, схемы и поля OpenAPI, связь кода с контрактами. Узлы-контракты общие для всех репозиториев, поэтому связи между сервисами видны в одном графе. Каждое ребро — свидетельство со ссылкой на строку кода.

**2. Анализ через AI.** LLM-агент получает задачу, сам пишет Cypher-запросы к графу через тонкие инструменты, открывает код по ссылкам из рёбер и подтверждает каждый вывод ссылкой на строку. Граф один, а направления анализа разные:

- **Импакт изменения** (`/impact`, реализовано) — по тикету, ADR или тексту: где в коде изменение, что внутри репозитория от него зависит, какие другие сервисы задеты и какие тесты это покрывают.
- **Расхождение контракта и кода** — эндпоинты, которые есть только в OpenAPI или только в роутере, поля, по которым DTO расходится со схемой. Данные для этого уже в графе.
- **Архитектура и зависимости** — слои, порты и их реализации, связи между сервисами.
- В следующих фазах — данные и события (таблицы, кэш, очереди), документы и issues в самом графе, семантический поиск по эмбеддингам.

Две команды: `/graphify <repo>` индексирует репозиторий, `/impact <input>` делает анализ импакта. Примеры в документации — на условной паре сервисов `orders-api` (Go, chi, OpenAPI) и `orders-web` (SvelteKit).

## Документы

| Файл | Что внутри |
|---|---|
| [graph-schema-v2.md](graph-schema-v2.md) | схема графа: уровни, метки, рёбра, форматы ключей, пайплайн индексации, проверенное поведение FalkorDB, инструменты агента, фазы и состояние реализации |
| [graph-schema.md](graph-schema.md) | предыдущая схема (межсервисный граф Kafka/gRPC); оставлена для справки, отличия — в разделе 11 v2 |
| [CLAUDE.md](CLAUDE.md) | правила для Claude Code при работе с этим репозиторием |
| [crawler-go/README.md](crawler-go/README.md) | Go-краулер: флаги, что извлекается, формат вывода |
| `.claude/skills/graphify/SKILL.md`, `.claude/skills/impact/SKILL.md` | скиллы — пошаговые инструкции агенту |

## Как это работает

```
 репозитории                 экстракторы                        граф                      агент
┌──────────────┐   ┌────────────────────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
│ orders-api   │──▶│ crawler-go (Go)                │   │                  │   │ /impact <input>     │
│   go.mod     │   │  Package File Symbol           │──▶│  FalkorDB        │◀──│   graph.py schema   │
│   api/*.yaml │   │  CALLS IMPLEMENTS REFERENCES   │   │  граф `impact`   │   │   graph.py search   │
│   router.go  │   │  TESTS HANDLES HttpEndpoint    │   │                  │   │   graph.py query    │
│              │   ├────────────────────────────────┤   │  индексы,        │   │   graph.py read     │
│ orders-web   │──▶│ scripts/graphify (Python)      │──▶│  constraints,    │   │   gh issue view     │
│   src/**     │   │  Repo Component File           │   │  Meta-словарь    │   └─────────────────────┘
│              │   │  OpenAPI → Schema Field        │   │                  │
└──────────────┘   │  SERIALIZES (DTO ↔ схема)      │   └──────────────────┘
                   └────────────────────────────────┘
                        claims JSON (model.py)
```

Все экстракторы выдают один формат — claims JSON: узлы с ключом `key` и рёбра-свидетельства с `claim_id` и `source_ref` (`<repo>@<sha>:<path>:L<n>`). Загрузчик пишет их в FalkorDB по правилам, проверенным на живом экземпляре (раздел 8 схемы): индексы и constraints — до данных, узлы — `MERGE` по `key`, рёбра — `MERGE` по `claim_id`. После загрузки вычисляются производные рёбра (вызов интерфейса → его реализации) и записывается `Meta` — словарь графа, который агент читает вместо того, чтобы носить схему в промпте.

## Установка

```bash
python3 -m venv .venv && .venv/bin/pip install falkordb pyyaml
docker compose up -d                                         # FalkorDB: Redis-протокол на :6379, браузер на http://localhost:3001
```

Ещё нужны Go (`crawler-go` требует go 1.26; при `GOTOOLCHAIN=auto` нужная версия скачается сама) и `gh` с авторизацией — для `/impact` по номеру issue. Краулер собирается автоматически в `.graphify/crawler-go`, когда его исходники новее бинарника.

## Быстрый старт

```bash
/graphify ../orders-api                                      # скилл: проиндексировать репозиторий в граф impact
/graphify ../orders-web
/impact orders-api#40                                        # скилл: импакт по issue, пути к ADR или тексту
```

Те же действия без скиллов:

```bash
.venv/bin/python scripts/indexer.py ../orders-api --graph impact -v
.venv/bin/python scripts/graph.py repos
.venv/bin/python scripts/graph.py schema
.venv/bin/python scripts/graph.py search "merchant name"
.venv/bin/python scripts/graph.py query "MATCH (h)-[:HANDLES]->(e:HttpEndpoint {key:'GET /api/v1/orders/{}'}) RETURN h.key, h.file, h.line_start"
.venv/bin/python scripts/graph.py read "orders-api:(*internal/delivery/http/handler/order.Handler).GetByID"
```

### `/graphify <repo> [graph] [--reset]`

Индексирует репозиторий. `<repo>` — путь (имя без пути ищется как соседняя директория проекта), `graph` по умолчанию `impact`, `--reset` удаляет граф перед загрузкой. Скилл проверяет, что FalkorDB запущен, вызывает `scripts/indexer.py`, читает его отчёт и прогоняет контрольные запросы: сходятся ли эндпоинты роутера и OpenAPI, есть ли `Unresolved`, что граф пока не покрывает.

Порядок важен: сначала владелец контрактов (API), потом клиенты — клиентские экстракторы `MERGE`-ятся в узлы `HttpEndpoint`/`Schema`, созданные владельцем. Повторный запуск для репозитория, который уже в графе, заменяет только его claims; остальные репозитории не трогаются.

### `/impact <#issue | repo#issue | path/to/adr.md | текст> [graph=<name>]`

Анализ импакта. Вход превращается в текст задачи (`gh issue view`, чтение ADR или сам текст), затем агент идёт по процессу: словарь графа → точки входа (поиск по идентификаторам и словам) → scope внутри сервиса (входящие и исходящие вызовы, типы) → задетые контракты и характер изменения (контракт / семантика / реализация) → распространение на другие сервисы → тесты. Каждое утверждение отчёта несёт ссылку на код (`file:line`), результат запроса или строку документа; без ссылки — помечается как предположение. Отсутствие потребителя в графе трактуется как «неизвестно», а не «никого нет».

## CLI

### `scripts/indexer.py <repo> [флаги]`

| Флаг | По умолчанию | Назначение |
|---|---|---|
| `<repo>` | — | путь к репозиторию |
| `--name` | имя директории | имя репозитория в ключах узлов |
| `--graph` | `impact` | имя графа FalkorDB |
| `--host`, `--port` | `localhost`, `6379` | адрес FalkorDB |
| `--reset` | — | удалить весь граф перед загрузкой |
| `--replace` | автоматически, если репа уже в графе | удалить прежние claims этого репозитория перед загрузкой |
| `--no-load` | — | только извлечь, FalkorDB не трогать |
| `--out <file>` | — | записать claims JSON (удобно вместе с `--no-load`) |
| `--only repo,go,openapi,dto` | все применимые | какие экстракторы запускать |
| `--no-tests` | — | не индексировать `_test.go` |
| `--openapi-glob <glob>` | `api/*.yaml` и типичные места | где искать OpenAPI (можно повторять) |
| `--batch` | `1000` | строк в одном запросе загрузки |
| `-v` | — | подробный лог на stderr |

### `scripts/graph.py <команда>`

Все команды принимают `--graph`, `--host`, `--port` перед подкомандой; то же через переменные `GRAPHIFY_GRAPH`, `GRAPHIFY_HOST`, `GRAPHIFY_PORT`.

| Команда | Что делает |
|---|---|
| `repos` | проиндексированные репозитории: коммит, время, GitHub-slug, корень, компоненты |
| `schema` | словарь графа из `Meta`: метки, рёбра, форматы ключей, что уже извлекается, примеры Cypher |
| `query "<cypher>" [-p k=v] [--json] [--wide] [--write]` | запрос только на чтение (`--write` разрешает запись); значения параметров разбираются как JSON |
| `search "<текст>" [--labels A,B] [-k N]` | точное совпадение по `key`/`name`/`path` (score 100) плюс полнотекст по словам: сначала все слова, потом любое слово по префиксу |
| `read "<source_ref или ключ Symbol/File>" [--context N]` | печатает код вокруг ссылки, корень репозитория берётся из узла `Repo` |

Примеры запросов агента:

```bash
.venv/bin/python scripts/graph.py query "MATCH (h)-[r:HANDLES]->(e:HttpEndpoint {key:'GET /api/v1/orders/{}'}) RETURN h.key, h.file, h.line_start, r.middlewares"
.venv/bin/python scripts/graph.py query "MATCH (f:Field {name:'merchant_name'})<-[:HAS_FIELD]-(s:Schema)<-[r:RETURNS]-(e:HttpEndpoint) RETURN s.name, e.key, r.status"
.venv/bin/python scripts/graph.py query "MATCH (d:Symbol)-[s:SERIALIZES]->(sc:Schema {name:'Order'}) RETURN d.key, s.match, s.missing_in_code, s.missing_in_spec" --wide
.venv/bin/python scripts/graph.py query "MATCH (c:Symbol)-[r:CALLS]->(s:Symbol {key:\$k}) RETURN c.key, r.via, r.line" -p 'k="orders-api:(*internal/domain/order.GeneralData).MerchantName"'
.venv/bin/python scripts/graph.py query "MATCH (e:HttpEndpoint) WHERE NOT (e.in_spec AND e.in_code) RETURN e.key, e.in_spec, e.in_code"
```

## Состав

| Часть | Язык | Что делает |
|---|---|---|
| `crawler-go/` | Go (`go/packages`, `go/types`) | пакеты, файлы, символы, `CALLS` / `IMPLEMENTS` / `REFERENCES` / `TESTS`, роуты chi и `http.ServeMux` → `HttpEndpoint` / `HANDLES`; печатает claims JSON |
| `scripts/indexer.py`, `scripts/graphify/` | Python | `Repo` / `Component` / файлы, запуск краулера, OpenAPI → `HttpEndpoint` / `Schema` / `Field`, `SERIALIZES` (DTO ↔ схема, с дрифтом полей), загрузка в FalkorDB, производные `CALLS` через интерфейсы, `Meta` |
| `scripts/graph.py` | Python | инструменты агента: `schema`, `query`, `search`, `read`, `repos` |
| `.claude/skills/graphify`, `.claude/skills/impact` | Markdown | скиллы `/graphify <repo> [graph]` и `/impact <input>` |

```
crawler-go/
  main.go                   CLI (-repo, -name, -commit, -out, -tests, -routes, -v)
  internal/model            формат claims, ключи эндпоинтов, разбиение имён на слова
  internal/goprog           загрузка пакетов, имена символов в нотации SSA
  internal/gocode           Package/File/Symbol, CALLS, IMPLEMENTS, REFERENCES, TESTS
  internal/routes           chi и http.ServeMux → HttpEndpoint, HANDLES
scripts/
  indexer.py                оркестратор: репа → краулер → OpenAPI → DTO → FalkorDB
  graph.py                  инструменты агента
  graphify/
    model.py                Claims, Source, normalize_path, tokens — общий контракт с краулером
    extract_repo.py         Repo, Component, не-Go файлы
    extract_openapi.py      HttpEndpoint, Schema, Field, ACCEPTS, RETURNS, HAS_FIELD
    extract_dto.py          SERIALIZES: DTO-структура ↔ схема, дрифт полей
    falkor.py               индексы, constraints, загрузка пачками, производные рёбра, удаление репы
    meta.py                 словарь графа: метки, рёбра, полнотекст, примеры запросов
.claude/skills/             /graphify, /impact
.graphify/                  собранный краулер (в .gitignore)
```

Один запуск индексера для Go-репозитория среднего размера (~400 файлов, ~6 тыс. символов) — несколько секунд.

## Что в графе сейчас

| Уровень | Метки | Рёбра | Откуда |
|---|---|---|---|
| L0 репозитории | `Repo`, `Component`, `File` (не-Go файлы) | `CONTAINS` | git, `cmd/*`, `package.json`, обход дерева |
| L1 код (Go) | `Package`, `File`, `Symbol` (`func`, `method`, `test`, `type`, `interface`, `interface_method`, `const`, `var`) | `CONTAINS`, `IMPORTS`, `CALLS {via}`, `IMPLEMENTS`, `REFERENCES {how}`, `TESTS {how}` | `crawler-go` |
| L2 контракты | `HttpEndpoint`, `Schema`, `Field` | `HANDLES`, `ACCEPTS`, `RETURNS`, `HAS_FIELD`, `SERIALIZES`, `REFERENCES` между схемами | роутер chi / `http.ServeMux`, OpenAPI, json-теги DTO |
| служебные | `Unresolved`, `Meta` | | любой экстрактор; загрузчик |

Что это даёт:

- **Сверка контракта и кода.** `HttpEndpoint.in_spec` / `in_code` показывают эндпоинты, которые есть только в OpenAPI или только в роутере; `SERIALIZES.missing_in_code` / `missing_in_spec` — поля, по которым расходятся DTO-структура и схема.
- **Проход через интерфейсы.** `CALLS {via:'interface'}` ведёт в интерфейсный метод, `IMPLEMENTS` — к реализациям; производное `CALLS {via:'derived', confidence: 0.8}` соединяет вызов с реализацией напрямую (моки и типы из тестов исключены), так что `-[:CALLS*1..5]->` проходит сквозь порты clean architecture.
- **Поиск по словам.** У каждого символа есть `tokens` — имя, разбитое на слова (`GetByID` → `get by id`), поэтому полнотекст находит код по английским словам из русского тикета.
- **Каждое ребро — свидетельство** со ссылкой `source_ref` на строку; агент открывает её через `graph.py read`.

Ещё нет (в `graph.py schema` помечено «ещё не извлекается»): таблицы и колонки из SQL, ключи кэша, каналы и события, SSE-события, внешние системы и конфиг, страницы веб-приложения, TS/Svelte-краулер (`CALLS_ENDPOINT`, `MIRRORS`, `USES_FIELD`, `MOCKS`), ADR и issues внутри графа (`MENTIONS`, `CITES`), производное `REACHES`, эмбеддинги и `Concept`. Репозитории без `go.mod` пока получают только L0 и OpenAPI; межсервисный шаг для них агент закрывает чтением кода и помечает как `unknown`.

## Расширение

**Новый Python-экстрактор.** Модуль в `scripts/graphify/` с функцией `extract(root, src) -> Claims` (образец — `extract_openapi.py`): узлы — `claims.node(label, key, **props)`, рёбра — `claims.edge(TYPE, (label, key), (label, key), src.props(path, line, **extra))`. Подключить в `scripts/indexer.py` и добавить имя в `--only`. Метки и типы рёбер объявить в `scripts/graphify/meta.py` (`implemented: True`), свойства для полнотекста — в `meta.FULLTEXT`.

**Краулер другого языка.** Пишется на этом языке (TypeScript — на Node), печатает тот же claims JSON:

```json
{"nodes": [{"label": "Symbol", "key": "orders-web:src/lib/api.ts#api.getOrder", "props": {"kind": "api_method", "file": "src/lib/api.ts", "line_start": 40, "line_end": 41}}],
 "edges": [{"type": "CALLS_ENDPOINT", "from": {"label": "Symbol", "key": "orders-web:src/lib/api.ts#api.getOrder"}, "to": {"label": "HttpEndpoint", "key": "GET /api/v1/orders/{}"},
            "props": {"repo": "orders-web", "commit": "3c3b9d9", "source_ref": "orders-web@3c3b9d9:src/lib/api.ts:L40", "extracted_by": "crawler-ts", "confidence": 1.0, "line": 40}}]}
```

Индексер подмешивает его через `Claims.merge_json`. Ключи швов должны совпадать с ключами владельца: `HttpEndpoint` — `METHOD /path`, где каждый параметр заменён на `{}` (`model.endpoint_key`), `Schema` — `<owner_repo>:<Name>`, `Field` — `<owner_repo>:<Schema>.<field>`.

**Проверка без загрузки.** `indexer.py <repo> --no-load --out claims.json` показывает, что извлеклось; при загрузке индексер сообщает о рёбрах, у которых не нашёлся узел-конец («dangling») — это всегда расхождение ключей между экстракторами.

## Ограничения

- Динамические вызовы Go (через значения функций, рефлексию) статически не разрешаются; передача функции как значения помечена `CALLS {via:'value'}`.
- `IMPLEMENTS` строится по совместимости типов, поэтому включает моки (`Symbol.is_mock`) — фильтруйте их в запросах; производные `CALLS` их уже исключают.
- Роутеры: chi и `http.ServeMux`. Другие (gin, echo, gorilla) дадут эндпоинты только из OpenAPI с `in_code: false`.
- Краулер собирает пакеты с флагами по умолчанию: build tags и альтернативный `go.mod` пока не передаются. Репозиторий должен собираться (`go build ./...`).
- OpenAPI 3: `$ref` только внутри файла (`#/components/...`); inline-схемы запросов и ответов становятся `Schema {kind:'inline'}` с именем `<operationId>.request` / `.response<status>`.
- Один граф — один набор репозиториев на одном коммите каждый; история не индексируется, sha живёт в `Repo.indexed_commit` и в `source_ref`.

## FalkorDB: доступ и диагностика

- Redis-протокол: `localhost:6379`; браузер: http://localhost:3001 (host `localhost`, port `6379`, граф `impact`).
- `redis-cli` на хосте нет — через контейнер или `scripts/graph.py query`:

```bash
docker exec falkordb redis-cli GRAPH.QUERY impact "MATCH (m:Meta) RETURN m.name"
docker exec falkordb redis-cli GRAPH.DELETE impact   # чистая пересборка (то же, что indexer.py --reset)
```

- Чтение несуществующего графа создаёт пустой — опечатка в имени не даст ошибки; `graph.py repos` покажет, что репозиториев нет.
- `unique constraint violation` при загрузке — экстрактор выдал два узла с одним `key`; `--no-load --out` и поиск дубликатов в JSON.
- Constraint в состоянии `FAILED` (`CALL db.constraints()`) — в графе уже есть дубликаты ключей, обычно после ручных записей; лечится `--reset`.
- Краулер не собрался — проверьте `go version` и доступ к proxy.golang.org при первой сборке (`GOTOOLCHAIN=auto` скачивает тулчейн).
- Браузер FalkorDB показывает `ERR invalid graph version` — граф удаляли и создавали заново (`GRAPH.DELETE`), а бэкенд UI держит кэш версии; `docker compose restart falkordb` чинит, данные сохраняются (AOF). `indexer.py --reset` поэтому очищает граф на месте, а не удаляет ключ.
- Чтобы увидеть картинку в UI, в `RETURN` должны быть сами узлы и рёбра (`RETURN h, r, e` или `RETURN p`), а не их свойства; для больших выборок — `LIMIT`.
