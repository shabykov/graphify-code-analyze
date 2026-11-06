#!/usr/bin/env python3
"""Thin tools over the impact graph for an agent that writes its own Cypher.

    graph.py schema                      # labels, edge types, key formats, examples (from Meta nodes)
    graph.py query "<cypher>" [-p k=v]   # read-only query, table output
    graph.py search "<text>" [--labels Symbol,Field] [-k 20]
    graph.py read "<source_ref | symbol key>" [--context 15]
    graph.py repos                       # indexed repositories
    graph.py viz "<cypher returning nodes/edges/paths>" [--out file.html] [--open]

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


LABEL_COLORS = {
    "Repo": "#1f3a5f", "Component": "#2f5d8a", "Package": "#5b7db1", "File": "#9db4d6", "Symbol": "#f0a830",
    "HttpEndpoint": "#d64545", "Schema": "#7b4fa6", "Field": "#b690d6", "Unresolved": "#888888", "Meta": "#cccccc",
    "Table": "#2e8b57", "Column": "#7fc29b", "Adr": "#c27ba0", "Issue": "#e07b39", "UiRoute": "#3c9fb5",
}

VIZ_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>impact graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/vis-network.min.js"></script>
<style>
 html,body{margin:0;height:100%;font:13px/1.4 -apple-system,Segoe UI,sans-serif;background:#fafafa;color:#222}
 #net{position:absolute;inset:0 0 0 0}
 #side{position:absolute;top:0;right:0;width:360px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ddd;padding:12px;box-sizing:border-box}
 #legend span{display:inline-block;margin:0 8px 6px 0}
 #legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:middle}
 pre{white-space:pre-wrap;word-break:break-all;background:#f3f3f3;padding:8px;border-radius:4px}
 .q{color:#666;font-size:12px}
</style></head><body>
<div id="net"></div>
<div id="side">
 <div class="q"><b>__COUNTS__</b><br>__QUERY__</div>
 <div id="legend"></div>
 <div id="detail">Клик по узлу или ребру — свойства здесь. Колесо — масштаб, перетаскивание — перемещение.</div>
</div>
<script>
const DATA = __DATA__;
const COLORS = __COLORS__;
const legend = document.getElementById('legend');
for (const l of [...new Set(DATA.nodes.map(n => n.label))].sort()) {
  legend.insertAdjacentHTML('beforeend', `<span><i style="background:${COLORS[l]||'#999'}"></i>${l}</span>`);
}
const nodes = new vis.DataSet(DATA.nodes.map(n => ({
  id: n.id, label: n.short, title: n.key, color: {background: COLORS[n.label]||'#999', border: '#555'},
  shape: n.label === 'HttpEndpoint' ? 'box' : (n.label === 'Schema' || n.label === 'Field' ? 'diamond' : 'dot'),
  size: n.label === 'Symbol' ? 10 : 14, font: {size: 11}, data: n
})));
const edges = new vis.DataSet(DATA.edges.map(e => ({
  id: e.id, from: e.from, to: e.to, label: e.type, arrows: 'to', font: {size: 9, align: 'middle', color: '#777'},
  color: {color: '#bbb', highlight: '#333'}, dashes: e.type === 'CALLS' && e.props.via === 'derived', data: e
})));
const net = new vis.Network(document.getElementById('net'), {nodes, edges}, {
  physics: {solver: 'forceAtlas2Based', stabilization: {iterations: 200}, forceAtlas2Based: {gravitationalConstant: -40, springLength: 90}},
  interaction: {hover: true, tooltipDelay: 100}, edges: {smooth: {type: 'continuous'}}
});
function show(obj, kind) {
  const props = Object.entries(obj.props || {}).filter(([k]) => k !== 'emb').map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join('\\n');
  document.getElementById('detail').innerHTML = `<b>${kind}</b> ${obj.label || obj.type}<pre>${props.replace(/</g,'&lt;')}</pre>`;
}
net.on('click', p => {
  if (p.nodes.length) show(nodes.get(p.nodes[0]).data, 'node');
  else if (p.edges.length) show(edges.get(p.edges[0]).data, 'edge');
});
</script></body></html>
"""


