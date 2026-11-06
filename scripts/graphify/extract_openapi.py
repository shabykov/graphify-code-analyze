"""L2 contracts from OpenAPI 3 specs: HttpEndpoint, Schema, Field and the edges
ACCEPTS, RETURNS, HAS_FIELD, REFERENCES.

The YAML is read as a node tree so every claim keeps its line in the spec.
"""

from __future__ import annotations

import glob
import os
from typing import Any
from urllib.parse import urlparse

import yaml

from .model import Claims, Source, endpoint_key, normalize_path, truncate

DEFAULT_GLOBS = [
    "api/*.yaml", "api/*.yml", "openapi.yaml", "openapi.yml", "openapi/*.yaml", "openapi/*.yml",
    "docs/openapi*.yaml", "docs/openapi*.yml", "docs/api/*.yaml", "api/openapi/*.yaml", "spec/*.yaml", "specs/*.yaml",
]
METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


# -- yaml node helpers -----------------------------------------------------------

def _items(node: Any) -> list[tuple[str, Any, int]]:
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(str(k.value), v, k.start_mark.line + 1) for k, v in node.value]


def _get(node: Any, key: str) -> Any:
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if str(k.value) == key:
            return v
    return None


def _s(node: Any) -> str | None:
    if isinstance(node, yaml.ScalarNode):
        return str(node.value)
    return None


def _bool(node: Any) -> bool | None:
    v = _s(node)
    if v is None:
        return None
    return v.lower() in ("true", "yes", "on")


def _seq(node: Any) -> list[Any]:
    return list(node.value) if isinstance(node, yaml.SequenceNode) else []


def _line(node: Any) -> int:
    return node.start_mark.line + 1 if node is not None else 0


def _ref_name(node: Any) -> str | None:
    ref = _s(_get(node, "$ref"))
    return ref.rsplit("/", 1)[-1] if ref else None


def _resolve(node: Any, components: Any) -> Any:
    """Follow a $ref into components.<section>; other nodes pass through."""
    name = _ref_name(node)
    if name and components is not None:
        target = _get(components, name)
        if target is not None:
            return target
    return node


def _server_prefix(servers: Any) -> str:
    for srv in _seq(servers):
        url = _s(_get(srv, "url")) or ""
        if url.startswith("/"):
            return url.rstrip("/")
        path = urlparse(url).path
        return path.rstrip("/")
    return ""


def _error_codes(node: Any, out: set[str]) -> None:
    """Collect error_code values from examples and enums anywhere under node."""
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if str(k.value) == "error_code":
                if isinstance(v, yaml.ScalarNode):
                    out.add(str(v.value))
                else:
                    for cand in (_get(v, "example"), *_seq(_get(v, "enum"))):
                        if isinstance(cand, yaml.ScalarNode):
                            out.add(str(cand.value))
            _error_codes(v, out)
    elif isinstance(node, yaml.SequenceNode):
        for v in node.value:
            _error_codes(v, out)


def _status(s: str) -> int | str:
    return int(s) if s.isdigit() else s


# -- extraction --------------------------------------------------------------------

