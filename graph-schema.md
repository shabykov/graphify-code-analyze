# Схема графов

Хранилище — FalkorDB. Графы:

- `global` — межсервисный граф: сервисы, компоненты, точки интеграции, утверждения из документации и кода.
- `code:<repo>@<commit>` — код-граф одной репы на одном коммите, по одному на проиндексированную репу.
- `markers` — кэш маркеров точек интеграции по библиотекам, общий для всех реп.

## Канонические идентификаторы

| Сущность | Канон | Источник канона |
|---|---|---|
| Service | имя репы (`orders`) | список реп организации, архитектурная документация |
| Component | `<repo>/<app_name>` (`orders/orders-consumer`) | список приложений в CI/CD-конфиге репы |
| HttpEndpoint | `METHOD /path/{param}` — путь нормализован, параметры в фигурных скобках | OpenAPI/Swagger, регистрация роутов |
| GrpcMethod | `pkg.Service/Method` (`pricing.v1.QuoteService/CreateQuote`) | proto-пакеты |
| Topic | полная строка имени (`orders.created.v1`) | литерал в коде/конфиге/доке |
| Table | `<owner_repo>.<db>.<table>` | миграции, container-диаграммы |
| CacheKeyspace | шаблон ключа (`order:{id}:status`) | код, container-диаграммы |

Компонент — деплоящееся приложение репы. Каждое приложение из CI/CD-конфига соответствует своей точке входа (`cmd/<app_name>/` в Go), образу и деплой-манифесту; имя приложения совпадает с именем деплоймента. Исполняемый пакет вне этого списка — служебная утилита, в компоненты не входит.

## Граф `global`

### Иерархия контейнеров

| Label | Ключ | Свойства |
|---|---|---|
| `System` | `name` | логическая система из архитектурных схем (группа сервисов одного домена); `source_ref` |
| `Service` | `repo` | `lang`; `indexed_commit`, `indexed_at` — коммит и время последней индексации кода, `null` пока код не собран |
| `Component` | `id` = `<repo>/<app_name>` | `repo`, `app_name`, `main_pkg`, `kind` (`api`/`consumer`/`cron`/…) |

Рёбра: `(System)-[:CONTAINS]->(Service)-[:CONTAINS]->(Component)`. `Service→Component` структурное, из CI/CD-конфига. `System→Service` — claim из документации, несёт провенанс.

### Точки интеграции

Общие свойства: `protocol` (`http`/`grpc`/`kafka`/`nats`/`mysql`/`redis`), `owner_repo` — сервис-владелец, если известен.

| Label | Ключ |
|---|---|
| `HttpEndpoint` | `method`, `path` |
| `GrpcMethod` | `fqn` |
| `Topic` | `name` |
| `Table` | `fqn` |
| `CacheKeyspace` | `pattern` |

### Резолвинг имён

| Label | Свойства |
|---|---|
| `Unresolved` | `name`, `name_kind` (`puml_alias`/`seq_participant`/`label`/`call_arg`), `source_ref` |

`Unresolved` — имя из документации или аргумент вызова из кода, которые на момент загрузки не сопоставлены с каноническим узлом. Claims из экстракторов ссылаются на `Unresolved` как на обычный узел.

`(Unresolved)-[:ALIAS_OF]->(System|Service|Component|Topic|Table)` — результат резолвинга. Свойства: `confidence`, `source_ref`, `extracted_by`. Одно `Unresolved` может иметь несколько `ALIAS_OF` (логическая система, покрывающая несколько сервисов). Запросы сверки и импакта проходят через `ALIAS_OF` как через `CONTAINS`.

Детерминированный резолвинг: расположение элемента в документации, привязанное к репе (каталог или файл на сервис); явная ссылка элемента диаграммы на документ сервиса; буквальное совпадение имён топиков и таблиц; proto-пакет → репа-владелец; нормализация имени (`lower`, удаление суффикса `-service`/`_service`, `_` → `-`) и точное совпадение со списком реп. Остаток резолвится агентом; его выход валидируется на существование целевого узла.

### Рёбра-утверждения (claims)

Одно ребро — одно свидетельство. Между одной парой узлов допускается несколько рёбер одного типа с разными источниками.

Свойства, обязательные на каждом claim:

| Свойство | Значения |
|---|---|
| `source_kind` | `doc` \| `code` |
| `source_ref` | `<repo>@<sha>:<path>:L<line>` — `arch-docs@ab12f:orders/system.puml:L41`, `orders@160d89d:internal/events/kafka.go:L88` |
| `extracted_by` | `c4-parser` \| `seq-parser` \| `go-crawler` \| `values-parser` \| `agent:<prompt-id>` |
| `granularity` | `system` \| `service` \| `component` \| `point` — уровень subject'а |
| `confidence` | `1.0` для детерминированных экстракторов, `< 1.0` для агентских, `0.0` для нерезолвленных |

Дополнительные свойства code-claims:

| Свойство | Значение |
|---|---|
| `marker` | id маркера, породившего claim |
| `env` | окружение конфига, из которого разрешено имя (`prod`) |
| `event_name` | имя события в коде, из которого через конфиг получен топик |
| `config_ref` | `<repo>@<sha>:<path>:L<line>` — строка конфига с разрешённым значением |
| `handler` | функция-обработчик для inbound-точки |
| `unresolved_reason` | причина, по которой цель осталась `Unresolved` |

