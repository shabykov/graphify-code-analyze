#!/usr/bin/env python3
"""Index one repository into the impact graph.

    .venv/bin/python scripts/indexer.py <repo> [--name N] [--graph impact] [--reset]

Steps: repo files → crawler-go (Go code + routes) → OpenAPI → DTO links → load
into FalkorDB → derived edges → Meta. Run with --no-load to only write JSON.
"""

from __future__ import annotations

import argparse
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
    ap.add_argument("--only", help="comma-separated extractors: repo,go,openapi,dto (default: all that apply)")
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
    only = set(args.only.split(",")) if args.only else {"repo", "go", "openapi", "dto"}
    commit = extract_repo.git_short(root)
    src = Source(repo=name, commit=commit)
    started = time.time()
    log(f"indexing {name} @ {commit} from {root}")

    all_claims = Claims()
    if "repo" in only:
        all_claims.merge(extract_repo.extract(root, name, src.by("repo")))
        log(f"  repo: {len(all_claims.nodes)} nodes")
    if "go" in only and os.path.exists(os.path.join(root, "go.mod")):
        if not shutil.which("go"):
            log("  go toolchain not found; skipping Go code")
        else:
            t = time.time()
            all_claims.merge(run_crawler(root, name, commit, not args.no_tests, args.verbose))
            log(f"  crawler-go: done in {time.time() - t:.1f}s")
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
    if args.no_load:
        return 0

    from graphify.falkor import Graph  # imported late so --no-load works without a running FalkorDB

    g = Graph(args.graph, args.host, args.port, verbose=args.verbose)
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
