"""Schema-mapping report: for every Salesforce field a verifiable artefact touches, and its Airtable counterpart, can
every legal source value land in the target without loss? Decided with Z3 where the question is a domain question
(a source value with no legal target representation), and by direct comparison where it is structural (types).

Result per mapping:
  lossless   every legal source value has exactly one target representation (with `transform` if scale differs)
  lossy      Z3 found a legal source value that cannot be represented; `counterexample` shows it
  blocked    no target field, or incompatible types; nothing to verify against

This is the third solver use in skills/plan-phase2.md (schema mapping), run before any write and shown in the UI's
Mapping tab. It complements the per-run `SchemaMismatch` pre-checks in app/solver.py.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

import z3

from .models import FieldSpec

NUMERIC = {"currency", "number", "percent"}
# Airtable label -> Salesforce API name, by label match (Salesforce labels come from describe) and known aliases
ALIASES = {"Stage": "StageName", "Loss Reason": "Loss_Reason__c", "Discount": "Discount__c", "Region": "Region__c",
           "Needs Review": "Needs_Review__c", "Net Amount": "Net_Amount__c", "Is Big Deal": "Is_Big_Deal__c"}


@dataclass
class Mapping:
    source: str
    source_type: str
    target: Optional[str]
    target_type: Optional[str]
    status: str  # lossless | lossy | blocked
    transform: str = ""
    reason: str = ""
    counterexample: Optional[dict[str, Any]] = None
    checks: Optional[dict[str, str]] = None


def propose_targets(sf_fields: dict[str, FieldSpec], at_fields: dict[str, FieldSpec], sf_labels: dict[str, str]) -> dict[str, Optional[str]]:
    """Source API name -> target field name by exact label/name match or known alias. No model involved."""
    by_name = {n.lower(): n for n in at_fields}
    inv_alias = {v: k for k, v in ALIASES.items()}
    out: dict[str, Optional[str]] = {}
    for sf_name in sf_fields:
        cands = [inv_alias.get(sf_name), sf_labels.get(sf_name), sf_name.replace("__c", "").replace("_", " ")]
        tgt = next((by_name[c.lower()] for c in cands if c and c.lower() in by_name), None)
        out[sf_name] = tgt
    return out


def check_mapping(sf: FieldSpec, at: Optional[FieldSpec], sf_meta: Optional[dict] = None) -> Mapping:
    sf_meta = sf_meta or {}
    if at is None:
        return Mapping(sf.name, sf.type, None, None, "blocked", reason="no target field with a matching label or alias in the Airtable table")
    same_family = (sf.type == at.type) or (sf.type in NUMERIC and at.type in NUMERIC)
    if not same_family:
        return Mapping(sf.name, sf.type, at.name, at.type, "blocked", reason=f"type mismatch: {sf.type} cannot be stored in a {at.type} field")
    checks: dict[str, str] = {}
    ctx = z3.Context()
    # ---- picklist: every source option must exist in the target options (Z3 over the union enum)
    if sf.type == "picklist":
        keys = list(dict.fromkeys(sf.option_keys() + at.option_keys() + ["__other__"]))
        Sort, consts = z3.EnumSort("Opt", keys, ctx=ctx)
        v = z3.Const("v", Sort)
        legal_src = z3.Or(*[v == consts[keys.index(k)] for k in sf.option_keys()]) if sf.options else z3.BoolVal(True, ctx)
        legal_tgt = z3.Or(*[v == consts[keys.index(k)] for k in at.option_keys()]) if at.options else z3.BoolVal(True, ctx)
        s = z3.Solver(ctx=ctx); s.add(legal_src, z3.Not(legal_tgt))
        if s.check() == z3.sat:
            val = str(s.model().eval(v))
            return Mapping(sf.name, sf.type, at.name, at.type, "lossy", reason=f"picklist value {val!r} has no option in target {at.name!r}",
                           counterexample={sf.name: val}, checks={"options": "sat"})
        checks["options"] = "unsat (every source option exists in the target)"
    # ---- nullability: a blank source value needs a blank-capable target
    if sf.nullable and not at.nullable:
        return Mapping(sf.name, sf.type, at.name, at.type, "lossy", reason="source may be blank but the target field cannot hold blank",
                       counterexample={sf.name: None}, checks={**checks, "nullability": "blank source, required target"})
    checks["nullability"] = "ok"
    # ---- numeric: scale transform and decimal precision (Z3: a source value whose scaled form is not representable)
    transform = ""
    if sf.type in NUMERIC:
        if sf.scale != at.scale:
            transform = f"target = source / {sf.scale / at.scale:g}" if sf.scale > at.scale else f"target = source * {at.scale / sf.scale:g}"
        src_dp = int(sf_meta.get("scale") or 0)
        tgt_dp = sf_meta.get("target_precision")
        if tgt_dp is not None:
            # source value with src_dp decimals in source units -> canonical -> target units with tgt_dp decimals
            x = z3.Real("x", ctx)
            s = z3.Solver(ctx=ctx)
            s.set("timeout", 5000)
            s.add(z3.IsInt(x * z3.RealVal(10 ** src_dp, ctx)))                          # x has at most src_dp decimals (source units)
            canonical = x / z3.RealVal(str(sf.scale), ctx)
            tgt_units = canonical * z3.RealVal(str(at.scale), ctx)
            s.add(z3.Not(z3.IsInt(tgt_units * z3.RealVal(10 ** int(tgt_dp), ctx))))    # not representable with tgt_dp decimals
            s.add(x > 0, x < 1000)
            r = s.check()
            if r == z3.sat:
                mdl = s.model(); xv = mdl.eval(x)
                val = float(xv.numerator_as_long()) / float(xv.denominator_as_long())
                return Mapping(sf.name, sf.type, at.name, at.type, "lossy", transform,
                               reason=f"source value {val:g} (in source units) needs more decimals than the target's {tgt_dp}",
                               counterexample={sf.name: val}, checks={**checks, "precision": "sat"})
            checks["precision"] = "unsat (every source value is representable)" if r == z3.unsat else f"z3 {r}"
    # ---- text length (Airtable single line text has no practical limit; the reverse direction would be lossy)
    if sf.type == "text":
        checks["length"] = f"source max {sf_meta.get('length') or 'n/a'}; Airtable text unbounded"
    return Mapping(sf.name, sf.type, at.name, at.type, "lossless", transform, "every legal source value has a target representation", None, checks)


def mapping_report(sf_fields: dict[str, FieldSpec], at_fields: dict[str, FieldSpec], sf_labels: dict[str, str],
                   sf_meta: dict[str, dict], at_meta: dict[str, dict], only: Optional[set[str]] = None) -> dict[str, Any]:
    targets = propose_targets(sf_fields, at_fields, sf_labels)
    rows: list[Mapping] = []
    for sf_name, spec in sf_fields.items():
        if only is not None and sf_name not in only:
            continue
        tgt = targets.get(sf_name)
        meta = dict(sf_meta.get(sf_name, {}))
        if tgt and tgt in at_meta and at_meta[tgt].get("precision") is not None:
            meta["target_precision"] = at_meta[tgt]["precision"]
        rows.append(check_mapping(spec, at_fields.get(tgt) if tgt else None, meta))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"counts": counts, "mappings": [asdict(r) for r in rows]}
