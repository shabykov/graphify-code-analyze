"""L0: the repository, its deployable components and its non-Go files.

Go source files are added by crawler-go together with their packages, so this
extractor skips them to keep the containment chain Repo → Package → File.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone

from .model import Claims, Source

SKIP_DIRS = {
    ".git", "vendor", "node_modules", ".idea", ".vscode", ".claude", ".svelte-kit", ".svelte-kit-vitest",
    "build", "dist", "playwright-report", "test-results", ".playwright-mcp", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", "coverage", ".tasks", ".cache", ".graphify",
}
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".webm", ".mp4", ".mov", ".zip", ".gz", ".br", ".map",
    ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".bin", ".exe", ".dylib", ".so", ".pyc", ".lock",
    ".ds_store",
}
MAX_FILE_BYTES = 2 * 1024 * 1024


def git(root: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def git_short(root: str) -> str:
    return git(root, "rev-parse", "--short", "HEAD") or "nogit"


def github_slug(remote: str) -> str | None:
    """owner/repo from an https or ssh GitHub remote."""
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", remote)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def classify(rel: str) -> tuple[str, str]:
    """(kind, lang) of a file by its path."""
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()
    lower = rel.lower()
    if name.endswith("_test.go"):
        return "test", "go"
    if ext == ".go":
        return "source", "go"
    if ext == ".sql":
        return ("migration" if "migration" in lower else "source"), "sql"
    if ext in (".yaml", ".yml"):
        if lower.startswith("api/") or "openapi" in lower or "swagger" in lower:
            return "spec", "yaml"
        return "config", "yaml"
    if ext in (".md", ".markdown", ".rst", ".txt", ".puml"):
        return "doc", "md"
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        kind = "test" if re.search(r"\.(test|spec)\.[tj]sx?$", name) else "source"
        return kind, "ts"
    if ext == ".svelte":
        return "source", "svelte"
    if ext in (".css", ".scss"):
        return "style", "css"
    if ext in (".json", ".toml", ".ini", ".env", ".golangci") or name.startswith(".env") or name in ("go.mod", "go.sum", "package.json"):
        return "config", ext.lstrip(".") or "env"
    if ext == ".sh" or name in ("Makefile", "Dockerfile") or name.startswith("Dockerfile"):
        return "build", "sh"
    if ext in (".html", ".svg", ".xml"):
        return "asset", ext.lstrip(".")
    return "other", ext.lstrip(".") or "none"


TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".svelte"}


def extract(root: str, name: str, src: Source, skip_exts: set[str] | None = None) -> Claims:
    """skip_exts: file extensions a language crawler owns (it creates those File nodes itself)."""
    skip_exts = {".go"} | (skip_exts or set())
    c = Claims()
    root = os.path.abspath(root)
    has_go = os.path.exists(os.path.join(root, "go.mod"))
    has_node = os.path.exists(os.path.join(root, "package.json"))
    lang = "go" if has_go else ("ts" if has_node else "unknown")
    remote = git(root, "remote", "get-url", "origin")
    module = ""
    if has_go:
        with open(os.path.join(root, "go.mod"), encoding="utf-8", errors="replace") as f:
            m = re.search(r"^module\s+(\S+)", f.read(), re.M)
            module = m.group(1) if m else ""

    c.node(
        "Repo",
        name,
        name=name,
        repo=name,
        lang=lang,
        root=root,
        module=module or None,
        remote=remote or None,
        github=github_slug(remote) if remote else None,
        default_branch=git(root, "rev-parse", "--abbrev-ref", "HEAD") or None,
        indexed_commit=src.commit,
        indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # Components: every package main under cmd/, or the single web app.
    components: list[tuple[str, str, str, str]] = []  # (app, kind, entry, runtime)
    cmd_dir = os.path.join(root, "cmd")
    if os.path.isdir(cmd_dir):
        for app in sorted(os.listdir(cmd_dir)):
            d = os.path.join(cmd_dir, app)
            if not os.path.isdir(d):
                continue
            entry = None
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".go") and not fn.endswith("_test.go"):
                    with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                        if re.search(r"^package\s+main\b", f.read(), re.M):
                            entry = f"cmd/{app}/{fn}"
                            break
            if entry:
                components.append((app, _component_kind(app), entry, "go"))
    elif has_go and os.path.exists(os.path.join(root, "main.go")):
        components.append((name, "service", "main.go", "go"))
    if has_node:
        entry = next(
            (f for f in ("svelte.config.js", "svelte.config.ts", "vite.config.ts", "next.config.js", "package.json") if os.path.exists(os.path.join(root, f))),
            "package.json",
        )
        components.append(("web", "web", entry, "node"))
    for app, kind, entry, runtime in components:
        key = f"{name}/{app}"
        c.node("Component", key, name=app, repo=name, kind=kind, entry=entry, runtime=runtime)
        c.edge("CONTAINS", ("Repo", name), ("Component", key), src.props(entry, 1))

    # Files: everything textual except Go sources, which the Go crawler owns.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT or fn.lower() in SKIP_EXT or ext in skip_exts:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            kind, flang = classify(rel)
            key = f"{name}:{rel}"
            c.node("File", key, name=fn, path=rel, repo=name, kind=kind, lang=flang, lines=data.count(b"\n"), size=size)
            c.edge("CONTAINS", ("Repo", name), ("File", key), src.props(rel))
    return c


def _component_kind(app: str) -> str:
    a = app.lower()
    if "api" in a or "server" in a or "http" in a:
        return "api"
    if "consumer" in a or "worker" in a:
        return "consumer"
    if "cron" in a or "ticker" in a or "job" in a:
        return "cron"
    if "migrat" in a:
        return "tool"
    return "service"
