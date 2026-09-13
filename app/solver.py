"""Z3 equivalence check between a Salesforce rule (Node over source fields) and an Airtable guard
(Node over target fields), each evaluated under its own system's semantics.

Encoding per field (shared variables, so the same record is fed to both sides):
  picklist / text : present:Bool, value:EnumSort over the union of known option keys + literals + OTHER
  currency/number/percent : present:Bool, value:Real in canonical units (0.5 == 50%)
  checkbox : value:Bool (never blank)
Non-nullable fields get `present == True`.

Salesforce side: numeric comparison => And(present, value*scale OP lit); eq on strings => And(present, value==lit);
ISBLANK => Not(present). Airtable side: numeric => If(present, value*scale, 0) OP lit; eq/is_blank as Salesforce.
These mirror app/reference.py; the tests check the two agree.

Outputs (all recorded in the trace): z3 result, SMT-LIB text of the problem posed, counterexample record,
leaf-by-leaf evaluation of both sides on that record (the human-readable "why"), well-formedness of each side,
and the names of tracked assertions in the unsat core when the disagreement is unsatisfiable.
"""
from __future__ import annotations

import itertools
import time
from fractions import Fraction
from typing import Any, Optional

import z3

from .models import FieldSpec, Node, SolverResult

OTHER = "__other__"
_CHECK_IDS = itertools.count(1)  # z3 EnumSort names are global per context; make each check's sorts unique


class SchemaMismatch(Exception):
    pass


class _Vars:
    def __init__(self) -> None:
        self.present: dict[str, z3.BoolRef] = {}
        self.value: dict[str, Any] = {}
        self.enum: dict[str, tuple[Any, dict[str, Any]]] = {}  # field -> (sort, key->const)
        self.domain: list[z3.BoolRef] = []


def _collect_literals(node: Optional[Node], field: str) -> set[str]:
    if node is None:
        return set()
    out: set[str] = set()
    if node.field == field and node.op in ("eq", "ne") and isinstance(node.value, str):
        out.add(node.value)
    for a in node.args:
        out |= _collect_literals(a, field)
    return out


def _build_vars(fields: dict[str, FieldSpec], nodes: list[Optional[Node]], name_prefix: str = "") -> _Vars:
    v = _Vars()
    for name, spec in fields.items():
        v.present[name] = z3.Bool(f"{name_prefix}{name}.present")
        if not spec.nullable:
            v.domain.append(v.present[name])
        if spec.type in ("currency", "number", "percent"):
            v.value[name] = z3.Real(f"{name_prefix}{name}.value")
        elif spec.type == "checkbox":
            v.value[name] = z3.Bool(f"{name_prefix}{name}.value")
            v.domain.append(v.present[name])
        else:
            keys = list(dict.fromkeys(spec.option_keys() + sorted({lit for n in nodes for lit in _collect_literals(n, name)}) + [OTHER]))
            sort, consts = z3.EnumSort(f"{name_prefix}{name}.Sort", [str(k) for k in keys])
            v.enum[name] = (sort, dict(zip([str(k) for k in keys], consts)))
            v.value[name] = z3.Const(f"{name_prefix}{name}.value", sort)
    return v


def _encode(node: Node, fields: dict[str, FieldSpec], v: _Vars, system: str) -> z3.BoolRef:
    op = node.op
    if op == "true":
        return z3.BoolVal(True)
    if op == "false":
        return z3.BoolVal(False)
    if op == "and":
        return z3.And(*[_encode(a, fields, v, system) for a in node.args])
    if op == "or":
        return z3.Or(*[_encode(a, fields, v, system) for a in node.args])
    if op == "not":
        return z3.Not(_encode(node.args[0], fields, v, system))
    assert node.field is not None
    spec = fields[node.field]
    present, value = v.present[node.field], v.value[node.field]
    if op == "is_blank":
        return z3.Not(present)
    if spec.type in ("currency", "number", "percent"):
        lit = z3.RealVal(str(node.value))
        scaled = value * z3.RealVal(str(spec.scale))
        cmp = {"eq": scaled == lit, "ne": scaled != lit, "lt": scaled < lit, "lte": scaled <= lit, "gt": scaled > lit, "gte": scaled >= lit}
        if system == "sf":
            return z3.And(present, cmp[op])
        # airtable: blank behaves as 0
        val0 = z3.If(present, scaled, z3.RealVal(0))
        cmp0 = {"eq": val0 == lit, "ne": val0 != lit, "lt": val0 < lit, "lte": val0 <= lit, "gt": val0 > lit, "gte": val0 >= lit}
        return cmp0[op]
    if spec.type == "checkbox":
        target = z3.BoolVal(bool(node.value))
        return value == target if op == "eq" else value != target
    sort, consts = v.enum[node.field]
    key = str(node.value)
    const = consts.get(key, consts[OTHER])
    eq = z3.And(present, value == const)
    return eq if op == "eq" else z3.Not(eq)


