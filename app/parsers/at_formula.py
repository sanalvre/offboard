"""Airtable guard-formula subset -> Node IR.

Accepted guard shapes:
  IF(<cond>, "VIOLATION", "")      IF(<cond>, 1, 0)      or a bare <cond>
Supported inside <cond>: AND/OR/NOT, {Field} = "v", comparisons = != < <= > >=, {Field} = BLANK(),
{Field} != BLANK(), numeric and string literals, TRUE()/FALSE(), parentheses.
Everything else is reported in `unsupported`.

Semantics (blank is 0 in numeric comparisons, etc.) live in the solver and reference interpreter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import FieldSpec, Node
from .common import ParseError, TokenStream, tokenize, unquote

_CMP = {"=": "eq", "!=": "ne", "<>": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
_CMP_FLIP = {"eq": "eq", "ne": "ne", "lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}


@dataclass
class _Val:
    node: Optional[Node] = None
    field: Optional[str] = None
    literal: Any = None
    is_literal: bool = False
    is_blank_call: bool = False


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

    def parse(self) -> Node:
        v = self.expr()
        if self.ts.peek().kind != "eof":
            raise ParseError(f"unexpected token {self.ts.peek().text!r} at {self.ts.peek().pos}")
        return self._as_bool(v)

    def expr(self) -> _Val:
        left = self.primary()
        t = self.ts.peek()
        if t.kind == "op" and t.text in _CMP:
            self.ts.next()
            right = self.primary()
            return self._compare(left, _CMP[t.text], right)
        if t.kind == "op":
            self._unsupported(f"operator:{t.text}")
            self.ts.next()
            self.primary()
            return _Val(node=Node(op="false"))
        return left

    def primary(self) -> _Val:
        t = self.ts.next()
        if t.kind == "lparen":
            v = self.expr()
            self.ts.expect("rparen")
            return v
        if t.kind == "field":
            name = t.text[1:-1]
            if name not in self.fields:
                self._unsupported(f"field:{name}")
            return _Val(field=name)
        if t.kind == "number":
            return _Val(literal=float(t.text) if "." in t.text else int(t.text), is_literal=True)
        if t.kind == "string":
            return _Val(literal=unquote(t.text), is_literal=True)
        if t.kind == "op" and t.text == "-" and self.ts.peek().kind == "number":
            n = self.ts.next()
            return _Val(literal=-(float(n.text) if "." in n.text else int(n.text)), is_literal=True)
        if t.kind == "ident":
            return self.func(t.text.upper(), t.text)
        raise ParseError(f"unexpected token {t.text!r} at {t.pos}")

    def args(self) -> list[_Val]:
        self.ts.expect("lparen")
        out: list[_Val] = []
        if not self.ts.accept("rparen"):
            out.append(self.expr())
            while self.ts.accept("comma"):
                out.append(self.expr())
            self.ts.expect("rparen")
        return out

    def func(self, up: str, raw: str) -> _Val:
        a = self.args()
        if up == "AND":
            return _Val(node=Node(op="and", args=[self._as_bool(x) for x in a]))
        if up == "OR":
            return _Val(node=Node(op="or", args=[self._as_bool(x) for x in a]))
        if up == "NOT":
            if len(a) != 1:
                raise ParseError("NOT takes one argument")
            return _Val(node=Node(op="not", args=[self._as_bool(a[0])]))
        if up == "BLANK":
            if a:
                raise ParseError("BLANK() takes no arguments")
            return _Val(is_blank_call=True)
        if up == "TRUE":
            return _Val(node=Node(op="true"), literal=True, is_literal=True)
        if up == "FALSE":
            return _Val(node=Node(op="false"), literal=False, is_literal=True)
        if up == "IF":
            # guard shape: IF(cond, truthy, falsy) -> cond
            if len(a) not in (2, 3):
                raise ParseError("IF takes 2 or 3 arguments")
            cond, then = a[0], a[1]
            else_ = a[2] if len(a) == 3 else _Val(literal="", is_literal=True)
            if not (then.is_literal and else_.is_literal and _truthy(then.literal) and not _truthy(else_.literal)):
                self._unsupported("IF:non_guard_shape")
                return _Val(node=Node(op="false"))
            return _Val(node=self._as_bool(cond))
        self._unsupported(raw.upper())
        return _Val(node=Node(op="false"))

    def _as_bool(self, v: _Val) -> Node:
        if v.node is not None:
            return v.node
        if v.field is not None:
            spec = self.fields.get(v.field)
            if spec and spec.type == "checkbox":
                return Node(op="eq", field=v.field, value=True)
            raise ParseError(f"field {v.field} used where a boolean was expected")
        if v.is_literal:
            return Node(op="true" if _truthy(v.literal) else "false")
        raise ParseError("expected a boolean expression")

    def _compare(self, left: _Val, op: str, right: _Val) -> _Val:
        if (left.is_literal or left.is_blank_call) and right.field is not None:
            left, right, op = right, left, _CMP_FLIP[op]
        if left.field is not None and right.field is not None:
            self._unsupported("field_to_field_comparison")
            return _Val(node=Node(op="false"))
        if left.field is None:
            if left.is_literal and right.is_literal:
                return _Val(node=Node(op="true" if _const(left.literal, op, right.literal) else "false"))
            raise ParseError("comparison must involve a field")
        if right.is_blank_call:
            if op == "eq":
                return _Val(node=Node(op="is_blank", field=left.field))
            if op == "ne":
                return _Val(node=Node(op="not", args=[Node(op="is_blank", field=left.field)]))
            raise ParseError("BLANK() only supports = and !=")
        if not right.is_literal:
            raise ParseError("comparison must be between a field and a literal")
        return _Val(node=Node(op=op, field=left.field, value=right.literal))


def _truthy(x: Any) -> bool:
    return bool(x) and x != "" and x != 0


def _const(a: Any, op: str, b: Any) -> bool:
    try:
        return {"eq": a == b, "ne": a != b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    except TypeError:
        return False


def parse_at_formula(formula: str, fields: dict[str, FieldSpec]) -> ParseOutcome:
    p = _Parser(formula, fields)
    try:
        node = p.parse()
    except ParseError as e:
        return ParseOutcome(node=None, unsupported=p.unsupported, error=str(e))
    if p.unsupported:
        return ParseOutcome(node=None, unsupported=p.unsupported)
    return ParseOutcome(node=node)
