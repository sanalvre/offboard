"""Convert Salesforce describe() and Airtable base-schema payloads into FieldSpecs.

Scale and compare_on are set here, once, because that is where the semantic traps live
(skills/problem-domain.md 3.3): Salesforce percent literal 50 == 50% (scale 100), Airtable percent 0.5 (scale 1);
ISPICKVAL compares API names, Airtable selects compare labels.
"""
from __future__ import annotations

from typing import Any, Optional

from ..models import FieldSpec, Option

SF_TYPE_MAP = {
    "picklist": "picklist", "currency": "currency", "percent": "percent", "double": "number", "int": "number",
    "long": "number", "string": "text", "textarea": "text", "email": "text", "phone": "text", "url": "text",
    "boolean": "checkbox",
}

AT_TYPE_MAP = {
    "singleSelect": "picklist", "currency": "currency", "percent": "percent", "number": "number",
    "singleLineText": "text", "multilineText": "text", "richText": "text", "email": "text", "url": "text",
    "phoneNumber": "text", "checkbox": "checkbox",
}


def sf_field_spec(f: dict[str, Any]) -> Optional[FieldSpec]:
    t = SF_TYPE_MAP.get(f.get("type"))
    if t is None:
        return None
    opts = [Option(api_name=p["value"], label=p.get("label", p["value"])) for p in f.get("picklistValues", []) if p.get("active", True)]
    return FieldSpec(name=f["name"], type=t, nullable=bool(f.get("nillable", True)),
                     scale=100.0 if t == "percent" else 1.0, options=opts, compare_on="api_name",
                     description=f.get("inlineHelpText") or f.get("label") or "")


def sf_fields(describe: dict[str, Any]) -> dict[str, FieldSpec]:
    out: dict[str, FieldSpec] = {}
    for f in describe.get("fields", []):
        spec = sf_field_spec(f)
        if spec:
            out[spec.name] = spec
    return out


def at_field_spec(f: dict[str, Any]) -> Optional[FieldSpec]:
    t = AT_TYPE_MAP.get(f.get("type"))
    if t is None:
        return None
    opts = []
    if t == "picklist":
        for c in (f.get("options") or {}).get("choices", []):
            opts.append(Option(api_name=c.get("id", c["name"]), label=c["name"]))
    # Airtable checkboxes are never blank; selects, numbers and text can be
    return FieldSpec(name=f["name"], type=t, nullable=t != "checkbox", scale=1.0, options=opts, compare_on="label",
                     description=f.get("description") or "")


def at_fields(table: dict[str, Any]) -> dict[str, FieldSpec]:
    out: dict[str, FieldSpec] = {}
    for f in table.get("fields", []):
        spec = at_field_spec(f)
        if spec:
            out[spec.name] = spec
    return out