def _model_record(m: z3.ModelRef, fields: dict[str, FieldSpec], v: _Vars) -> dict[str, Any]:
    rec: dict[str, Any] = {}
    for name, spec in fields.items():
        pres = m.eval(v.present[name], model_completion=True)
        if spec.type != "checkbox" and not z3.is_true(pres):
            rec[name] = None
            continue
        val = m.eval(v.value[name], model_completion=True)
        if spec.type in ("currency", "number", "percent"):
            fr = Fraction(val.numerator_as_long(), val.denominator_as_long())
            rec[name] = float(fr) if fr.denominator != 1 else int(fr)
        elif spec.type == "checkbox":
            rec[name] = z3.is_true(val)
        else:
            rec[name] = str(val)
    return rec


def _leaves(node: Node, fields: dict[str, FieldSpec], v: _Vars, system: str, m: z3.ModelRef, out: list[dict]) -> None:
    if node.op in ("and", "or", "not"):
        for a in node.args:
            _leaves(a, fields, v, system, m, out)
        return
    if node.op in ("true", "false"):
        return
    val = z3.is_true(m.eval(_encode(node, fields, v, system), model_completion=True))
    out.append({"op": node.op, "field": node.field, "value": node.value, "result": val})


def check_equivalence(
    sf_node: Node, sf_fields: dict[str, FieldSpec],
    at_node: Node, at_fields: dict[str, FieldSpec], field_map: dict[str, str],
    timeout_ms: int = 5000,
) -> SolverResult:
    """field_map: airtable field name -> salesforce field name. Raises SchemaMismatch when the target
    schema cannot represent the source rule (unmapped field, missing select option)."""
    t0 = time.perf_counter()
    # ---- schema checks first: the solver must never claim equivalence over a schema that cannot express the rule
    for f in at_node.fields():
        if f not in field_map or field_map[f] not in sf_fields:
            raise SchemaMismatch(f"target field {f!r} is not mapped to a source field")
        if f not in at_fields:
            raise SchemaMismatch(f"target field {f!r} does not exist in the target schema")
    # keep only mapping entries whose target really exists; an LLM may emit placeholders like "(no counterpart)"
    field_map = {at: sf for at, sf in field_map.items() if at in at_fields}
    for f in sf_node.fields():
        if f not in sf_fields:
            raise SchemaMismatch(f"source field {f!r} has no metadata")
        if f not in field_map.values():
            raise SchemaMismatch(f"source field {f!r} has no target field in the Airtable schema")
    inv = {sf: at for at, sf in field_map.items()}
    for sf_name, spec in sf_fields.items():
        if sf_name not in sf_node.fields() or spec.type != "picklist":
            continue
        at_spec = at_fields[inv[sf_name]]
        missing = [k for k in spec.option_keys() if k not in at_spec.option_keys()]
        if at_spec.options and missing:
            raise SchemaMismatch(f"picklist {sf_name}: values {missing} have no option in target field {inv[sf_name]!r}")
        used = _collect_literals(sf_node, sf_name)
        bad = [u for u in used if spec.options and u not in spec.option_keys()]
        if bad:
            raise SchemaMismatch(f"rule compares {sf_name} with unknown value(s) {bad}")

    # ---- shared variables keyed by SOURCE field name; the target node is rewritten onto source names
    at_on_source = _rename(at_node, field_map)
    # target FieldSpecs keyed by source name (scale/compare_on differ per system)
    at_fields_src = {field_map[k]: v for k, v in at_fields.items() if k in field_map}
    used = sf_node.fields() | at_on_source.fields()
    merged_fields = {k: sf_fields[k] for k in sf_fields if k in used}  # only fields the rules mention
    prefix = f"c{next(_CHECK_IDS)}_"
    v = _build_vars(merged_fields, [sf_node, at_on_source], name_prefix=prefix)
    for name in at_fields_src:
        if name in merged_fields and at_fields_src[name].type != merged_fields[name].type and \
           {at_fields_src[name].type, merged_fields[name].type} - {"currency", "number", "percent"}:
            raise SchemaMismatch(f"type mismatch on {name}: source {merged_fields[name].type}, target {at_fields_src[name].type}")

    sf_expr = _encode(sf_node, merged_fields, v, "sf")
    at_expr = _encode(at_on_source, {**merged_fields, **at_fields_src}, v, "at")

    def _sat(expr: z3.BoolRef) -> bool:
        s = z3.Solver(); s.set("timeout", timeout_ms); s.add(*v.domain); s.add(expr)
        return s.check() == z3.sat

    wellformed = {
        "source": {"can_fire": _sat(sf_expr), "can_pass": _sat(z3.Not(sf_expr))},
        "target": {"can_fire": _sat(at_expr), "can_pass": _sat(z3.Not(at_expr))},
    }

    s = z3.Solver()
    s.set("timeout", timeout_ms)
    s.set(unsat_core=True)
    for i, d in enumerate(v.domain):
        s.assert_and_track(d, f"domain_{i}")
    s.assert_and_track(sf_expr != at_expr, "sides_disagree")
    sexpr = s.sexpr().replace(prefix, "")  # the per-check prefix only exists to keep z3's global sort names unique; strip it so traces are reproducible
    res = s.check()
    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    if res == z3.unsat:
        return SolverResult(status="equivalent", reason="no record exists on which the two rules disagree",
                            unsat_core=sorted(str(c) for c in s.unsat_core()), wellformed=wellformed, sexpr=sexpr,
                            z3_result="unsat", elapsed_ms=elapsed)
    if res == z3.sat:
        m = s.model()
        rec = _model_record(m, merged_fields, v)
        sf_fires = z3.is_true(m.eval(sf_expr, model_completion=True))
        at_flags = z3.is_true(m.eval(at_expr, model_completion=True))
        sf_leaves: list[dict] = []; at_leaves: list[dict] = []
        _leaves(sf_node, merged_fields, v, "sf", m, sf_leaves)
        _leaves(at_on_source, {**merged_fields, **at_fields_src}, v, "at", m, at_leaves)
        reason = (f"record {rec}: Salesforce rule {'fires' if sf_fires else 'does not fire'}, "
                  f"Airtable guard {'flags' if at_flags else 'does not flag'}")
        return SolverResult(status="not_equivalent", reason=reason, counterexample=rec, source_fires=sf_fires,
                            target_flags=at_flags, wellformed={**wellformed, "leaf_evaluation": {"source": sf_leaves, "target": at_leaves}},
                            sexpr=sexpr, z3_result="sat", elapsed_ms=elapsed)
    return SolverResult(status="error", reason=f"z3 returned {res} (timeout {timeout_ms} ms)", wellformed=wellformed,
                        sexpr=sexpr, z3_result=str(res), elapsed_ms=elapsed)


