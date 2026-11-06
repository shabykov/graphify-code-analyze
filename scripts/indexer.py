#!/usr/bin/env python3
"""Index one repository into the impact graph.

    .venv/bin/python scripts/indexer.py <repo> [--name N] [--graph impact] [--reset]

Steps: repo files → crawler-go (Go code + routes) → OpenAPI → DTO links → load
into FalkorDB → derived edges → Meta. Run with --no-load to only write JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graphify import extract_dto, extract_openapi, extract_repo  # noqa: E402
from graphify.model import Claims, Source  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRAWLER_SRC = os.path.join(PROJECT, "crawler-go")
CRAWLER_BIN = os.path.join(PROJECT, ".graphify", "crawler-go")
CRAWLER_TS = os.path.join(PROJECT, "crawler-ts")


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def build_crawler(force: bool = False) -> str:
    """Build crawler-go when its binary is missing or older than its sources."""
    newest = 0.0
    for dirpath, _, files in os.walk(CRAWLER_SRC):
        for fn in files:
            if fn.endswith(".go") or fn in ("go.mod", "go.sum"):
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
    if not force and os.path.exists(CRAWLER_BIN) and os.path.getmtime(CRAWLER_BIN) >= newest:
        return CRAWLER_BIN
    os.makedirs(os.path.dirname(CRAWLER_BIN), exist_ok=True)
    env = dict(os.environ, GOTOOLCHAIN=os.environ.get("GOTOOLCHAIN", "auto"))
    log("building crawler-go …")
    subprocess.run(["go", "build", "-o", CRAWLER_BIN, "."], cwd=CRAWLER_SRC, check=True, env=env)
    return CRAWLER_BIN


def run_crawler(root: str, name: str, commit: str, with_tests: bool, verbose: bool) -> Claims:
    binary = build_crawler()
    fd, out = tempfile.mkstemp(prefix="crawler-go-", suffix=".json")
    os.close(fd)
    try:
        cmd = [binary, "-repo", root, "-name", name, "-commit", commit, "-out", out, f"-tests={'true' if with_tests else 'false'}"]
        if verbose:
            cmd.append("-v")
        subprocess.run(cmd, check=True, env=dict(os.environ, GOTOOLCHAIN=os.environ.get("GOTOOLCHAIN", "auto")))
        c = Claims()
        c.merge_json(out)
        return c
    finally:
        os.unlink(out)


def ts_applicable(root: str) -> bool:
    return os.path.exists(os.path.join(root, "package.json")) and os.path.isdir(os.path.join(root, "src"))


def ensure_crawler_ts() -> str:
    """Install crawler-ts dependencies on first use; returns the entry script."""
    if not shutil.which("node"):
        raise RuntimeError("node is not installed; crawler-ts needs Node.js")
    if not os.path.isdir(os.path.join(CRAWLER_TS, "node_modules", "typescript")):
        log("installing crawler-ts dependencies …")
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=CRAWLER_TS, check=True)
    return os.path.join(CRAWLER_TS, "index.mjs")


def contract_schemas(graph, owner: str | None) -> tuple[str, dict[str, list[str]]] | None:
    """Schemas of the repository that owns the API contracts, for MIRRORS / USES_FIELD.
    With one owner in the graph it is picked automatically."""
    if graph is None:
        return None
    res = graph.query("MATCH (s:Schema)-[:HAS_FIELD]->(f:Field) RETURN s.owner_repo, s.name, collect(f.name)")
    by_owner: dict[str, dict[str, list[str]]] = {}
    for o, name, fields in res.result_set:
        by_owner.setdefault(o, {})[name] = sorted(fields)
    if owner:
        return (owner, by_owner.get(owner, {})) if owner in by_owner else None
    if len(by_owner) == 1:
        o = next(iter(by_owner))
        return o, by_owner[o]
    return None


def run_crawler_ts(root: str, name: str, commit: str, schemas: tuple[str, dict[str, list[str]]] | None, verbose: bool) -> Claims:
    entry = ensure_crawler_ts()
    fd, out = tempfile.mkstemp(prefix="crawler-ts-", suffix=".json")
    os.close(fd)
    schemas_file = None
    try:
        cmd = ["node", entry, "--repo", root, "--name", name, "--commit", commit, "--out", out]
        if schemas:
            fd2, schemas_file = tempfile.mkstemp(prefix="schemas-", suffix=".json")
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                json.dump({"owner": schemas[0], "schemas": schemas[1]}, f)
            cmd += ["--schemas", schemas_file]
        if verbose:
            cmd.append("-v")
        subprocess.run(cmd, check=True)
        c = Claims()
        c.merge_json(out)
        return c
    finally:
        os.unlink(out)
        if schemas_file:
            os.unlink(schemas_file)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="path to the repository")
    ap.add_argument("--name", help="repository name used in keys (default: directory name)")
    ap.add_argument("--graph", default="impact", help="FalkorDB graph name (default: impact)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--reset", action="store_true", help="empty the whole graph before loading (in place, so the browser UI keeps working)")
    ap.add_argument("--replace", action="store_true", help="remove this repository's previous claims before loading (default when the repo is already in the graph)")
    ap.add_argument("--no-load", action="store_true", help="extract only; do not touch FalkorDB")
    ap.add_argument("--out", help="also write the claims JSON here")
    ap.add_argument("--only", help="comma-separated extractors: repo,go,ts,openapi,dto (default: all that apply)")
    ap.add_argument("--contracts-owner", help="repo whose OpenAPI schemas client types mirror (default: the only owner in the graph)")
    ap.add_argument("--no-tests", action="store_true", help="skip _test.go files")
    ap.add_argument("--openapi-glob", action="append", help="glob(s) for OpenAPI files relative to the repo (default: api/*.yaml and common locations)")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.repo)
    if not os.path.isdir(root):
        log(f"not a directory: {root}")
        return 2
    name = args.name or os.path.basename(root.rstrip("/"))
    only = set(args.only.split(",")) if args.only else {"repo", "go", "ts", "openapi", "dto"}
    commit = extract_repo.git_short(root)
    src = Source(repo=name, commit=commit)
    started = time.time()
    log(f"indexing {name} @ {commit} from {root}")

    graph = None
    if not args.no_load:
        from graphify.falkor import Graph  # imported late so --no-load works without a running FalkorDB

        graph = Graph(args.graph, args.host, args.port, verbose=args.verbose)

    use_ts = "ts" in only and ts_applicable(root) and shutil.which("node") is not None
    all_claims = Claims()
    if "repo" in only:
        all_claims.merge(extract_repo.extract(root, name, src.by("repo"), skip_exts=extract_repo.TS_EXTS if use_ts else None))
        log(f"  repo: {len(all_claims.nodes)} nodes")
    if "go" in only and os.path.exists(os.path.join(root, "go.mod")):
        if not shutil.which("go"):
            log("  go toolchain not found; skipping Go code")
        else:
            t = time.time()
            all_claims.merge(run_crawler(root, name, commit, not args.no_tests, args.verbose))
            log(f"  crawler-go: done in {time.time() - t:.1f}s")
    if use_ts:
        t = time.time()
        schemas = contract_schemas(graph, args.contracts_owner) if graph is not None and graph.exists() else None
        if schemas:
            log(f"  contracts owner: {schemas[0]} ({len(schemas[1])} schemas)")
        else:
            log("  no contracts owner in the graph: MIRRORS / USES_FIELD skipped (index the API repo first or pass --contracts-owner)")
        all_claims.merge(run_crawler_ts(root, name, commit, schemas, args.verbose))
        log(f"  crawler-ts: done in {time.time() - t:.1f}s")
    elif "ts" in only and ts_applicable(root):
        log("  node not found; skipping TypeScript code")
    if "openapi" in only:
        specs = extract_openapi.find_specs(root, args.openapi_glob)
        all_claims.merge(extract_openapi.extract(root, src.by("openapi"), args.openapi_glob))
        log(f"  openapi: {len(specs)} spec file(s)")
    if "dto" in only:
        linked = extract_dto.link(all_claims, name, src.by("dto"))
        all_claims.merge(linked)
        log(f"  dto: {len(linked.edges)} SERIALIZES")

    log(all_claims.stats())
    if args.out:
        all_claims.write_json(args.out)
        log(f"claims written to {args.out}")
    if args.no_load or graph is None:
        return 0

    g = graph
    if args.reset:
        g.clear()
        log(f"graph {args.graph} cleared")
    g.ensure_schema()
    already = g.exists() and bool(g.query("MATCH (r:Repo {key: $k}) RETURN count(r)", {"k": name}).result_set[0][0])
    if already or args.replace:
        removed = g.remove_repo(name)
        log(f"  removed previous claims of {name}: {sum(removed.values())} items")
    t = time.time()
    stats = g.load(all_claims, batch=args.batch)
    derived = g.derive()
    g.load_meta()
    log(f"loaded into {args.graph} in {time.time() - t:.1f}s")
    for typ, n in derived.items():
        log(f"  derived {typ}: {n}")
    if stats["dangling"]:
        log("  edges whose endpoint node is missing (dropped):")
        for k, n in sorted(stats["dangling"].items()):
            log(f"    {k}: {n}")
    nodes, edges = g.counts()
    log(f"graph now: {sum(nodes.values())} nodes, {sum(edges.values())} edges; total {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
