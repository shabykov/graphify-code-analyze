---
name: impact
description: Change impact analysis over the FalkorDB impact graph. Use when the user says /impact, asks what a ticket / issue / ADR / described change will affect, which services or endpoints break, where in the code a change lands, or what to test. Input is a GitHub issue reference, a path to an ADR/markdown, or plain text.
argument-hint: <#issue | repo#issue | path/to/adr.md | free text> [graph=<name>]
---

# /impact — what does this change touch?

Input: `$ARGUMENTS`. You write Cypher yourself through `scripts/graph.py` (read-only). Every statement in the report carries evidence: a `source_ref` / `file:line`, a query result, or a line of a document. A statement without evidence is labelled as an assumption.

Project root: the directory that holds this `.claude/`; run commands from there with `.venv/bin/python`. Graph name: `graph=<name>` in the input, else `impact`.

## 0. Resolve the input to text

| Input looks like | Do |
|---|---|
| `#123` or `<repo>#123` | find the GitHub slug: `graph.py repos` (column `github`); then `gh issue view 123 -R <owner/repo> --json title,body,url,labels,state` — if the number is a PR, `gh pr view`. Without a repo prefix and with several repos indexed, try each; stop at the first hit. |
| a path ending in `.md` | read the file (an ADR or design doc). Its "current implementation" / "code pointers" sections are evidence, not conclusions. |
| anything else | the text itself is the task. |

Name `SERVICE` — the repository where the change is made (from the issue's repo, the ADR's repo, or the text). If it is not clear, ask.

## 1. Learn the vocabulary

`graph.py schema` — labels, edge types, key formats, example queries. Labels and edges marked "ещё не извлекается" are absent: whatever depends on them (DB tables, cache, SSE events, UI pages, docs/issues inside the graph) must be checked by reading code and marked **uncovered by the graph**.

## 2. Find the entry points

From most to least exact:

1. Identifiers in the text — paths, `METHOD /path`, `Type.Method`, field names, `ERROR_CODES`: `graph.py search "<identifier>"` (exact key/name match scores 100).
2. Words — `graph.py search "<two or three words>"` (full-text over symbol names/tokens/docs, endpoints, schema fields, files). Try English identifiers for a ticket written in another language (`merchant name` for «имя продавца»).
3. Structure — `MATCH (p:Package {repo:$r}) RETURN p.key, p.layer, p.doc`, then `MATCH (f:File)-[:CONTAINS]->(s:Symbol) WHERE f.path STARTS WITH '<dir>' …`.

Read the candidates: `graph.py read "<symbol key>"` or `graph.py read "<source_ref>"`. Keep the scope minimal and list what you deliberately left out.

## 3. Scope inside `SERVICE`

For each entry symbol (`$k`):

```cypher
-- who calls it (incoming), through interfaces too
MATCH (caller:Symbol)-[c:CALLS]->(s:Symbol {key:$k}) RETURN caller.key, caller.file, c.line, c.via
-- what it reaches (outgoing, depth-limited)
MATCH (s:Symbol {key:$k})-[:CALLS*1..4]->(t:Symbol) WHERE t.kind IN ['func','method'] RETURN DISTINCT t.key, t.layer, t.file
-- types it touches
MATCH (s:Symbol {key:$k})-[r:REFERENCES]->(t:Symbol) RETURN t.key, r.how
```

`CALLS.via = 'interface'` lands on an interface method; `IMPLEMENTS` or the derived `CALLS {via:'derived'}` (confidence 0.8) continue to implementations — say which one is actually wired when it matters.

## 4. Which integration points are affected, and how

```cypher
-- endpoints served by the scope
MATCH (h:Symbol)-[:HANDLES]->(e:HttpEndpoint) WHERE h.key IN $keys RETURN e.key, e.in_spec, e.in_code, e.auth
-- endpoints that reach the scope through calls
MATCH (h:Symbol)-[:HANDLES]->(e:HttpEndpoint), (h)-[:CALLS*1..5]->(s:Symbol) WHERE s.key IN $keys RETURN DISTINCT e.key
-- contract of an endpoint
MATCH (e:HttpEndpoint {key:$e})-[r:RETURNS|ACCEPTS]->(sc:Schema)-[:HAS_FIELD]->(f:Field) RETURN type(r), r.status, sc.name, collect(f.name)
-- which DTO struct produces the schema, and whether code and spec disagree
MATCH (d:Symbol)-[s:SERIALIZES]->(sc:Schema {key:$s}) RETURN d.key, s.match, s.missing_in_code, s.missing_in_spec
```

For every affected point state **what changes**: the contract (schema/field/status/error code), the semantics (same fields, different meaning or values), or only the implementation. Implementation-only changes do not propagate further.

## 5. Propagate to other services

```cypher
-- who calls this endpoint (indexed clients)
MATCH (c:Symbol)-[r:CALLS_ENDPOINT]->(e:HttpEndpoint {key:$e}) RETURN c.repo, c.key, r.source_ref
-- who mirrors / reads a field of the schema
MATCH (f:Field {key:$f})<-[u:USES_FIELD]-(c:Symbol) RETURN c.repo, c.key, u.access, u.source_ref
MATCH (sc:Schema {key:$s})<-[m:MIRRORS]-(t:Symbol) RETURN t.repo, t.key, m.drift
```

If these edge types are not implemented or return nothing: **no consumer in the graph means "unknown", not "none"**. Then look for consumers in repositories that are indexed but lack a client crawler (grep their code for the path), and in docs; label such findings `docs` or `unknown`.

## 6. Tests

```cypher
MATCH (t:Symbol {kind:'test'})-[r:TESTS]->(s:Symbol) WHERE s.key IN $keys RETURN t.key, t.file, r.how
```

Plus tests of the handlers found in step 4. Name the tests to run and the ones that must change.

## Report

1. **Входные данные** — what was analysed, `SERVICE`, graph, indexed commit (`graph.py repos`).
2. **Scope** — code locations with `file:line` and why each is in scope; what was excluded.
3. **Задетые точки `SERVICE`** — table: point (endpoint/schema/field), how it is reached (chain of keys), nature of change (контракт / семантика / реализация), evidence.
4. **Импакт на другие сервисы** — per service: component, through which point, breaks or not and why, chain, confidence `code` / `docs` / `unknown`.
5. **Тесты** — existing tests covering the scope; tests to add.
6. **Непокрытое графом** — labels/edges not extracted yet, `Unresolved` nodes met on the way, repositories that should be indexed to close gaps.

Keep queries you ran reproducible: quote the final Cypher of each key finding.