def _rename(node: Node, field_map: dict[str, str]) -> Node:
    return Node(op=node.op, field=field_map.get(node.field, node.field) if node.field else None, value=node.value,
                args=[_rename(a, field_map) for a in node.args])


# ------------------------------------------------------------------------------------ transformations (formula fields)

from .models import Expr  # noqa: E402


def _encode_expr(e: Expr, fields: dict[str, FieldSpec], v: _Vars, system: str, blanks_as: str) -> tuple[z3.BoolRef, z3.ArithRef]:
    """Returns (present, value) in the formula's own units, mirroring reference.evaluate_expr."""
    T, zero = z3.BoolVal(True), z3.RealVal(0)
    op = e.op
    if op == "num":
        return T, z3.RealVal(str(e.value))
    if op == "field":
        spec = fields[e.field]  # type: ignore[index]
        p, val = v.present[e.field], v.value[e.field] * z3.RealVal(str(spec.scale))  # type: ignore[index]
        if system == "at" or blanks_as == "BlankAsZero":
            return T, z3.If(p, val, zero)
        return p, val
    if op == "blankvalue":
        spec = fields[e.field]  # type: ignore[index]
        p, val = v.present[e.field], v.value[e.field] * z3.RealVal(str(spec.scale))  # type: ignore[index]
        return T, z3.If(p, val, z3.RealVal(str(e.value)))
    if op == "neg":
        p, a = _encode_expr(e.args[0], fields, v, system, blanks_as)
        return p, -a
    if op == "if":
        c = _encode(e.cond, fields, v, system)  # type: ignore[arg-type]
        pa, a = _encode_expr(e.args[0], fields, v, system, blanks_as)
        pb, b = _encode_expr(e.args[1], fields, v, system, blanks_as)
        return z3.If(c, pa, pb), z3.If(c, a, b)
    pa, a = _encode_expr(e.args[0], fields, v, system, blanks_as)
    pb, b = _encode_expr(e.args[1], fields, v, system, blanks_as)
    both = z3.And(pa, pb)
    if op == "add":
        return both, a + b
    if op == "sub":
        return both, a - b
    if op == "mul":
        return both, a * b
    if op == "div":
        return z3.And(both, b != zero), z3.If(b != zero, a / b, zero)
    if op == "min":
        return both, z3.If(a <= b, a, b)
    if op == "max":
        return both, z3.If(a >= b, a, b)
    raise ValueError(op)


