# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A "ticket → code → cross-service impact" graph. Extractors load repositories into one FalkorDB graph, `impact`. An LLM agent queries it through thin tools, writes its own Cypher, and answers three questions: where in the code a change lands, what inside the repo depends on it, and which other services are affected.

## Layout

- `graph-schema-v2.md` — the current design (labels, edges, pipeline, verified FalkorDB behaviour). Build against it.
- `graph-schema.md` — the previous design (Kafka/gRPC crawler, graphs `global` / `code:<repo>@<commit>` / `markers`). Reference only.
- `crawler-go/` — Go module. Analyses a Go repository with `go/packages` + `go/types` and prints claims JSON: `Package`, `File`, `Symbol`, `CONTAINS`, `IMPORTS`, `CALLS`, `IMPLEMENTS`, `REFERENCES`, `TESTS`, plus chi / `http.ServeMux` routes → `HttpEndpoint`, `HANDLES`. The indexer builds it into `.graphify/crawler-go` whenever its sources are newer than the binary.
- `scripts/indexer.py` + `scripts/graphify/` — Python orchestration: `Repo`/`Component`/non-Go `File`s, runs the crawler, OpenAPI → `HttpEndpoint`/`Schema`/`Field`/`ACCEPTS`/`RETURNS`/`HAS_FIELD`, `SERIALIZES` (DTO struct ↔ schema, with field drift), FalkorDB loader (indexes and constraints first, nodes by `key`, edges by `claim_id`, derived `CALLS {via:'derived'}`), `Meta` nodes. `model.py` defines the claims JSON both sides share.
- `scripts/graph.py` — the agent's tools: `schema`, `query`, `search`, `read`, `repos`.
- `.claude/skills/graphify/`, `.claude/skills/impact/` — `/graphify <repo> [graph]` indexes a repository; `/impact <#issue | repo#issue | adr.md | text>` runs the analysis.
- `.venv/` — Python 3.14 with `falkordb`, `pyyaml`. Always run Python as `.venv/bin/python`.

Not extracted yet (`implemented: False` in `scripts/graphify/meta.py`, shown as "ещё не извлекается" by `graph.py schema` — that list is authoritative): SQL → `Table`/`Column`/`DEFINES`/`READS`/`WRITES`, `CacheKey`, `Channel`/`Event`/`PRODUCES`/`CONSUMES`, `SseEvent`/`EMITS`/`LISTENS`, `ExternalSystem`/`ConfigKey`, `UiRoute`/`NAVIGATES_TO`, a TS/Svelte crawler (`CALLS_ENDPOINT`, `MIRRORS`, `USES_FIELD`, `MOCKS`, `VISITS`), docs/ADR/issue extractors (`Adr`, `Issue`, `PullRequest`, `DocChunk`, `MENTIONS`, `CITES`, `DEPENDS_ON`, `SUPERSEDES`, `CHUNK_OF`), `ALIAS_OF`, `REACHES`, embeddings/`Concept`/`ABOUT`. Repositories without `go.mod` currently get L0 and OpenAPI only.

Ask the user which repos to index and where they are. Index contract owners (APIs) before their clients: client extractors merge into the seams the owner created. Keep real project names out of the docs; examples use `orders-api` / `orders-web`.

## Commands

```bash
docker compose up -d                                   # FalkorDB: redis on :6379, browser UI on http://localhost:3001

.venv/bin/python scripts/indexer.py <repo> --graph impact -v        # index or re-index one repository
.venv/bin/python scripts/indexer.py <repo> --reset                  # wipe the graph first
.venv/bin/python scripts/indexer.py <repo> --no-load --out c.json   # extract only, inspect the JSON

.venv/bin/python scripts/graph.py repos
.venv/bin/python scripts/graph.py schema
.venv/bin/python scripts/graph.py query "MATCH (e:HttpEndpoint) RETURN e.key LIMIT 5"
.venv/bin/python scripts/graph.py search "merchant name"
.venv/bin/python scripts/graph.py read "<source_ref or Symbol/File key>"

cd crawler-go && GOTOOLCHAIN=auto go vet ./... && go build -o ../.graphify/crawler-go .   # manual crawler build
../.graphify/crawler-go -repo <repo> -v -out /dev/null                                     # crawler alone, stats on stderr

docker exec falkordb redis-cli GRAPH.QUERY impact "MATCH (m:Meta) RETURN m.name"   # raw access; redis-cli is not on the host
```

A read query against a missing graph creates an empty one, so a typo in the graph name silently succeeds.

## Design rules (details in graph-schema-v2.md)

- One graph for all repos: FalkorDB has no cross-graph queries.
- Every node has a `key` (stable, no commit sha) and `repo` (seam nodes: `owner_repo`); every edge has `claim_id`, `source_ref` (`<repo>@<sha>:<path>:L<n>`), `extracted_by`, `confidence`, `repo`. One edge = one piece of evidence.
- Seam nodes (L2: `HttpEndpoint`, `Schema`, `Field`, `Table`, …) get the same canonical key from every extractor, so `MERGE` from different repos lands on one node. `HttpEndpoint` key: `METHOD /path` with every parameter replaced by `{}`.
- Load order: `MERGE` nodes by `key` first, then `MERGE` edges by `claim_id` and `SET` the rest. Never `MERGE` a whole `(a)-[r]->(b)` pattern — FalkorDB duplicates the nodes when the edge is missing.
- Create range indexes and uniqueness constraints before loading; the plan cache keeps plans built before an index existed.
- Deterministic extraction first; embeddings/LLM edges carry `confidence < 1` and never overwrite deterministic ones.
- Unresolved references become `Unresolved` nodes. A missing consumer in the graph means "unknown", not "none".
- Language split: an analyser for a language is written in that language (Go crawler in Go; a TS crawler would be Node) and emits the claims JSON; Python orchestrates, parses specs and docs, loads. New labels/edges are declared in `scripts/graphify/meta.py` with `implemented: True` once an extractor produces them.
