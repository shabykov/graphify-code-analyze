"""The graph's own vocabulary, loaded as Meta nodes so an agent can read the
schema from the graph instead of carrying it in a prompt.

`implemented` marks what the current extractors actually produce.
"""

from __future__ import annotations

from .model import Claims

LABELS: list[dict] = [
    # L0
    {"name": "Repo", "key_format": "<repo>", "implemented": True,
     "description": "Репозиторий. root — абсолютный путь на машине индексации, github — owner/repo для gh, indexed_commit — проиндексированный коммит.",
     "properties": ["name", "lang", "root", "module", "remote", "github", "default_branch", "indexed_commit", "indexed_at"]},
    {"name": "Component", "key_format": "<repo>/<app>", "implemented": True,
     "description": "Деплоящееся приложение репозитория: package main под cmd/ или веб-приложение.",
     "properties": ["name", "kind", "entry", "runtime"]},
    # L1
    {"name": "Package", "key_format": "<repo>:<путь директории>", "implemented": True,
     "description": "Пакет / директория исходников. layer — архитектурный слой из пути (delivery, usecase, domain, repository, pkg, cmd, …).",
     "properties": ["name", "path", "import_path", "layer", "doc"]},
    {"name": "File", "key_format": "<repo>:<path>", "implemented": True,
     "description": "Файл репозитория. kind: source | test | migration | spec | doc | config | style | build | asset.",
     "properties": ["path", "lang", "kind", "lines", "package", "layer"]},
    {"name": "Symbol", "key_format": "<repo>:<fqn> (Go: internal/a.Func, (*internal/a.T).M, (internal/a.I).M; TS: src/lib/api.ts#api.getOrder, src/lib/utils/x.ts#fn; Svelte-компонент/страница/тест-файл: <repo>:<path>)", "implemented": True,
     "description": "Объявление в коде. Go kind: func | method | test | type | interface | interface_method | const | var. TS/Svelte kind: function | method | api_method | class | type | const | var | component | page | layout | test (один символ на файл теста). file/line_start/line_end — где читать; tokens — имя словами для полнотекста; is_mock/test_file — для фильтрации.",
     "properties": ["name", "fqn", "kind", "file", "line_start", "line_end", "exported", "signature", "doc", "tokens", "layer", "package", "test_file", "is_mock", "fields", "json_fields", "value", "type"]},
    # L2
    {"name": "HttpEndpoint", "key_format": "<METHOD> <path с {} вместо параметров>, например GET /api/v1/orders/{}", "implemented": True,
     "description": "HTTP-эндпоинт — шов между сервисами. in_spec/in_code/in_client показывают, виден ли он в OpenAPI, в роутере сервера и в клиентском коде; path — канон из OpenAPI, path_code — как в коде. Узел только с in_client — клиент зовёт то, чего у сервера нет.",
     "properties": ["method", "path", "path_code", "path_pattern", "operation_id", "summary", "description", "tags", "auth", "sse", "rate_limit", "middlewares", "params", "error_codes", "in_spec", "in_code", "in_client", "owner_repo", "spec_file", "spec_ref"]},
    {"name": "Schema", "key_format": "<owner_repo>:<Name>", "implemented": True,
     "description": "Схема OpenAPI (DTO контракта). kind: object | enum | array | inline.",
     "properties": ["name", "kind", "type", "description", "enum_values", "required", "spec_file", "spec_ref", "owner_repo"]},
    {"name": "Field", "key_format": "<owner_repo>:<Schema>.<field>", "implemented": True,
     "description": "Поле схемы. Меняется поле — ищи, кто RETURNS/ACCEPTS его схему и кто USES_FIELD.",
     "properties": ["name", "schema", "type", "format", "required", "nullable", "description", "enum_values", "example", "spec_ref", "owner_repo"]},
    {"name": "SseEvent", "key_format": "<endpoint_key>#<event>", "implemented": False,
     "description": "Событие SSE-потока эндпоинта.", "properties": ["name", "description"]},
    {"name": "UiRoute", "key_format": "<repo>:<route>, например orders-web:/orders/[id]", "implemented": True,
     "description": "Страница веб-приложения из файлового роутинга SvelteKit (src/routes/**/+page.svelte). kind: page | layout | endpoint.", "properties": ["path", "params", "kind"]},
    {"name": "Table", "key_format": "<owner_repo>.<table>", "implemented": False,
     "description": "Таблица БД из миграций.", "properties": ["name", "db", "migration_ref"]},
    {"name": "Column", "key_format": "<owner_repo>.<table>.<column>", "implemented": False,
     "description": "Колонка таблицы.", "properties": ["name", "type", "nullable", "default"]},
    {"name": "CacheKey", "key_format": "<owner_repo>:<pattern>", "implemented": False,
     "description": "Шаблон ключа кэша; переменные части — {}.", "properties": ["pattern", "store", "ttl", "builder"]},
    {"name": "Channel", "key_format": "<owner_repo>:<transport>:<pattern>", "implemented": False,
     "description": "Канал pub/sub или топик.", "properties": ["pattern", "transport"]},
    {"name": "Event", "key_format": "<owner_repo>:<fqn типа события>", "implemented": False,
     "description": "Внутреннее доменное событие, передаваемое через диспетчер.", "properties": ["name", "fqn", "fields"]},
    {"name": "ExternalSystem", "key_format": "<name>", "implemented": False,
     "description": "Внешняя система (БД, кэш, LLM, почта, OAuth).", "properties": ["kind", "sdk"]},
    {"name": "ConfigKey", "key_format": "<owner_repo>:<ENV_NAME>", "implemented": False,
     "description": "Ключ конфигурации / переменная окружения.", "properties": ["name", "default", "doc"]},
    {"name": "Unresolved", "key_format": "<repo>:<kind>:<name>", "implemented": True,
     "description": "Ссылка, которую экстрактор не смог сопоставить с узлом. reason объясняет почему. Отсутствие цели в графе значит «неизвестно», а не «нет».",
     "properties": ["name", "kind", "reason"]},
    # L3
    {"name": "Adr", "key_format": "<repo>:ADR-<n>", "implemented": False,
     "description": "Architecture Decision Record из docs/adr.", "properties": ["number", "title", "status", "date", "file", "summary"]},
    {"name": "Issue", "key_format": "<repo>#<n>", "implemented": False,
     "description": "GitHub issue (живые данные через gh).", "properties": ["number", "title", "state", "labels", "url", "body", "created_at", "closed_at"]},
    {"name": "PullRequest", "key_format": "<repo>!<n>", "implemented": False,
     "description": "GitHub pull request.", "properties": ["number", "title", "merged_at", "branch"]},
    {"name": "DocChunk", "key_format": "<doc_key>#<n>", "implemented": False,
     "description": "Фрагмент документа (ADR, issue, README) по заголовку.", "properties": ["heading", "text", "line_start", "line_end"]},
    {"name": "Concept", "key_format": "concept:<slug>", "implemented": False,
     "description": "Понятие домена с синонимами на разных языках.", "properties": ["name", "aliases", "description"]},
    {"name": "Meta", "key_format": "label:<Label> | rel:<TYPE>", "implemented": True,
     "description": "Этот словарь: описание меток и типов рёбер графа.", "properties": ["kind", "name", "description", "key_format", "properties", "from", "to", "implemented"]},
]