def check_transformation(
    sf_expr: Expr, sf_fields: dict[str, FieldSpec], sf_blanks_as: str, sf_result_scale: float,
    at_expr: Expr, at_fields: dict[str, FieldSpec], field_map: dict[str, str], at_result_scale: float = 1.0,
    timeout_ms: int = 5000,
) -> SolverResult:
    """Do the two formulas compute the same canonical value (or both blank) for every record?
    result scales: a Salesforce Percent formula returns 50 for 50% (scale 100); Airtable percent returns 0.5 (1)."""
    t0 = time.perf_counter()
    field_map = {at: sf for at, sf in field_map.items() if at in at_fields}
    for f in at_expr.fields():
        if f not in field_map or field_map[f] not in sf_fields:
            raise SchemaMismatch(f"target field {f!r} is not mapped to a source field")
    for f in sf_expr.fields():
        if f not in sf_fields:
            raise SchemaMismatch(f"source field {f!r} has no metadata")
        if f not in field_map.values():
            raise SchemaMismatch(f"source field {f!r} has no target field in the Airtable schema")
    at_on_src = _rename_expr(at_expr, field_map)
    at_fields_src = {field_map[k]: s for k, s in at_fields.items() if k in field_map}
    used = sf_expr.fields() | at_on_src.fields()
    merged = {k: sf_fields[k] for k in sf_fields if k in used}
    prefix = f"c{next(_CHECK_IDS)}_"
    v = _build_vars(merged, [sf_expr.cond, at_on_src.cond], name_prefix=prefix)
    sp, sv = _encode_expr(sf_expr, merged, v, "sf", sf_blanks_as)
    ap, av = _encode_expr(at_on_src, {**merged, **at_fields_src}, v, "at", "BlankAsZero")
    sv_c, av_c = sv / z3.RealVal(str(sf_result_scale)), av / z3.RealVal(str(at_result_scale))
    s = z3.Solver()
    s.set("timeout", timeout_ms)
    s.set(unsat_core=True)
    for i, d in enumerate(v.domain):
        s.assert_and_track(d, f"domain_{i}")
    s.assert_and_track(z3.Or(sp != ap, z3.And(sp, ap, sv_c != av_c)), "outputs_disagree")
    sexpr = s.sexpr().replace(prefix, "")
    res = s.check()
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    if res == z3.unsat:
        return SolverResult(status="equivalent", reason="no record exists on which the two formulas compute different values",
                            unsat_core=sorted(str(c) for c in s.unsat_core()), sexpr=sexpr, z3_result="unsat", elapsed_ms=elapsed)
    if res == z3.sat:
        m = s.model()
        rec = _model_record(m, merged, v)

        def out(p, val):
            if not z3.is_true(m.eval(p, model_completion=True)):
                return None
            x = m.eval(val, model_completion=True)
            fr = Fraction(x.numerator_as_long(), x.denominator_as_long())
            return float(fr) if fr.denominator != 1 else int(fr)
        outputs = {"source": out(sp, sv_c), "target": out(ap, av_c)}
        return SolverResult(status="not_equivalent", reason=f"record {rec}: source computes {outputs['source']}, target computes {outputs['target']}",
                            counterexample=rec, outputs=outputs, sexpr=sexpr, z3_result="sat", elapsed_ms=elapsed)
    return SolverResult(status="error", reason=f"z3 returned {res}", sexpr=sexpr, z3_result=str(res), elapsed_ms=elapsed)


def _rename_expr(e: Expr, field_map: dict[str, str]) -> Expr:
    return Expr(op=e.op, value=e.value, field=field_map.get(e.field, e.field) if e.field else None,
                args=[_rename_expr(a, field_map) for a in e.args], cond=_rename(e.cond, field_map) if e.cond else None)
