---
name: graphify
description: Index a repository into the FalkorDB impact graph. Use when the user says /graphify, asks to index / crawl / load a repo into the graph, re-index after changes, or asks what is indexed. Runs scripts/indexer.py (repo files → crawler-go → OpenAPI → DTO links → FalkorDB) and verifies the result.
argument-hint: <repo-path-or-name> [graph] [--reset]
---

# /graphify — index a repository into the impact graph

Arguments: `$ARGUMENTS` = `<repo> [graph] [--reset]`

- `<repo>` — path to a repository. A bare name is looked up as a sibling directory of this project (`../<name>`).
- `[graph]` — FalkorDB graph name, default `impact`. One graph holds all repositories; cross-service impact only works inside one graph.
- `--reset` — delete the whole graph first. Without it, re-indexing a repository replaces only that repository's claims.

Project root: the directory that holds this `.claude/`. All commands below run from there.

## Steps

1. **FalkorDB up.** `docker ps --format '{{.Names}} {{.Status}}' | grep falkordb` — if missing, `docker compose up -d` and wait for `(healthy)`.
2. **Toolchain.** `.venv/bin/python -c "import falkordb, yaml"`; `go version` (needed when the repo has `go.mod`; the crawler builds itself into `.graphify/crawler-go`).
3. **Index.**
   ```bash
   .venv/bin/python scripts/indexer.py <repo> --graph <graph> [--reset] -v
   ```
   Extra flags when needed: `--name <repo-name>` if the directory name is not the name to use in keys; `--openapi-glob 'docs/api/*.yaml'` if the OpenAPI files are somewhere unusual; `--no-tests` to skip `_test.go`; `--no-load --out claims.json` to inspect without touching the graph.
4. **Read the output.** The script prints per-extractor counts, dropped edges whose endpoint node is missing ("dangling"), derived-edge counts and the final graph size. Dangling `HANDLES Symbol->HttpEndpoint` or `SERIALIZES` mean the Go crawler and OpenAPI disagreed on a key — report them, don't hide them.
5. **Verify** with `scripts/graph.py` (read-only):
   ```bash
   .venv/bin/python scripts/graph.py --graph <graph> repos
   .venv/bin/python scripts/graph.py --graph <graph> query "MATCH (e:HttpEndpoint) RETURN count(e), sum(CASE WHEN e.in_spec AND e.in_code THEN 1 ELSE 0 END) AS both, sum(CASE WHEN e.in_spec AND NOT e.in_code THEN 1 ELSE 0 END) AS spec_only, sum(CASE WHEN e.in_code AND NOT e.in_spec THEN 1 ELSE 0 END) AS code_only"
   .venv/bin/python scripts/graph.py --graph <graph> query "MATCH (u:Unresolved {repo: '<name>'}) RETURN u.kind, u.name, u.reason LIMIT 20"
   .venv/bin/python scripts/graph.py --graph <graph> search "<a word from the repo's domain>"
   ```
6. **Report** in a few lines: repo, commit, graph, node/edge counts by label/type, contract mismatches (`spec_only` / `code_only` endpoints), unresolved items, and what the graph does not cover yet (`graph.py schema` marks labels and edge types that no extractor produces).

## Notes

- Keys never contain the commit sha; `source_ref` on every edge does. Re-indexing keeps keys stable.
- Index the repository that owns the API contracts before its clients: clients `MERGE` into the owner's `HttpEndpoint`/`Schema` nodes.
- Repositories without `go.mod` get L0 (files, components) and OpenAPI only; say so in the report.
- Never run `GRAPH.DELETE` by hand to "fix" one repo — use `--reset` only when the user asks for a clean graph.