| Тип | from → to | Доп. свойства | Источники |
|---|---|---|---|
| `EXPOSES` | Component \| Service → точка | | код; container-диаграммы |
| `CALLS` | Component \| Service \| System → HttpEndpoint \| GrpcMethod \| Service \| System | `protocol` — обязателен, если цель не точка | код; связи на container-диаграммах; sequence-диаграммы |
| `PRODUCES` | Component \| Service → Topic | | код; sequence-диаграммы |
| `CONSUMES` | Component \| Service → Topic | | код; sequence-диаграммы |
| `USES` | Service → Topic \| Table \| CacheKeyspace | `direction: null` | container-диаграммы: связь сервиса с брокером / БД / кэшем без направления |
| `READS` | Component → Table \| CacheKeyspace | | код |
| `WRITES` | Component → Table \| CacheKeyspace | | код |
| `OWNS` | Service → Topic \| Table \| CacheKeyspace | | container-диаграммы (элемент внутри границы сервиса); миграции |
| `FLOWS_TO` | точка → точка | `component` — компонент, внутри которого идёт поток; `handler` | код: outbound-точка достижима из обработчика inbound-точки |

`FLOWS_TO` связывает точки внутри одного компонента и даёт транзитивность импакта через сервис: изменение топика, который потребляет компонент, задевает всё, что достижимо из его обработчика.

### Подтверждение утверждений разной гранулярности

Doc-claim подтверждён code-claim'ом, если существует уточняющий путь через `CONTAINS*` и `ALIAS_OF*`:

- `(S1:Service)-[:CALLS {protocol}]->(S2:Service)` подтверждается путём `(S1)-[:CONTAINS]->(c1)-[:CALLS]->(p)<-[:EXPOSES]-(c2)<-[:CONTAINS]-(S2)`, где `p.protocol` совпадает с `protocol` ребра.
- `(S)-[:USES]->(T)` подтверждается любым из `PRODUCES`/`CONSUMES`/`READS`/`WRITES` от компонента `S` к `T`.
- `(Sys:System)-[:CALLS]->(X)` подтверждается подтверждением для любого `Service` под `Sys`.

Классы результата сверки для тройки `(subject, predicate, object)`:

| Класс | Условие |
|---|---|
| подтверждено точно | doc и code на одном уровне гранулярности |
| подтверждено грубо | doc на уровне выше, code уточняет |
| только в доках | нет code-claim ни на одном уровне ниже |
| только в коде | нет doc-claim ни на одном уровне выше |
| не сопоставлено | subject или object — `Unresolved` без `ALIAS_OF` |

Классы вычисляются запросом, в графе не хранятся.

### Пример

```cypher
(:Service {repo:"orders", lang:"go", indexed_commit:"160d89d"})
  -[:CONTAINS]->
(:Component {id:"orders/orders-api", app_name:"orders-api", main_pkg:"cmd/orders-api"})
  -[:CALLS {source_kind:"code", source_ref:"orders@160d89d:internal/quote/client.go:L212",
            extracted_by:"go-crawler", granularity:"component", confidence:1.0}]->
(:GrpcMethod {fqn:"pricing.v1.QuoteService/CreateQuote", protocol:"grpc", owner_repo:"pricing"})

(:Service {repo:"orders"})
  -[:CALLS {source_kind:"doc", source_ref:"arch-docs@ab12f:landscape.puml:L31",
            extracted_by:"c4-parser", granularity:"service", protocol:"grpc", confidence:1.0}]->
(:Service {repo:"pricing"})

(:Unresolved {name:"OS", name_kind:"seq_participant",
              source_ref:"orders@160d89d:docs/diagrams/checkout.puml:L4"})
  -[:ALIAS_OF {confidence:0.95, extracted_by:"agent:alias-v1"}]->
(:Service {repo:"orders"})
```

## Граф `code:<repo>@<commit>`

| Label | Ключ | Свойства |
|---|---|---|
| `Package` | `path` | |
| `Symbol` | `fqn` | `kind` (`func`/`method`/`type`), `file`, `line_start`, `line_end`, `pkg` |
| `Root` | `component_id` — равен `Component.id` в `global` | `main_pkg` |
| `Marker` | `fqn` — копия из графа `markers` | `module`, `version`, `kind`, `arg_index` |

| Тип | from → to | Свойства |
|---|---|---|
| `CALLS` | Symbol → Symbol | |
| `ENTRY` | Root → Symbol | |
| `IN_PKG` | Symbol → Package | |
| `INVOKES_MARKER` | Symbol → Marker | `arg_value` — извлечённое имя топика/роута/таблицы; `arg_resolved` — удалось ли извлечь литерал; `source_ref` |

Членство символа в компоненте вычисляется: `MATCH (r:Root)-[:ENTRY]->()-[:CALLS*0..]->(s)`. Точки интеграции компонента — символы с `INVOKES_MARKER`, достижимые из его `Root`. `arg_value` становится ключом узла-точки в `global`; при `arg_resolved = false` в `global` создаётся `Unresolved {name_kind:"call_arg"}`.

Для Go граф строится через `golang.org/x/tools`: `go/packages` загружает `./...`, call graph — RTA от каждого `Root`.

## Граф `markers`

| Label | Ключ | Свойства |
|---|---|---|
| `Module` | `path`, `version` | |
| `Marker` | `fqn` | `kind` (`produce`/`consume`/`http-route`/`grpc-client`/`grpc-server`/`sql`/`cache`), `arg_index`, `extracted_by`, `confidence` |

`(Module)-[:DEFINES]->(Marker)`. Маркер — библиотечный символ, вызов которого образует точку интеграции, с указанием аргумента, несущего имя канала. Маркеры для `module@version` добываются один раз и переиспользуются всеми репами с этой зависимостью. При индексации репы маркеры её зависимостей копируются в `code:<repo>@<commit>`.
