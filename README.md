# impact-analysis

Граф «тикет → код → межсервисный импакт». Экстракторы загружают репозитории в один граф FalkorDB `impact`; LLM-агент через тонкие tools пишет Cypher сам и отвечает на три вопроса: где в коде изменение, что внутри репы от него зависит, какие другие сервисы задеты.

## Документы

- [graph-schema-v2.md](graph-schema-v2.md) — текущая схема: метки и рёбра, пайплайн индексации, проверенное поведение FalkorDB, инструменты агента, фазы.
- [graph-schema.md](graph-schema.md) — предыдущая схема (межсервисный граф Kafka/gRPC). Отличия — в разделе 11 v2.

## Быстрый старт

```bash
docker compose up -d                                         # FalkorDB
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

Требования: Docker, Go (краулер собирается сам, `GOTOOLCHAIN=auto` подтянет нужную версию), Python-окружение `.venv` с `falkordb` и `pyyaml`, `gh` — для `/impact` по номеру issue.

## Состав

| Часть | Язык | Что делает |
|---|---|---|
| `crawler-go/` | Go (`go/packages`, `go/types`) | пакеты, файлы, символы, `CALLS` / `IMPLEMENTS` / `REFERENCES` / `TESTS`, роуты chi и `http.ServeMux` → `HttpEndpoint` / `HANDLES`; печатает claims-JSON |
| `scripts/indexer.py`, `scripts/graphify/` | Python | `Repo` / `Component` / файлы, запуск краулера, OpenAPI → `HttpEndpoint` / `Schema` / `Field`, `SERIALIZES` (DTO ↔ схема, с дрифтом полей), загрузка в FalkorDB, производные `CALLS` через интерфейсы, `Meta` |
| `scripts/graph.py` | Python | инструменты агента: `schema`, `query`, `search`, `read`, `repos` |
| `.claude/skills/graphify`, `.claude/skills/impact` | Markdown | скиллы `/graphify <repo> [graph]` и `/impact <input>` |

Один запуск индексера для Go-репозитория среднего размера (~400 файлов) — несколько секунд. Повторный запуск для репы, которая уже в графе, заменяет только её claims; `--reset` пересобирает граф целиком.

## Что в графе сейчас

Реализовано: L0 (`Repo`, `Component`, `File`), L1 для Go (`Package`, `Symbol`, `CALLS`, `IMPLEMENTS`, `REFERENCES`, `TESTS`, `IMPORTS`), контракты из OpenAPI (`HttpEndpoint`, `Schema`, `Field`, `ACCEPTS`, `RETURNS`, `HAS_FIELD`), привязка кода к контрактам (`HANDLES`, `SERIALIZES`), флаги расхождений `in_spec` / `in_code`, `Unresolved`, `Meta`.

Ещё нет (помечено в `graph.py schema`): таблицы и колонки из SQL, ключи кэша, каналы и события, SSE-события, внешние системы и конфиг, страницы веб-приложения, TS/Svelte-краулер (`CALLS_ENDPOINT`, `MIRRORS`, `USES_FIELD`, `MOCKS`), ADR и issues (`MENTIONS`, `CITES`), `REACHES`, эмбеддинги. Репозитории без `go.mod` пока получают только L0 и OpenAPI.

## FalkorDB

- Redis-протокол: `localhost:6379`
- Браузер: http://localhost:3001 (host `localhost`, port `6379`, граф `impact`)

`redis-cli` на хосте нет, запросы идут через контейнер или через `scripts/graph.py`:

```bash
docker exec falkordb redis-cli GRAPH.QUERY impact "MATCH (m:Meta) RETURN m.name"
docker exec falkordb redis-cli GRAPH.DELETE impact   # чистая пересборка (то же, что indexer.py --reset)
```

Чтение несуществующего графа создаёт пустой — следите за опечатками в имени.