def _short_name(label: str, props: dict) -> str:
    key = str(props.get("key", ""))
    if label == "HttpEndpoint":
        return key
    if label in ("Symbol", "Field", "Schema", "Package", "File", "Component"):
        name = props.get("name") or key
        return str(name)[-40:]
    return (props.get("name") or key)[:40]


def _collect(g: Graph, rows: list[list[Any]]) -> tuple[dict[int, dict], dict[int, dict]]:
    """Nodes and edges found anywhere in the result rows (entities, paths, lists)."""
    nodes: dict[int, dict] = {}
    edges: dict[int, dict] = {}

    def add_node(n: Any) -> None:
        label = n.labels[0] if n.labels else "?"
        nodes[n.id] = {"id": n.id, "label": label, "key": n.properties.get("key", ""), "short": _short_name(label, n.properties), "props": n.properties}

    def add_edge(e: Any) -> None:
        edges[e.id] = {"id": e.id, "from": e.src_node, "to": e.dest_node, "type": e.relation, "props": e.properties}

    def visit(v: Any) -> None:
        if hasattr(v, "labels") and hasattr(v, "properties"):
            add_node(v)
        elif hasattr(v, "relation"):
            add_edge(v)
        elif hasattr(v, "nodes") and hasattr(v, "edges"):
            for n in v.nodes():
                add_node(n)
            for e in v.edges():
                add_edge(e)
        elif isinstance(v, list):
            for x in v:
                visit(x)

    for row in rows:
        for v in row:
            visit(v)
    missing = sorted({e["from"] for e in edges.values()} | {e["to"] for e in edges.values()} - set(nodes))
    if missing:
        res = g.query("MATCH (n) WHERE id(n) IN $ids RETURN n", {"ids": missing})
        for (n,) in res.result_set:
            add_node(n)
    return nodes, edges


def cmd_viz(g: Graph, args: argparse.Namespace) -> int:
    res = g.query(args.cypher, parse_params(args.param))
    nodes, edges = _collect(g, res.result_set or [])
    if not nodes:
        print("the query returned no nodes; RETURN the entities (n, r, p), not their properties", file=sys.stderr)
        return 1
    if len(nodes) > args.max_nodes:
        print(f"{len(nodes)} nodes is more than --max-nodes {args.max_nodes}; add LIMIT or narrow the MATCH", file=sys.stderr)
        return 1
    html = (VIZ_TEMPLATE
            .replace("__DATA__", json.dumps({"nodes": list(nodes.values()), "edges": list(edges.values())}, ensure_ascii=False, default=str))
            .replace("__COLORS__", json.dumps(LABEL_COLORS))
            .replace("__COUNTS__", f"{len(nodes)} узлов, {len(edges)} рёбер · граф {g.name}")
            .replace("__QUERY__", args.cypher.replace("<", "&lt;")))
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{len(nodes)} nodes, {len(edges)} edges -> {out}")
    if args.open:
        import webbrowser
        webbrowser.open("file://" + out)
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
    v = sub.add_parser("viz", help="render the nodes/edges a query returns as an interactive HTML page")
    v.add_argument("cypher")
    v.add_argument("-p", "--param", action="append", help="k=v (v parsed as JSON when possible)")
    v.add_argument("--out", default=os.path.join(".graphify", "viz.html"))
    v.add_argument("--max-nodes", type=int, default=1500)
    v.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()
    g = Graph(args.graph, args.host, args.port)
    return {"schema": cmd_schema, "query": cmd_query, "search": cmd_search, "read": cmd_read, "repos": cmd_repos, "viz": cmd_viz}[args.cmd](g, args)


if __name__ == "__main__":
    sys.exit(main())