RELS: list[dict] = [
    {"name": "CONTAINS", "from": ["Repo", "Package", "File", "Symbol"], "to": ["Component", "Package", "File", "Symbol"], "implemented": True,
     "description": "Иерархия: Repo→Component, Repo→Package, Repo→File (не Go), Package→File, File→Symbol, Type→Method, Interface→interface_method.", "properties": []},
    {"name": "IMPORTS", "from": ["Package"], "to": ["Package"], "implemented": True, "description": "Пакет импортирует пакет того же репозитория.", "properties": []},
    {"name": "CALLS", "from": ["Symbol"], "to": ["Symbol"], "implemented": True,
     "description": "Вызов. via: static — прямой; interface — вызов интерфейсного метода (реализации через IMPLEMENTS); value — функция передана как значение (callback); derived — вычислено после загрузки: вызов интерфейса → его реализация, confidence 0.8.",
     "properties": ["via", "line"]},
    {"name": "IMPLEMENTS", "from": ["Symbol"], "to": ["Symbol"], "implemented": True, "description": "Тип реализует интерфейс; метод реализует интерфейсный метод.", "properties": []},
    {"name": "REFERENCES", "from": ["Symbol", "Schema"], "to": ["Symbol", "Schema"], "implemented": True,
     "description": "Использование типа: how = param | return | field | embeds | instantiates | uses. Между схемами OpenAPI: how = field | items | allOf | oneOf | anyOf.",
     "properties": ["how", "field", "line"]},
    {"name": "TESTS", "from": ["Symbol"], "to": ["Symbol"], "implemented": True, "description": "Тест проверяет символ: how = calls (вызывает) | names (имя теста содержит имя символа).", "properties": ["how", "line"]},
    {"name": "MOCKS", "from": ["Symbol"], "to": ["HttpEndpoint"], "implemented": True, "description": "e2e-тест (Playwright page.route) мокает эндпоинт; раскрывается на все методы с этим путём, derived=true.", "properties": ["pattern", "prefix", "line"]},
    {"name": "VISITS", "from": ["Symbol"], "to": ["UiRoute"], "implemented": True, "description": "e2e-тест открывает страницу (page.goto).", "properties": ["path", "line"]},
    {"name": "HANDLES", "from": ["Symbol", "Unresolved"], "to": ["HttpEndpoint", "UiRoute"], "implemented": True, "description": "Обработчик обслуживает эндпоинт (из роутера) или страницу (+page.svelte, load).", "properties": ["middlewares", "kind", "line"]},
    {"name": "CALLS_ENDPOINT", "from": ["Symbol"], "to": ["HttpEndpoint"], "implemented": True, "description": "Клиентский код вызывает эндпоинт сервера: how = fetch | xhr | event_source; client_path — путь как в коде.", "properties": ["method", "how", "client_path", "line"]},
    {"name": "ACCEPTS", "from": ["HttpEndpoint"], "to": ["Schema"], "implemented": True, "description": "Тело запроса эндпоинта. in = body | multipart | form.", "properties": ["in", "mime", "array"]},
    {"name": "RETURNS", "from": ["HttpEndpoint", "SseEvent"], "to": ["Schema"], "implemented": True, "description": "Ответ эндпоинта со статусом; error_codes — коды ошибок из примеров.", "properties": ["status", "mime", "array", "error_codes"]},
    {"name": "HAS_FIELD", "from": ["Schema"], "to": ["Field"], "implemented": True, "description": "Поле схемы.", "properties": ["required"]},
    {"name": "SERIALIZES", "from": ["Symbol"], "to": ["Schema"], "implemented": True,
     "description": "Структура кода (DTO) соответствует схеме OpenAPI. match = name | json_tags; missing_in_code / missing_in_spec — расхождения полей.",
     "properties": ["match", "missing_in_code", "missing_in_spec"]},
    {"name": "MIRRORS", "from": ["Symbol"], "to": ["Schema"], "implemented": True, "description": "Клиентский TS-тип повторяет схему OpenAPI (по имени); missing_in_code / missing_in_spec — расхождения полей.", "properties": ["match", "missing_in_code", "missing_in_spec"]},
    {"name": "USES_FIELD", "from": ["Symbol"], "to": ["Field"], "implemented": True, "description": "Клиентский код читает или пишет поле контракта. via = script (по типам, confidence 1.0) | markup (по объявленному типу переменной в разметке Svelte, confidence 0.8).", "properties": ["access", "via", "line"]},
    {"name": "EMITS", "from": ["HttpEndpoint"], "to": ["SseEvent"], "implemented": False, "description": "Эндпоинт отдаёт SSE-событие.", "properties": ["line"]},
    {"name": "LISTENS", "from": ["Symbol"], "to": ["SseEvent"], "implemented": False, "description": "Клиент слушает SSE-событие.", "properties": ["line"]},
    {"name": "NAVIGATES_TO", "from": ["Symbol"], "to": ["UiRoute"], "implemented": False, "description": "Переход на страницу.", "properties": ["line"]},
    {"name": "READS", "from": ["Symbol"], "to": ["Table", "Column", "CacheKey"], "implemented": False, "description": "Чтение таблицы/колонки/ключа кэша.", "properties": ["sql", "columns", "line"]},
    {"name": "WRITES", "from": ["Symbol"], "to": ["Table", "Column", "CacheKey"], "implemented": False, "description": "Запись в таблицу/колонку/ключ кэша.", "properties": ["sql", "columns", "line"]},
    {"name": "PRODUCES", "from": ["Symbol"], "to": ["Event", "Channel"], "implemented": False, "description": "Публикация события/сообщения.", "properties": ["line"]},
    {"name": "CONSUMES", "from": ["Symbol"], "to": ["Event", "Channel"], "implemented": False, "description": "Обработка события/сообщения.", "properties": ["line"]},
    {"name": "CALLS_EXTERNAL", "from": ["Symbol"], "to": ["ExternalSystem"], "implemented": False, "description": "Вызов внешней системы через SDK.", "properties": ["operation", "line"]},
    {"name": "READS_CONFIG", "from": ["Symbol"], "to": ["ConfigKey"], "implemented": False, "description": "Чтение ключа конфигурации.", "properties": ["line"]},
    {"name": "DEFINES", "from": ["File"], "to": ["Table", "Column"], "implemented": False, "description": "Миграция создаёт/меняет таблицу или колонку.", "properties": ["op", "line"]},
    {"name": "REACHES", "from": ["HttpEndpoint", "UiRoute", "Symbol"], "to": ["Table", "Column", "CacheKey", "Channel", "Event", "ExternalSystem", "HttpEndpoint"], "implemented": False,
     "description": "Производное: что достижимо из точки входа по CALLS*. path — цепочка fqn.", "properties": ["path", "path_len", "derived"]},
    {"name": "MENTIONS", "from": ["Adr", "Issue", "PullRequest", "DocChunk"], "to": ["File", "Symbol", "HttpEndpoint", "Schema", "Field", "Table", "Column", "UiRoute", "ConfigKey", "Unresolved"], "implemented": False,
     "description": "Документ упоминает элемент кода или контракта.", "properties": ["kind", "text", "line"]},
    {"name": "CITES", "from": ["Adr", "Issue", "PullRequest", "DocChunk"], "to": ["Adr", "Issue", "PullRequest"], "implemented": False, "description": "Документ ссылается на другой документ.", "properties": ["text"]},
    {"name": "DEPENDS_ON", "from": ["Issue"], "to": ["Issue", "Adr"], "implemented": False, "description": "Зависимость задач.", "properties": ["text"]},
    {"name": "SUPERSEDES", "from": ["Adr"], "to": ["Adr"], "implemented": False, "description": "ADR переопределяет другой ADR.", "properties": ["partial"]},
    {"name": "CHUNK_OF", "from": ["DocChunk"], "to": ["Adr", "Issue", "File"], "implemented": False, "description": "Фрагмент принадлежит документу.", "properties": ["order"]},
    {"name": "ABOUT", "from": ["Concept", "DocChunk"], "to": ["Package", "Symbol", "HttpEndpoint", "Table", "Schema", "Concept"], "implemented": False, "description": "Понятие домена относится к элементу.", "properties": ["confidence"]},
    {"name": "ALIAS_OF", "from": ["Unresolved"], "to": ["*"], "implemented": False, "description": "Резолв нераспознанной ссылки.", "properties": ["confidence"]},
]

