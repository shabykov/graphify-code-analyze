"""Neutral exchange format between extractors and the loader.

Nodes are keyed by (label, key); edges are claims that each carry one piece of
evidence (source_ref). The same claim seen twice stays one edge; new evidence for
the same relation becomes a second edge. crawler-go/internal/model mirrors this.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    """Drop None values; FalkorDB properties are scalars or lists of scalars."""
    out: dict[str, Any] = {}
    for k, v in props.items():
        if v is None:
            continue
        if isinstance(v, tuple):
            v = list(v)
        if isinstance(v, (set, frozenset)):
            v = sorted(v)
        out[k] = v
    return out


def _without_key(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in props.items() if k != "key"}


class Claims:
    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self._nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self._edges: dict[str, dict[str, Any]] = {}

    # -- nodes -------------------------------------------------------------

    def node(self, label: str, key: str, **props: Any) -> dict[str, Any]:
        """Add a node or merge props into the existing one; later non-None values win."""
        n = self._nodes.get((label, key))
        if n is None:
            n = {"label": label, "key": key, "props": {"key": key}}
            self._nodes[(label, key)] = n
            self.nodes.append(n)
        n["props"].update(_clean(props))
        return n

    def get(self, label: str, key: str) -> dict[str, Any] | None:
        return self._nodes.get((label, key))

    def has(self, label: str, key: str) -> bool:
        return (label, key) in self._nodes

    def nodes_by_label(self, label: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n["label"] == label]

    # -- edges -------------------------------------------------------------

    def edge(self, typ: str, frm: tuple[str, str], to: tuple[str, str], props: dict[str, Any]) -> dict[str, Any]:
        """Add one claim. The id hashes the relation and its source_ref."""
        props = _clean(props)
        source_ref = props.get("source_ref", "")
        raw = "|".join([typ, frm[0], frm[1], to[0], to[1], source_ref])
        claim_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        e = self._edges.get(claim_id)
        if e is not None:
            return e
        props["claim_id"] = claim_id
        e = {
            "type": typ,
            "from": {"label": frm[0], "key": frm[1]},
            "to": {"label": to[0], "key": to[1]},
            "claim_id": claim_id,
            "props": props,
        }
        self._edges[claim_id] = e
        self.edges.append(e)
        return e

    def edges_by_type(self, typ: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["type"] == typ]

    # -- bulk --------------------------------------------------------------

    def merge(self, other: "Claims | None") -> None:
        if other is None:
            return
        for n in other.nodes:
            self.node(n["label"], n["key"], **_without_key(n["props"]))
        for e in other.edges:
            self.edge(e["type"], (e["from"]["label"], e["from"]["key"]), (e["to"]["label"], e["to"]["key"]), e["props"])

    def merge_json(self, path: str) -> None:
        """Merge claims written by a crawler (same JSON shape)."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for n in data.get("nodes", []):
            self.node(n["label"], n["key"], **_without_key(n.get("props", {})))
        for e in data.get("edges", []):
            self.edge(e["type"], (e["from"]["label"], e["from"]["key"]), (e["to"]["label"], e["to"]["key"]), e.get("props", {}))

    def write_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"nodes": self.nodes, "edges": self.edges}, f, ensure_ascii=False)

    def labels(self) -> list[str]:
        return sorted({n["label"] for n in self.nodes})

    def types(self) -> list[str]:
        return sorted({e["type"] for e in self.edges})

    def stats(self) -> str:
        nodes = Counter(n["label"] for n in self.nodes)
        edges = Counter(e["type"] for e in self.edges)
        lines = [f"nodes: {len(self.nodes)}"]
        lines += [f"  {k:<16}{v:>7}" for k, v in sorted(nodes.items())]
        lines.append(f"edges: {len(self.edges)}")
        lines += [f"  {k:<16}{v:>7}" for k, v in sorted(edges.items())]
        return "\n".join(lines)


@dataclass(frozen=True)
class Source:
    """Which scan produced a claim: repo, commit and extractor name."""

    repo: str
    commit: str
    extracted_by: str = ""

    def by(self, extracted_by: str) -> "Source":
        return Source(self.repo, self.commit, extracted_by)

    def ref(self, path: str, line: int = 0) -> str:
        if line > 0:
            return f"{self.repo}@{self.commit}:{path}:L{line}"
        return f"{self.repo}@{self.commit}:{path}"

    def props(self, path: str, line: int = 0, **extra: Any) -> dict[str, Any]:
        p: dict[str, Any] = {
            "repo": self.repo,
            "commit": self.commit,
            "source_ref": self.ref(path, line),
            "extracted_by": self.extracted_by,
            "confidence": 1.0,
        }
        if line > 0:
            p["line"] = line
        p.update(_clean(extra))
        return p


_PARAM_RE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}|:[A-Za-z_][A-Za-z0-9_]*|\*+")


def normalize_path(path: str) -> str:
    """Replace every path parameter with {} so chi ({id}), OpenAPI ({receipt_id}),
    a TS template literal (${id}) and a Playwright glob (*) give the same key."""
    path = _PARAM_RE.sub("{}", path)
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def endpoint_key(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_path(path)}"


_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+|[^\W_]+", re.UNICODE)


def tokens(identifier: str) -> str:
    """Split an identifier into lowercase words: GetByID -> 'get by id',
    store_name -> 'store name', OTPRequest -> 'otp request'."""
    return " ".join(m.group(0).lower() for m in _TOKEN_RE.finditer(identifier))


def snake(identifier: str) -> str:
    return tokens(identifier).replace(" ", "_")


def truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
