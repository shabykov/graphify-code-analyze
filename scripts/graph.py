#!/usr/bin/env python3
"""Thin tools over the impact graph for an agent that writes its own Cypher.

    graph.py schema                      # labels, edge types, key formats, examples (from Meta nodes)
    graph.py query "<cypher>" [-p k=v]   # read-only query, table output
    graph.py search "<text>" [--labels Symbol,Field] [-k 20]
    graph.py read "<source_ref | symbol key>" [--context 15]
    graph.py repos                       # indexed repositories

All commands take --graph (default: impact), --host, --port.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graphify import meta  # noqa: E402
from graphify.falkor import Graph  # noqa: E402
from graphify.model import tokens  # noqa: E402

SEARCH_LABELS = ["Symbol", "HttpEndpoint", "Field", "Schema", "Package", "File", "Table", "Column", "UiRoute", "Adr", "Issue", "DocChunk", "Concept"]


def cell(v: Any, width: int) -> str:
    if v is None:
        s = ""
    elif hasattr(v, "properties") and hasattr(v, "labels"):  # Node
        labels = getattr(v, "labels", None) or []
        s = f"{labels[0] if labels else '?'}{{{v.properties.get('key', '')}}}"
    elif hasattr(v, "relation"):  # Edge
        s = f"-[{v.relation}]->"
    elif isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v).replace("\n", "⏎")
    if width and len(s) > width:
        s = s[: width - 1] + "…"
    return s


def table(header: list[str], rows: list[list[Any]], width: int) -> str:
    cells = [[cell(v, width) for v in row] for row in rows]
    widths = [len(h) for h in header]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header))]
    out.append("  ".join("-" * w for w in widths))
    out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in cells]
    return "\n".join(out)


def cmd_schema(g: Graph, args: argparse.Namespace) -> int:
    res = g.query("MATCH (m:Meta) RETURN m.kind, m.name, m.description, m.key_format, m.properties, m.from, m.to, m.implemented, m.cypher ORDER BY m.kind, m.name")
    if not res.result_set:
        print("no Meta nodes: the graph is empty or was loaded without scripts/indexer.py", file=sys.stderr)
        return 1
    print("# Узлы (label · формат key · реализовано · описание · свойства)\n")
    for kind, name, desc, keyf, props, frm, to, impl, cypher in res.result_set:
        if kind != "label":
            continue
        print(f"- **{name}** · key: `{keyf}` · {'✓' if impl else 'ещё не извлекается'}\n  {desc}\n  props: {', '.join(props or [])}")
    print("\n# Рёбра (type · from → to · реализовано · описание · свойства)\n")
    for kind, name, desc, keyf, props, frm, to, impl, cypher in res.result_set:
        if kind != "rel":
            continue
        print(f"- **{name}** · {', '.join(frm or [])} → {', '.join(to or [])} · {'✓' if impl else 'ещё не извлекается'}\n  {desc}\n  props: {', '.join(props or [])}")
    print("\n# Примеры запросов\n")
    for kind, name, desc, keyf, props, frm, to, impl, cypher in res.result_set:
        if kind == "example":
            print(f"- {name}:\n  `{cypher}`")
    print("\nЗапросы: `graph.py query \"<cypher>\"`; параметры `-p k=v`. Полнотекст: `graph.py search \"<слова>\"`.")
    return 0


def parse_params(items: list[str] | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items or []:
        k, _, v = item.partition("=")
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    return params


def cmd_query(g: Graph, args: argparse.Namespace) -> int:
    res = g.query(args.cypher, parse_params(args.param), read_only=not args.write)
    header = [h[1] for h in res.header] if res.header else []
    rows = res.result_set or []
    if args.json:
        print(json.dumps([[cell(v, 0) if hasattr(v, "properties") or hasattr(v, "relation") else v for v in row] for row in rows], ensure_ascii=False, indent=1))
    elif header:
        print(table(header, rows, 0 if args.wide else args.width))
        print(f"\n{len(rows)} row(s)")
    else:
        print("ok")
    return 0


def _ft_escape(tok: str) -> str:
    return re.sub(r"[^\w*]", " ", tok).strip()


def cmd_search(g: Graph, args: argparse.Namespace) -> int:
    text = args.text.strip()
    labels = args.labels.split(",") if args.labels else SEARCH_LABELS
    hits: list[tuple[float, str, str, str, str]] = []  # score, label, key, kind, snippet
    seen: set[tuple[str, str]] = set()

    def add(score: float, label: str, props: dict) -> None:
        key = props.get("key", "")
        if (label, key) in seen:
            return
        seen.add((label, key))
        snippet = props.get("summary") or props.get("doc") or props.get("description") or props.get("signature") or props.get("path") or props.get("title") or ""
        where = f"{props.get('file')}:{props.get('line_start')}" if props.get("file") else (props.get("spec_ref") or props.get("source_ref") or "")
        hits.append((score, label, key, str(props.get("kind") or ""), f"{where} · {snippet}".strip(" ·").replace("\n", " ")[:160]))

    # 1. exact key or name
    for label in labels:
        res = g.query(f"MATCH (n:{label}) WHERE n.key = $t OR n.name = $t OR n.fqn = $t OR n.path = $t RETURN n LIMIT 20", {"t": text})
        for (n,) in res.result_set:
            add(100.0, label, n.properties)
    # 2. full-text: all words, then any word as prefix
    words = [w for w in tokens(text).split() if len(w) >= 2]
    queries = []
    if words:
        queries.append(" ".join(_ft_escape(w) for w in words))
        queries.append("|".join(f"{_ft_escape(w)}*" for w in words))
    for qi, q in enumerate(queries):
        for label in labels:
            if label not in meta.FULLTEXT:
                continue
            try:
                res = g.query(f"CALL db.idx.fulltext.queryNodes('{label}', $q) YIELD node, score RETURN node, score ORDER BY score DESC LIMIT $k", {"q": q, "k": args.k})
            except Exception:
                continue
            for n, score in res.result_set:
                add(float(score) * (1.0 if qi == 0 else 0.5), label, n.properties)
    hits.sort(key=lambda h: -h[0])
    if not hits:
        print("nothing found")
        return 1
    print(table(["score", "label", "key", "kind", "where · snippet"], [[f"{h[0]:.2f}", h[1], h[2], h[3], h[4]] for h in hits[: args.k]], 0 if args.wide else 110))
    return 0


_REF_RE = re.compile(r"^(?P<repo>[^@:]+)@(?P<sha>[^:]+):(?P<path>[^:]+?)(?::L(?P<line>\d+))?$")


def cmd_read(g: Graph, args: argparse.Namespace) -> int:
    target = args.ref.strip()
    m = _REF_RE.match(target)
    if m:
        repo, path, line = m.group("repo"), m.group("path"), int(m.group("line") or 0)
        start, end = max(1, line - args.context), line + args.context
    else:
        res = g.query("MATCH (s:Symbol {key: $k}) RETURN s.repo, s.file, s.line_start, s.line_end", {"k": target})
        if not res.result_set:
            res = g.query("MATCH (f:File {key: $k}) RETURN f.repo, f.path, 1, f.lines", {"k": target})
        if not res.result_set:
            print(f"not a source_ref and no Symbol/File with key {target!r}", file=sys.stderr)
            return 1
        repo, path, start, end = res.result_set[0]
        start = max(1, int(start) - 2)
        end = int(end) + 2
    root_res = g.query("MATCH (r:Repo {key: $k}) RETURN r.root", {"k": repo})
    if not root_res.result_set:
        print(f"repo {repo!r} is not in the graph", file=sys.stderr)
        return 1
    full = os.path.join(root_res.result_set[0][0], path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        print(f"cannot read {full}: {e}", file=sys.stderr)
        return 1
    end = min(end, len(lines))
    print(f"# {repo}:{path}:L{start}-L{end}")
    for i in range(start, end + 1):
        print(f"{i:>5}  {lines[i - 1]}")
    return 0


def cmd_repos(g: Graph, args: argparse.Namespace) -> int:
    res = g.query("MATCH (r:Repo) OPTIONAL MATCH (r)-[:CONTAINS]->(c:Component) RETURN r.key, r.lang, r.indexed_commit, r.indexed_at, r.github, r.root, collect(c.key) ORDER BY r.key")
    if not res.result_set:
        print("no repositories indexed")
        return 1
    print(table(["repo", "lang", "commit", "indexed_at", "github", "root", "components"], res.result_set, 0))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", default=os.environ.get("GRAPHIFY_GRAPH", "impact"))
    ap.add_argument("--host", default=os.environ.get("GRAPHIFY_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPHIFY_PORT", "6379")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="vocabulary of the graph")
    q = sub.add_parser("query", help="run a Cypher query")
    q.add_argument("cypher")
    q.add_argument("-p", "--param", action="append", help="k=v (v parsed as JSON when possible)")
    q.add_argument("--write", action="store_true", help="allow writes (default: read-only)")
    q.add_argument("--json", action="store_true")
    q.add_argument("--wide", action="store_true", help="do not truncate cells")
    q.add_argument("--width", type=int, default=90)
    s = sub.add_parser("search", help="exact + full-text search across labels")
    s.add_argument("text")
    s.add_argument("--labels")
    s.add_argument("-k", type=int, default=20)
    s.add_argument("--wide", action="store_true")
    r = sub.add_parser("read", help="print source around a source_ref or a Symbol/File key")
    r.add_argument("ref")
    r.add_argument("--context", type=int, default=15)
    sub.add_parser("repos", help="indexed repositories")
    args = ap.parse_args()
    g = Graph(args.graph, args.host, args.port)
    return {"schema": cmd_schema, "query": cmd_query, "search": cmd_search, "read": cmd_read, "repos": cmd_repos}[args.cmd](g, args)


if __name__ == "__main__":
    sys.exit(main())
