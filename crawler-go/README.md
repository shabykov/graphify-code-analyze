# crawler-go

Анализирует один Go-репозиторий и печатает claims JSON для `scripts/indexer.py`: уровень L1 графа (пакеты, файлы, символы и связи между ними) и HTTP-роуты уровня L2. Работает на `go/packages` + `go/types`: зависимости берутся из export data, парсятся только файлы самого репозитория. Репозиторий должен собираться (`go build ./...`).

Обычно вызывается индексером, который собирает бинарник в `../.graphify/crawler-go` при изменении исходников. Вручную:

```bash
GOTOOLCHAIN=auto go build -o ../.graphify/crawler-go .
../.graphify/crawler-go -repo ../../orders-api -v -out claims.json
```

| Флаг | По умолчанию | Назначение |
|---|---|---|
| `-repo` | — | путь к репозиторию (обязателен) |
| `-name` | имя директории | имя репозитория в ключах |
| `-commit` | `git rev-parse --short HEAD` | sha для `source_ref` |
| `-out` | `-` (stdout) | файл для JSON |
| `-tests` | `true` | включать `_test.go` |
| `-routes` | `true` | извлекать регистрации HTTP-роутов |
| `-v` | — | статистика и время на stderr |

## Что извлекается

| Узлы | Ключ |
|---|---|
| `Package` | `<repo>:<путь от корня>` — `orders-api:internal/usecase/order` |
| `File` | `<repo>:<path>` — `orders-api:internal/delivery/http/router.go` |
| `Symbol` | `<repo>:<fqn>` в нотации SSA с путём пакета от корня: `orders-api:internal/domain/order.New`, `orders-api:(*internal/delivery/http/handler/order.Handler).GetByID`, `orders-api:(internal/usecase/adapters/storage.OrderUpdater).Upsert` |
| `HttpEndpoint` | `METHOD /path`, параметры заменены на `{}` — `GET /api/v1/orders/{}` |
| `Unresolved` | обработчик роута, который не удалось привести к объявленной функции |

`Symbol.kind`: `func`, `method`, `test`, `type`, `interface`, `interface_method`, `const`, `var`. У структур — `fields[]` и `json_fields[]` (для `SERIALIZES`), у констант — `value`, у всех — `file`, `line_start`, `line_end`, `doc`, `signature`, `tokens`, `layer`, `test_file`, `is_mock`.

| Рёбра | Откуда |
|---|---|
| `CONTAINS` | Repo → Package → File → Symbol; тип → метод; интерфейс → интерфейсный метод |
| `IMPORTS` | импорты внутри модуля |
| `CALLS {via}` | `TypesInfo.Uses`: `static` — прямой вызов, `interface` — вызов интерфейсного метода, `value` — функция передана как значение; вызовы из замыканий относятся к объемлющей функции |
| `IMPLEMENTS` | `types.Implements` для каждой пары тип × интерфейс репозитория, плюс метод → интерфейсный метод |
| `REFERENCES {how}` | использование типа: `param`, `return`, `field`, `embeds`, `instantiates`, `uses` |
| `TESTS {how}` | `calls` — тест вызывает символ; `names` — имя теста содержит имя символа (`TestOrder_Total` → `Order.Total`) |
| `HANDLES {middlewares}` | chi: `r.Get/Post/…`, `Route`, `Group`, `Use` внутри групп, цепочки `With(...)`, `Method`; `http.ServeMux`: `Handle`/`HandleFunc` с паттернами `"GET /path"` |

Пропускается то, у чего нет узла: параметры типов, типы внутри функций, методы анонимных интерфейсов, cgo-файлы из кэша сборки. Тестовые варианты пакетов (`pkg [pkg.test]`) дают только свои `_test.go`.

## Формат вывода

```json
{
  "nodes": [{"label": "Symbol", "key": "orders-api:internal/domain/order.New", "props": {"key": "…", "kind": "func", "file": "internal/domain/order/order.go", "line_start": 12, "line_end": 30, "…": "…"}}],
  "edges": [{"type": "CALLS", "from": {"label": "Symbol", "key": "…"}, "to": {"label": "Symbol", "key": "…"}, "claim_id": "5d41402abc4b2a76b9719",
             "props": {"claim_id": "…", "repo": "orders-api", "commit": "1a2b3c4", "source_ref": "orders-api@1a2b3c4:internal/domain/order/order.go:L20", "extracted_by": "crawler-go/code", "confidence": 1.0, "line": 20, "via": "static"}}]
}
```

`claim_id` — первые 20 hex-символов SHA-1 от `type|from.label|from.key|to.label|to.key|source_ref`; Python-сторона считает его так же (`scripts/graphify/model.py`). Узел `Repo` краулер не создаёт — его добавляет индексер.