class _Spec:
    def __init__(self, c: Claims, src: Source, rel: str, root: Any) -> None:
        self.c = c
        self.src = src
        self.rel = rel
        self.repo = src.repo
        self.root = root
        comps = _get(root, "components")
        self.schemas = _get(comps, "schemas")
        self.parameters = _get(comps, "parameters")
        self.responses = _get(comps, "responses")
        self.request_bodies = _get(comps, "requestBodies")
        self.title = _s(_get(_get(root, "info"), "title"))

    def run(self) -> None:
        for name, sn, line in _items(self.schemas):
            self.schema(name, sn, line)
        prefix = _server_prefix(_get(self.root, "servers"))
        global_auth = "bearer" if _seq(_get(self.root, "security")) else "none"
        for path, item, _ in _items(_get(self.root, "paths")):
            if path.startswith("x-"):
                continue
            item_params = _seq(_get(item, "parameters"))
            for method, op, oline in _items(item):
                if method.lower() not in METHODS:
                    continue
                self.operation(prefix + path, method.upper(), op, oline, item_params, global_auth)

    # -- schemas ---------------------------------------------------------------

    def schema_key(self, name: str) -> str:
        return f"{self.repo}:{name}"

    def schema(self, name: str, sn: Any, line: int, inline: bool = False) -> str:
        key = self.schema_key(name)
        kind = "enum" if _get(sn, "enum") is not None else ("array" if _s(_get(sn, "type")) == "array" else "object")
        if inline:
            kind = "inline"
        required = [str(r.value) for r in _seq(_get(sn, "required")) if isinstance(r, yaml.ScalarNode)]
        self.c.node(
            "Schema", key,
            name=name, repo=self.repo, owner_repo=self.repo, kind=kind, type=_s(_get(sn, "type")),
            description=truncate(_s(_get(sn, "description")), 2000),
            enum_values=[str(v.value) for v in _seq(_get(sn, "enum"))] or None,
            required=required or None, spec_file=self.rel, spec_ref=self.src.ref(self.rel, line), source_ref=self.src.ref(self.rel, line),
        )
        self.fields(key, name, sn, required)
        for comb in ("allOf", "oneOf", "anyOf"):
            for part in _seq(_get(sn, comb)):
                ref = _ref_name(part)
                if ref:
                    self.c.edge("REFERENCES", ("Schema", key), ("Schema", self.schema_key(ref)), self.src.props(self.rel, _line(part), how=comb))
                elif _get(part, "properties") is not None:
                    self.fields(key, name, part, required + [str(r.value) for r in _seq(_get(part, "required"))])
        if kind == "array":
            ref = _ref_name(_get(sn, "items"))
            if ref:
                self.c.edge("REFERENCES", ("Schema", key), ("Schema", self.schema_key(ref)), self.src.props(self.rel, line, how="items"))
        return key

    def fields(self, skey: str, sname: str, sn: Any, required: list[str]) -> None:
        for fname, fn, fline in _items(_get(sn, "properties")):
            ref = _ref_name(fn)
            items_ref = _ref_name(_get(fn, "items"))
            ftype = _s(_get(fn, "type"))
            if items_ref:
                ftype = f"array<{items_ref}>"
            elif ref:
                ftype = ref
            elif ftype == "array":
                ftype = f"array<{_s(_get(_get(fn, 'items'), 'type')) or 'object'}>"
            fkey = f"{skey}.{fname}"
            self.c.node(
                "Field", fkey,
                name=fname, schema=sname, repo=self.repo, owner_repo=self.repo, type=ftype, format=_s(_get(fn, "format")),
                required=fname in required, nullable=_bool(_get(fn, "nullable")),
                description=truncate(_s(_get(fn, "description")), 1000),
                enum_values=[str(v.value) for v in _seq(_get(fn, "enum"))] or None,
                example=truncate(_s(_get(fn, "example")), 200),
                spec_ref=self.src.ref(self.rel, fline), source_ref=self.src.ref(self.rel, fline),
            )
            self.c.edge("HAS_FIELD", ("Schema", skey), ("Field", fkey), self.src.props(self.rel, fline, required=fname in required))
            target = ref or items_ref
            if target:
                self.c.edge("REFERENCES", ("Schema", skey), ("Schema", self.schema_key(target)), self.src.props(self.rel, fline, how="field", field=fname, array=bool(items_ref) or None))

    def schema_target(self, schema: Any, inline_name: str) -> tuple[str, bool] | None:
        """(schema key, is_array) for a media-type schema; inline objects become their own Schema."""
        if schema is None:
            return None
        ref = _ref_name(schema)
        if ref:
            return self.schema_key(ref), False
        if _s(_get(schema, "type")) == "array":
            items = _get(schema, "items")
            iref = _ref_name(items)
            if iref:
                return self.schema_key(iref), True
            if _get(items, "properties") is not None:
                return self.schema(inline_name, items, _line(items), inline=True), True
            return None
        if _get(schema, "properties") is not None or _get(schema, "allOf") is not None:
            return self.schema(inline_name, schema, _line(schema), inline=True), False
        return None

    # -- operations --------------------------------------------------------------

    def operation(self, full_path: str, method: str, op: Any, oline: int, item_params: list[Any], global_auth: str) -> None:
        key = endpoint_key(method, full_path)
        op_id = _s(_get(op, "operationId")) or f"{method.lower()}_{normalize_path(full_path).strip('/').replace('/', '_').replace('{}', 'x')}"
        auth = global_auth
        sec = _get(op, "security")
        if sec is not None:
            schemes = _seq(sec)
            auth = "none" if not schemes else (str(schemes[0].value[0][0].value) if isinstance(schemes[0], yaml.MappingNode) and schemes[0].value else "bearer")
            if auth.lower().startswith("bearer"):
                auth = "bearer"
        responses = _get(op, "responses")
        sse = any("text/event-stream" in mime for _, resp, _ in _items(responses) for mime, _, _ in _items(_get(_resolve(resp, self.responses), "content")))
        if sse and auth != "none":
            auth = "sse_token"
        params: list[str] = []
        for p in item_params + _seq(_get(op, "parameters")):
            p = _resolve(p, self.parameters)
            loc, name = _s(_get(p, "in")), _s(_get(p, "name"))
            if loc and name:
                params.append(f"{loc}:{name}")
        all_codes: set[str] = set()
        self.c.node(
            "HttpEndpoint", key,
            name=key, method=method, path=full_path, path_pattern=normalize_path(full_path), operation_id=op_id,
            summary=truncate(_s(_get(op, "summary")), 500), description=truncate(_s(_get(op, "description")), 4000),
            tags=[str(t.value) for t in _seq(_get(op, "tags"))] or None, auth=auth, sse=sse or None, in_spec=True,
            owner_repo=self.repo, repo=self.repo, spec_file=self.rel, spec_title=self.title,
            spec_ref=self.src.ref(self.rel, oline), source_ref=self.src.ref(self.rel, oline), params=params or None,
        )
        ep = ("HttpEndpoint", key)

        body = _resolve(_get(op, "requestBody"), self.request_bodies)
        for mime, media, mline in _items(_get(body, "content")):
            target = self.schema_target(_get(media, "schema"), f"{op_id}.request")
            if target is None:
                continue
            where = "multipart" if "multipart" in mime else ("form" if "form" in mime else "body")
            self.c.edge("ACCEPTS", ep, ("Schema", target[0]), self.src.props(self.rel, mline, **{"in": where, "mime": mime, "array": target[1] or None}))

        for status, resp, rline in _items(responses):
            resp = _resolve(resp, self.responses)
            codes: set[str] = set()
            _error_codes(resp, codes)
            all_codes |= codes
            for mime, media, mline in _items(_get(resp, "content")):
                target = self.schema_target(_get(media, "schema"), f"{op_id}.response{status}")
                if target is None:
                    continue
                self.c.edge("RETURNS", ep, ("Schema", target[0]), self.src.props(self.rel, mline, status=_status(status), mime=mime, array=target[1] or None, error_codes=sorted(codes) or None))
        if all_codes:
            self.c.node("HttpEndpoint", key, error_codes=sorted(all_codes))


def find_specs(root: str, globs: list[str] | None = None) -> list[str]:
    found: set[str] = set()
    for pattern in globs or DEFAULT_GLOBS:
        for path in glob.glob(os.path.join(root, pattern)):
            if os.path.isfile(path):
                found.add(os.path.abspath(path))
    return sorted(found)


def extract(root: str, src: Source, globs: list[str] | None = None) -> Claims:
    c = Claims()
    root = os.path.abspath(root)
    for path in find_specs(root, globs):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.compose(f.read(), Loader=yaml.SafeLoader)
        except Exception:
            continue
        if not isinstance(doc, yaml.MappingNode):
            continue
        keys = {str(k.value) for k, _ in doc.value}
        if "openapi" not in keys and "swagger" not in keys:
            continue
        _Spec(c, src, rel, doc).run()
    return c
