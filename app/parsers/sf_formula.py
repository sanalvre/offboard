"""Salesforce validation-rule formula subset -> Node IR.

Supported: AND/OR/NOT, && || !, ISPICKVAL(field,"v"), ISBLANK/ISNULL(field), TEXT(field) = "v",
BLANKVALUE(field, default) in comparisons, NULLVALUE alias, comparisons = == <> != < <= > >=,
numeric / string / TRUE / FALSE literals, bare checkbox fields, parentheses.

Anything else is recorded in `unsupported` (by construct name) and the rule is not encodable.
Field-to-field comparisons are unsupported on purpose: they need a different encoding and are rare in
validation rules.

Semantics live in the solver and reference interpreter, not here. This module only builds structure.
The one semantic rewrite here is BLANKVALUE(f, d) op v, which becomes a null-safe expression:
  if (d op v) is true:  OR(ISBLANK(f), f op v)      else: AND(NOT ISBLANK(f), f op v)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Dependency, FieldSpec, Node, RuleIR, RuleSource
from .common import ParseError, TokenStream, tokenize, unquote

_CMP = {"=": "eq", "==": "eq", "<>": "ne", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
_CMP_FLIP = {"eq": "eq", "ne": "ne", "lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}
_KNOWN_UNSUPPORTED = {
    "PRIORVALUE", "ISCHANGED", "ISNEW", "TODAY", "NOW", "DATE", "DATEVALUE", "DATETIMEVALUE", "YEAR", "MONTH", "DAY",
    "VLOOKUP", "REGEX", "CONTAINS", "BEGINS", "INCLUDES", "LEN", "LEFT", "RIGHT", "MID", "UPPER", "LOWER", "TRIM",
    "CASE", "IF", "VALUE", "ROUND", "FLOOR", "CEILING", "ABS", "MAX", "MIN", "MOD", "CURRENCYRATE", "OWNER", "IMAGE",
    "HYPERLINK", "FIND", "SUBSTITUTE", "BR", "ISNUMBER", "GETRECORDIDS", "CASESAFEID", "DISTANCE", "GEOLOCATION",
}


@dataclass
class _Val:
    """Intermediate value during parsing: a boolean Node, a field reference, or a literal."""

    node: Optional[Node] = None
    field: Optional[str] = None
    literal: Any = None
    is_literal: bool = False
    blankvalue_default: Any = None  # set when this is BLANKVALUE(field, default)
    text_of: bool = False  # TEXT(field)


@dataclass
class ParseOutcome:
    node: Optional[Node]
    unsupported: list[str] = field(default_factory=list)
    error: Optional[str] = None


class _Parser:
    def __init__(self, src: str, fields: dict[str, FieldSpec]):
        self.ts = TokenStream(tokenize(src))
        self.fields = fields
        self.unsupported: list[str] = []

    def _unsupported(self, name: str) -> None:
        if name not in self.unsupported:
            self.unsupported.append(name)

    # grammar --------------------------------------------------------------
    def parse(self) -> Node:
        n = self.or_expr()
        if self.ts.peek().kind != "eof":
            raise ParseError(f"unexpected token {self.ts.peek().text!r} at {self.ts.peek().pos}")
        return self._as_bool(n)

    def or_expr(self) -> _Val:
        parts = [self.and_expr()]
        while self.ts.accept("op", "||"):
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else _Val(node=Node(op="or", args=[self._as_bool(p) for p in parts]))

    def and_expr(self) -> _Val:
        parts = [self.not_expr()]
        while self.ts.accept("op", "&&"):
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else _Val(node=Node(op="and", args=[self._as_bool(p) for p in parts]))

    def not_expr(self) -> _Val:
        if self.ts.accept("op", "!"):
            return _Val(node=Node(op="not", args=[self._as_bool(self.not_expr())]))
        return self.comparison()

    def comparison(self) -> _Val:
        left = self.primary()
        t = self.ts.peek()
        if t.kind == "op" and t.text in _CMP:
            self.ts.next()
            right = self.primary()
            return self._compare(left, _CMP[t.text], right)
        return left

    def primary(self) -> _Val:
        t = self.ts.next()
        if t.kind == "lparen":
            v = self.or_expr()
            self.ts.expect("rparen")
            return v
        if t.kind == "number":
            return _Val(literal=float(t.text) if "." in t.text else int(t.text), is_literal=True)
        if t.kind == "string":
            return _Val(literal=unquote(t.text), is_literal=True)
        if t.kind == "ident":
            up = t.text.upper()
            if self.ts.peek().kind == "lparen":
                return self.func(up, t.text)
            if up == "TRUE":
                return _Val(node=Node(op="true"), literal=True, is_literal=True)
            if up == "FALSE":
                return _Val(node=Node(op="false"), literal=False, is_literal=True)
            if t.text.startswith("$"):
                self._unsupported(t.text.split(".")[0])
                return _Val(node=Node(op="false"))
            return self._field(t.text)
        if t.kind == "op" and t.text == "-" and self.ts.peek().kind == "number":
            n = self.ts.next()
            return _Val(literal=-(float(n.text) if "." in n.text else int(n.text)), is_literal=True)
        raise ParseError(f"unexpected token {t.text!r} at {t.pos}")

    def args(self) -> list[_Val]:
        self.ts.expect("lparen")
        out: list[_Val] = []
        if not self.ts.accept("rparen"):
            out.append(self.or_expr())
            while self.ts.accept("comma"):
                out.append(self.or_expr())
            self.ts.expect("rparen")
        return out

    def func(self, up: str, raw: str) -> _Val:
        a = self.args()
        if up == "AND":
            return _Val(node=Node(op="and", args=[self._as_bool(x) for x in a]))
        if up == "OR":
            return _Val(node=Node(op="or", args=[self._as_bool(x) for x in a]))
        if up == "NOT":
            self._arity(up, a, 1)
            return _Val(node=Node(op="not", args=[self._as_bool(a[0])]))
        if up == "ISPICKVAL":
            self._arity(up, a, 2)
            f, lit = a[0], a[1]
            if f.field is None or not lit.is_literal:
                raise ParseError("ISPICKVAL(field, \"value\") expected")
            self._check_option(f.field, lit.literal)
            return _Val(node=Node(op="eq", field=f.field, value=lit.literal))
        if up in ("ISBLANK", "ISNULL"):
            self._arity(up, a, 1)
            if a[0].field is None:
                raise ParseError(f"{up}(field) expected")
            return _Val(node=Node(op="is_blank", field=a[0].field))
        if up == "TEXT":
            self._arity(up, a, 1)
            if a[0].field is None:
                raise ParseError("TEXT(field) expected")
            return _Val(field=a[0].field, text_of=True)
        if up in ("BLANKVALUE", "NULLVALUE"):
            self._arity(up, a, 2)
            if a[0].field is None or not a[1].is_literal:
                raise ParseError(f"{up}(field, literal) expected")
            return _Val(field=a[0].field, blankvalue_default=a[1].literal)
        self._unsupported(up if up in _KNOWN_UNSUPPORTED else raw)
        return _Val(node=Node(op="false"))

    # helpers --------------------------------------------------------------
    def _arity(self, name: str, a: list[_Val], n: int) -> None:
        if len(a) != n:
            raise ParseError(f"{name} takes {n} argument(s), got {len(a)}")

    def _field(self, name: str) -> _Val:
        if name not in self.fields:
            self._unsupported(f"field:{name}")
            return _Val(field=name)
        return _Val(field=name)

    def _check_option(self, fname: str, value: Any) -> None:
        spec = self.fields.get(fname)
        if spec and spec.type == "picklist" and spec.options and value not in spec.option_keys():
            self._unsupported(f"option:{fname}={value}")

    def _as_bool(self, v: _Val) -> Node:
        if v.node is not None:
            return v.node
        if v.field is not None and not v.text_of and v.blankvalue_default is None:
            spec = self.fields.get(v.field)
            if spec and spec.type == "checkbox":
                return Node(op="eq", field=v.field, value=True)
            raise ParseError(f"field {v.field} used where a boolean was expected")
        if v.is_literal:
            return Node(op="true" if v.literal else "false")
        raise ParseError("expected a boolean expression")

    def _compare(self, left: _Val, op: str, right: _Val) -> _Val:
        # normalise to field OP literal
        if left.is_literal and right.field is not None:
            left, right, op = right, left, _CMP_FLIP[op]
        if left.field is not None and right.field is not None:
            self._unsupported("field_to_field_comparison")
            return _Val(node=Node(op="false"))
        if left.is_literal and right.is_literal:
            return _Val(node=Node(op="true" if _const(left.literal, op, right.literal) else "false"))
        if left.field is None or not right.is_literal:
            raise ParseError("comparison must be between a field and a literal")
        f, v = left.field, right.literal
        if left.text_of:
            self._check_option(f, v)
        if left.blankvalue_default is not None:
            leaf = Node(op=op, field=f, value=v)
            blank = Node(op="is_blank", field=f)
            if _const(left.blankvalue_default, op, v):
                return _Val(node=Node(op="or", args=[blank, leaf]))
            return _Val(node=Node(op="and", args=[Node(op="not", args=[blank]), leaf]))
        return _Val(node=Node(op=op, field=f, value=v))


def _const(a: Any, op: str, b: Any) -> bool:
    try:
        return {"eq": a == b, "ne": a != b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    except TypeError:
        return False


def parse_sf_formula(formula: str, fields: dict[str, FieldSpec]) -> ParseOutcome:
    p = _Parser(formula, fields)
    try:
        node = p.parse()
    except ParseError as e:
        return ParseOutcome(node=None, unsupported=p.unsupported, error=str(e))
    if p.unsupported:
        return ParseOutcome(node=None, unsupported=p.unsupported)
    return ParseOutcome(node=node)


def infer_dependency(node: Optional[Node], fields: dict[str, FieldSpec]) -> Dependency:
    """Heuristic: the picklist that gates the rule is the trigger; the other field is constrained."""
    if node is None:
        return Dependency()
    used = [f for f in node.fields() if f in fields]
    picks = [f for f in used if fields[f].type == "picklist"]
    others = [f for f in used if fields[f].type != "picklist"]
    if picks and others:
        return Dependency(trigger_field=picks[0], constrained_field=others[0])
    if len(used) == 1:
        return Dependency(constrained_field=used[0])
    return Dependency()


def build_rule_ir(source: RuleSource, fields: dict[str, FieldSpec]) -> RuleIR:
    out = parse_sf_formula(source.formula, fields)
    return RuleIR(
        source=source,
        fields={k: v for k, v in fields.items()},
        condition=out.node,
        dependency=infer_dependency(out.node, fields),
        unsupported=out.unsupported,
        parse_error=out.error,
    )
