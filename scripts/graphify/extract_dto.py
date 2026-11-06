"""SERIALIZES: which code struct produces which OpenAPI schema.

A struct matches a schema by name (OrderDTO → Order) or, failing that, by an
identical set of json field names. The edge records the drift in both directions.
"""

from __future__ import annotations

from .model import Claims, Source

DTO_LAYERS = {"delivery", "handler", "handlers", "api", "http", "transport", "rest", "dto", "controller", "controllers"}
NAME_SUFFIXES = ("DTO", "Dto", "Response", "Request", "View", "Resp", "Req")


def link(all_claims: Claims, repo: str, src: Source) -> Claims:
    c = Claims()
    schemas = {
        n["props"]["name"]: n
        for n in all_claims.nodes_by_label("Schema")
        if n["props"].get("owner_repo") == repo and n["props"].get("kind") in ("object", "inline")
    }
    fields_of: dict[str, set[str]] = {}
    for e in all_claims.edges_by_type("HAS_FIELD"):
        field = all_claims.get("Field", e["to"]["key"])
        if field:
            fields_of.setdefault(e["from"]["key"], set()).add(field["props"]["name"])
    by_fieldset: dict[frozenset[str], list[str]] = {}
    for name, sn in schemas.items():
        fs = frozenset(fields_of.get(sn["key"], ()))
        if len(fs) >= 2:
            by_fieldset.setdefault(fs, []).append(sn["key"])

    for sym in all_claims.nodes_by_label("Symbol"):
        p = sym["props"]
        if p.get("repo") != repo or p.get("kind") != "type" or not p.get("json_fields"):
            continue
        path = (p.get("file") or "").lower()
        if p.get("layer") not in DTO_LAYERS and "dto" not in path and "/api/" not in path:
            continue
        candidates: list[tuple[str, str]] = []
        name = p["name"]
        variants = [name] + [name[: -len(s)] for s in NAME_SUFFIXES if name.endswith(s) and len(name) > len(s)]
        for v in variants:
            if v in schemas:
                candidates.append((schemas[v]["key"], "name"))
                break
        if not candidates:
            keys = by_fieldset.get(frozenset(p["json_fields"]), [])
            if len(keys) == 1:
                candidates.append((keys[0], "json_tags"))
        for skey, match in candidates[:1]:
            spec_fields = fields_of.get(skey, set())
            code_fields = set(p["json_fields"])
            props = src.props(p["file"], p.get("line_start", 0), match=match,
                              missing_in_code=sorted(spec_fields - code_fields) or None,
                              missing_in_spec=sorted(code_fields - spec_fields) or None,
                              common_fields=len(spec_fields & code_fields))
            # A name match without a single shared field is more likely two unrelated
            # structs with similar names than contract drift; leave it for a human to judge.
            if match == "name" and not (spec_fields & code_fields):
                props["match"] = "name_only"
                props["confidence"] = 0.5
            c.edge("SERIALIZES", ("Symbol", sym["key"]), ("Schema", skey), props)
    return c