COMMON_EDGE_PROPS = ["claim_id", "repo", "commit", "source_ref", "extracted_by", "confidence"]

# Full-text indexes per label (properties searched by graph.py search).
FULLTEXT: dict[str, list[str]] = {
    "Symbol": ["name", "tokens", "doc", "summary"],
    "File": ["name", "path"],
    "Package": ["name", "path", "doc"],
    "HttpEndpoint": ["path", "operation_id", "summary", "description"],
    "Field": ["name", "description"],
    "Schema": ["name", "description"],
    "Table": ["name"],
    "Column": ["name"],
    "UiRoute": ["path", "title"],
    "Adr": ["title", "summary"],
    "Issue": ["title", "body"],
    "DocChunk": ["heading", "text"],
    "Concept": ["name", "aliases"],
    "Unresolved": ["name"],
}

EXAMPLES: list[tuple[str, str]] = [
    ("Кто обрабатывает эндпоинт", "MATCH (h)-[:HANDLES]->(e:HttpEndpoint {key:'GET /api/v1/orders/{}'}) RETURN h.key, h.file, h.line_start"),
    ("Что вызывает функция, включая реализации интерфейсов", "MATCH (s:Symbol {key:$k})-[c:CALLS*1..4]->(t:Symbol) RETURN DISTINCT t.key, t.file, t.line_start"),
    ("Кто вызывает функцию (входящие)", "MATCH (caller:Symbol)-[c:CALLS]->(s:Symbol {key:$k}) RETURN caller.key, c.via, c.line"),
    ("Эндпоинты, отдающие схему с полем", "MATCH (f:Field {name:'merchant_name'})<-[:HAS_FIELD]-(s:Schema)<-[r:RETURNS]-(e:HttpEndpoint) RETURN e.key, r.status, s.name"),
    ("DTO-структура схемы и её расхождения", "MATCH (d:Symbol)-[s:SERIALIZES]->(sc:Schema {name:'Order'}) RETURN d.key, s.match, s.missing_in_code, s.missing_in_spec"),
    ("Тесты символа", "MATCH (t:Symbol {kind:'test'})-[r:TESTS]->(s:Symbol {key:$k}) RETURN t.key, t.file, r.how"),
    ("Полнотекст по символам", "CALL db.idx.fulltext.queryNodes('Symbol', 'merchant name') YIELD node, score RETURN node.key, node.kind, score ORDER BY score DESC LIMIT 20"),
    ("Реализации интерфейса", "MATCH (impl:Symbol)-[:IMPLEMENTS]->(i:Symbol {kind:'interface', name:'OrderReader'}) RETURN impl.key, impl.is_mock"),
    ("Эндпоинты только в коде или только в спеке", "MATCH (e:HttpEndpoint) WHERE NOT (e.in_spec AND e.in_code) RETURN e.key, e.in_spec, e.in_code"),
    ("Кто на клиенте вызывает эндпоинт и какая страница до него доходит", "MATCH (c:Symbol)-[ce:CALLS_ENDPOINT]->(e:HttpEndpoint {key:$e}) OPTIONAL MATCH (p:Symbol {kind:'page'})-[:CALLS*1..4]->(c) RETURN c.key, ce.line, collect(DISTINCT p.key)"),
    ("Кто читает поле контракта на клиенте", "MATCH (f:Field {key:$f})<-[u:USES_FIELD]-(s:Symbol) RETURN s.key, u.access, u.via, u.line"),
    ("Сквозная связка клиент → эндпоинт → обработчик", "MATCH (c:Symbol)-[:CALLS_ENDPOINT]->(e:HttpEndpoint)<-[:HANDLES]-(h:Symbol) RETURN c.repo, c.key, e.key, h.key"),
]


def labels() -> list[str]:
    return [l["name"] for l in LABELS]


def rel_types() -> list[str]:
    return [r["name"] for r in RELS]


def claims() -> Claims:
    c = Claims()
    for l in LABELS:
        c.node("Meta", f"label:{l['name']}", kind="label", name=l["name"], description=l["description"], key_format=l["key_format"],
               properties=l["properties"], implemented=l["implemented"], repo="_meta")
    for r in RELS:
        c.node("Meta", f"rel:{r['name']}", kind="rel", name=r["name"], description=r["description"], key_format="claim_id",
               properties=r["properties"] + COMMON_EDGE_PROPS, **{"from": r["from"], "to": r["to"]}, implemented=r["implemented"], repo="_meta")
    for i, (title, cypher) in enumerate(EXAMPLES, 1):
        c.node("Meta", f"example:{i}", kind="example", name=title, cypher=cypher, repo="_meta")
    return c
