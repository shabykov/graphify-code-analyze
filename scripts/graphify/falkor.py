"""FalkorDB loader: indexes and constraints first, then nodes by key, then edges
by claim_id, then the derived edges. Query helpers for the agent CLI live here too.
"""

from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from typing import Any

from falkordb import FalkorDB

from . import meta
from .model import Claims, chunks

IGNORABLE = ("already indexed", "already exists", "Constraint already exists")


class Graph:
    def __init__(self, name: str, host: str = "localhost", port: int = 6379, verbose: bool = False) -> None:
        self.db = FalkorDB(host=host, port=port)
        self.name = name
        self.g = self.db.select_graph(name)
        self.verbose = verbose

    def log(self, *a: Any) -> None:
        if self.verbose:
            print(*a, file=sys.stderr)

    # -- basics ----------------------------------------------------------------

    def query(self, cypher: str, params: dict[str, Any] | None = None, read_only: bool = True):
        if read_only:
            return self.g.ro_query(cypher, params or {})
        return self.g.query(cypher, params or {})

    def exists(self) -> bool:
        return self.name in self.db.list_graphs()

    def delete(self) -> None:
        if self.exists():
            self.g.delete()

    def clear(self) -> None:
        """Empty the graph in place. Dropping the key resets FalkorDB's graph version,
        and the browser UI keeps a cached version that then stops matching
        ("ERR invalid graph version" until its process restarts)."""
        if self.exists():
            self.g.query("MATCH (n) DETACH DELETE n")

    def _try(self, cypher: str) -> bool:
        try:
            self.g.query(cypher)
            return True
        except Exception as e:  # FalkorDB reports an existing index as an error
            if any(s in str(e) for s in IGNORABLE):
                return False
            raise

    # -- schema ----------------------------------------------------------------

    def ensure_schema(self) -> None:
        for label in meta.labels():
            self._try(f"CREATE INDEX FOR (n:{label}) ON (n.key)")
            self._try(f"CREATE INDEX FOR (n:{label}) ON (n.repo)")
            try:
                self.g.create_node_unique_constraint(label, "key")
            except Exception as e:
                if not any(s in str(e) for s in IGNORABLE):
                    raise
        self._try("CREATE INDEX FOR (n:Symbol) ON (n.file)")
        self._try("CREATE INDEX FOR (n:Symbol) ON (n.kind)")
        self._try("CREATE INDEX FOR (n:Symbol) ON (n.name)")
        self._try("CREATE INDEX FOR (n:File) ON (n.path)")
        for rel in meta.rel_types():
            self._try(f"CREATE INDEX FOR ()-[r:{rel}]-() ON (r.claim_id)")
            self._try(f"CREATE INDEX FOR ()-[r:{rel}]-() ON (r.repo)")
            try:
                self.g.create_edge_unique_constraint(rel, "claim_id")
            except Exception as e:
                if not any(s in str(e) for s in IGNORABLE):
                    raise
        for label, props in meta.FULLTEXT.items():
            cols = ", ".join(f"n.{p}" for p in props)
            self._try(f"CREATE FULLTEXT INDEX FOR (n:{label}) ON ({cols})")
        self.wait_constraints()

    def wait_constraints(self, timeout: float = 60.0) -> None:
        """Constraints build asynchronously; loading under a PENDING one still works, but a FAILED one must be seen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.g.ro_query("CALL db.constraints() YIELD status RETURN collect(DISTINCT status)")
            statuses = res.result_set[0][0] if res.result_set else []
            if "PENDING" not in statuses:
                if "FAILED" in statuses:
                    raise RuntimeError("a uniqueness constraint failed to build; the graph already holds duplicate keys")
                return
            time.sleep(0.2)

    # -- loading ---------------------------------------------------------------

    def load(self, claims: Claims, batch: int = 1000) -> dict[str, Any]:
        stats: dict[str, Any] = {"nodes": Counter(), "edges": Counter(), "dangling": Counter()}
        by_label: dict[str, list[dict]] = defaultdict(list)
        for n in claims.nodes:
            by_label[n["label"]].append({"key": n["key"], "props": n["props"]})
        for label, rows in by_label.items():
            for part in chunks(rows, batch):
                self.g.query(f"UNWIND $rows AS r MERGE (n:{label} {{key: r.key}}) SET n += r.props", {"rows": part})
            stats["nodes"][label] += len(rows)
            self.log(f"  nodes {label}: {len(rows)}")

        by_shape: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for e in claims.edges:
            by_shape[(e["type"], e["from"]["label"], e["to"]["label"])].append(
                {"from": e["from"]["key"], "to": e["to"]["key"], "claim_id": e["claim_id"], "props": e["props"]}
            )
        for (typ, fl, tl), rows in by_shape.items():
            linked = 0
            for part in chunks(rows, batch):
                res = self.g.query(
                    f"UNWIND $rows AS r MATCH (a:{fl} {{key: r.from}}) MATCH (b:{tl} {{key: r.to}}) "
                    f"MERGE (a)-[e:{typ} {{claim_id: r.claim_id}}]->(b) SET e += r.props RETURN count(e)",
                    {"rows": part},
                )
                linked += int(res.result_set[0][0]) if res.result_set else 0
            stats["edges"][typ] += linked
            if linked < len(rows):
                stats["dangling"][f"{typ} {fl}->{tl}"] += len(rows) - linked
            self.log(f"  edges {typ} {fl}->{tl}: {linked}/{len(rows)}")
        return stats

    def derive(self) -> dict[str, int]:
        """Edges computed from the loaded graph: endpoint defaults and calls through interfaces."""
        out: dict[str, int] = {}
        # Playwright mocks match a URL regardless of method: expand each "ANY <path>"
        # placeholder to the real endpoints with that path (or path prefix), then drop it.
        res = self.g.query(
            "MATCH (t:Symbol)-[m:MOCKS]->(p:HttpEndpoint {method:'ANY'}) "
            "MATCH (e:HttpEndpoint) WHERE e.method <> 'ANY' AND "
            "(e.path_pattern = p.path_pattern OR (coalesce(m.prefix, false) AND e.path_pattern STARTS WITH p.path_pattern + '/')) "
            "MERGE (t)-[d:MOCKS {claim_id: m.claim_id + ':' + e.key}]->(e) "
            "SET d.repo = m.repo, d.commit = m.commit, d.source_ref = m.source_ref, d.line = m.line, d.extracted_by = m.extracted_by, "
            "d.confidence = m.confidence, d.pattern = m.pattern, d.derived = true RETURN count(d)"
        )
        out["MOCKS expanded"] = int(res.result_set[0][0]) if res.result_set else 0
        res = self.g.query("MATCH (p:HttpEndpoint {method:'ANY'}) DETACH DELETE p RETURN count(p)")
        out["MOCKS placeholders removed"] = int(res.result_set[0][0]) if res.result_set else 0
        self.g.query("MATCH (e:HttpEndpoint) WHERE e.path IS NULL SET e.path = coalesce(e.path_code, e.path_pattern)")
        self.g.query("MATCH (e:HttpEndpoint) SET e.in_spec = coalesce(e.in_spec, false), e.in_code = coalesce(e.in_code, false), e.in_client = coalesce(e.in_client, false)")
        res = self.g.query(
            "MATCH (a:Symbol)-[c:CALLS {via:'interface'}]->(im:Symbol {kind:'interface_method'})<-[:IMPLEMENTS]-(impl:Symbol) "
            "WHERE coalesce(impl.is_mock, false) = false AND coalesce(impl.test_file, false) = false AND a.key <> impl.key "
            "MERGE (a)-[d:CALLS {claim_id: 'derived:' + c.claim_id + ':' + impl.key}]->(impl) "
            "SET d.via = 'derived', d.derived = true, d.repo = a.repo, d.commit = c.commit, d.source_ref = c.source_ref, "
            "d.line = c.line, d.extracted_by = 'derive', d.confidence = 0.8 RETURN count(d)"
        )
        out["CALLS derived"] = int(res.result_set[0][0]) if res.result_set else 0
        return out

    def load_meta(self) -> None:
        self.g.query("MATCH (m:Meta) DELETE m")
        self.load(meta.claims())

    def remove_repo(self, repo: str) -> dict[str, int]:
        """Drop everything one repository produced: its edges, then its own nodes, then seam nodes nobody references."""
        out: dict[str, int] = {}
        for rel in meta.rel_types():
            res = self.g.query(f"MATCH ()-[r:{rel} {{repo: $repo}}]->() DELETE r RETURN count(r)", {"repo": repo})
            out[f"edges {rel}"] = int(res.result_set[0][0]) if res.result_set else 0
        owned = ["Repo", "Component", "Package", "File", "Symbol", "Unresolved", "Adr", "Issue", "PullRequest", "DocChunk"]
        for label in owned:
            res = self.g.query(f"MATCH (n:{label} {{repo: $repo}}) DETACH DELETE n RETURN count(n)", {"repo": repo})
            out[f"nodes {label}"] = int(res.result_set[0][0]) if res.result_set else 0
        res = self.g.query(
            "MATCH (n) WHERE (n:HttpEndpoint OR n:Schema OR n:Field OR n:SseEvent OR n:UiRoute OR n:Table OR n:Column OR n:CacheKey "
            "OR n:Channel OR n:Event OR n:ExternalSystem OR n:ConfigKey) AND n.owner_repo = $repo AND NOT (n)--() DELETE n RETURN count(n)",
            {"repo": repo},
        )
        out["orphan seams"] = int(res.result_set[0][0]) if res.result_set else 0
        return out

    def counts(self) -> tuple[Counter, Counter]:
        nodes = Counter()
        for row in self.g.ro_query("MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c").result_set:
            nodes[row[0]] = row[1]
        edges = Counter()
        for row in self.g.ro_query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c").result_set:
            edges[row[0]] = row[1]
        return nodes, edges
