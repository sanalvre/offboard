"""Reference interpreter: evaluates a Node on a concrete record under Salesforce or Airtable semantics.

This is the independent oracle the solver is tested against. It is deliberately plain Python with no Z3,
so an encoding mistake in the solver cannot also be present here.

Record representation: {field_name: value}; a missing key or None means blank. Numeric values are in
CANONICAL units (the real-world quantity: 0.5 means 50%, 1000 means $1000). Each FieldSpec.scale says
how the system's formula literal relates to canonical units (Salesforce percent: literal 50 == 0.5).

Semantics, with their evidence (see skills/plan.md 1.2, 1.3 and skills/problem-domain.md 3.3):
  Salesforce validation rule formulas
    - numeric comparison on a blank field is FALSE (blank is null; verified in the Dev org 2026-09-13)
    - ISPICKVAL / TEXT(f) = "v" on a blank picklist is FALSE; <> is the negation of =
    - ISBLANK(f) is TRUE iff the field is blank
  Airtable formulas
    - a blank number is 0 in numeric comparisons (verified in the real base 2026-09-13)
    - {Select} = "v" on a blank select is FALSE ("" = "v"); != is the negation
    - {f} = BLANK() is TRUE iff the field is blank
"""
from __future__ import annotations

from typing import Any, Literal, Mapping

from .models import FieldSpec, Node

System = Literal["sf", "at"]


def _cmp(a: Any, op: str, b: Any) -> bool:
    return {"eq": a == b, "ne": a != b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]


def evaluate(node: Node, record: Mapping[str, Any], fields: Mapping[str, FieldSpec], system: System) -> bool:
    op = node.op
    if op == "true":
        return True
    if op == "false":
        return False
    if op == "and":
        return all(evaluate(a, record, fields, system) for a in node.args)
    if op == "or":
        return any(evaluate(a, record, fields, system) for a in node.args)
    if op == "not":
        return not evaluate(node.args[0], record, fields, system)

    assert node.field is not None
    spec = fields[node.field]
    raw = record.get(node.field)
    blank = raw is None or raw == ""

    if op == "is_blank":
        return blank

    if spec.type in ("currency", "number", "percent"):
        lit = node.value
        if system == "sf":
            if blank:
                return False
            return _cmp(raw * spec.scale, op, lit)
        # airtable: blank behaves as 0
        val = 0 if blank else raw * spec.scale
        return _cmp(val, op, lit)

    if spec.type == "checkbox":
        val = bool(raw) if not blank else False
        return _cmp(val, op, bool(node.value))

    # picklist / text: string equality only
    if op not in ("eq", "ne"):
        raise ValueError(f"{op} not defined for {spec.type}")
    if blank:
        eq = False
    else:
        eq = str(raw) == str(node.value)
    return eq if op == "eq" else not eq


# --------------------------------------------------------------------------- numeric expressions (formula fields)

from typing import Optional  # noqa: E402

from .models import Expr  # noqa: E402


def evaluate_expr(expr: Expr, record: Mapping[str, Any], fields: Mapping[str, FieldSpec], system: System,
                  blanks_as: str = "BlankAsBlank") -> Optional[float]:
    """Value of a numeric formula in the formula's own units (Salesforce percent operands appear as 50 for 50%,
    Airtable as 0.5). Returns None for a blank result.

    Salesforce (formula field): with BlankAsBlank any arithmetic on a blank operand yields blank, unless guarded by
    BLANKVALUE; with BlankAsZero blank operands are 0. Division by zero yields blank (Salesforce shows #Error).
    Airtable: blank operands are 0; division by zero yields blank (Airtable shows an error, no value).
    IF conditions use the boolean semantics of `evaluate`.
    """
    def ev(e: Expr) -> Optional[float]:
        op = e.op
        if op == "num":
            return float(e.value)  # type: ignore[arg-type]
        if op == "blank":
            return None
        if op == "field":
            raw = record.get(e.field)  # type: ignore[arg-type]
            spec = fields[e.field]  # type: ignore[index]
            if raw is None or raw == "":
                return 0.0 if (system == "at" or blanks_as == "BlankAsZero") else None
            return raw * spec.scale
        if op == "blankvalue":
            raw = record.get(e.field)  # type: ignore[arg-type]
            spec = fields[e.field]  # type: ignore[index]
            return float(e.value) if raw is None or raw == "" else raw * spec.scale  # type: ignore[arg-type]
        if op == "neg":
            a = ev(e.args[0])
            return None if a is None else -a
        if op == "if":
            return ev(e.args[0]) if evaluate(e.cond, record, fields, system) else ev(e.args[1])  # type: ignore[arg-type]
        a, b = ev(e.args[0]), ev(e.args[1])
        if a is None or b is None:
            return None
        if op == "add":
            return a + b
        if op == "sub":
            return a - b
        if op == "mul":
            return a * b
        if op == "div":
            return None if b == 0 else a / b
        if op == "min":
            return min(a, b)
        if op == "max":
            return max(a, b)
        raise ValueError(op)
    return ev(expr)
