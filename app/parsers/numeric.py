"""Numeric formula subset -> Expr IR, for Salesforce formula fields and Airtable formula fields.

Grammar (both systems): additive ( +,- ) over multiplicative ( *,/ ) over unary minus over primary, where primary is
a number, a field (Salesforce identifier or Airtable {Field}), a parenthesised expression, or a function:
  IF(<boolean>, expr, expr)                 boolean parsed by the existing condition parser of that system
  BLANKVALUE(field, number) / NULLVALUE     Salesforce null guard
  IF({F} = BLANK(), number, {F})            Airtable idiom for the same guard, recognised and lowered to blankvalue
  MIN(a, b) / MAX(a, b)
Anything else is reported in `unsupported`. Boolean-returning formulas (checkbox) go through the condition parsers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import Expr, FieldSpec, Node
from . import at_formula, sf_formula
from .common import ParseError, TokenStream, tokenize, unquote


@dataclass
class ExprOutcome:
    expr: Optional[Expr]
    unsupported: list[str] = field(default_factory=list)
    error: Optional[str] = None


class _NumParser:
    def __init__(self, src: str, fields: dict[str, FieldSpec], system: str):
        self.src = src
        self.ts = TokenStream(tokenize(src))
        self.fields = fields
        self.system = system
        self.unsupported: list[str] = []

    def _uns(self, n: str) -> None:
        if n not in self.unsupported:
            self.unsupported.append(n)

    def parse(self) -> Expr:
        e = self.additive()
        if self.ts.peek().kind != "eof":
            raise ParseError(f"unexpected token {self.ts.peek().text!r} at {self.ts.peek().pos}")
        return e

    def additive(self) -> Expr:
        e = self.multiplicative()
        while self.ts.peek().kind == "op" and self.ts.peek().text in ("+", "-"):
            op = self.ts.next().text
            e = Expr(op="add" if op == "+" else "sub", args=[e, self.multiplicative()])
        return e

    def multiplicative(self) -> Expr:
        e = self.unary()
        while self.ts.peek().kind == "op" and self.ts.peek().text in ("*", "/"):
            op = self.ts.next().text
            e = Expr(op="mul" if op == "*" else "div", args=[e, self.unary()])
        return e

    def unary(self) -> Expr:
        if self.ts.accept("op", "-"):
            return Expr(op="neg", args=[self.unary()])
        return self.primary()

    def primary(self) -> Expr:
        t = self.ts.next()
        if t.kind == "lparen":
            e = self.additive()
            self.ts.expect("rparen")
            return e
        if t.kind == "number":
            return Expr(op="num", value=float(t.text))
        if t.kind == "field":  # airtable {Field}
            name = t.text[1:-1]
            return self._field(name)
        if t.kind == "ident":
            up = t.text.upper()
            if self.ts.peek().kind == "lparen":
                return self.func(up, t.text)
            if self.system == "sf":
                return self._field(t.text)
            raise ParseError(f"unexpected identifier {t.text!r} at {t.pos}")
        raise ParseError(f"unexpected token {t.text!r} at {t.pos}")

    def _field(self, name: str) -> Expr:
        spec = self.fields.get(name)
        if spec is None:
            self._uns(f"field:{name}")
        elif spec.type not in ("currency", "number", "percent"):
            self._uns(f"non_numeric_field:{name}")
        return Expr(op="field", field=name)

    def _slice_args(self) -> list[str]:
        """Return the raw source text of each argument of the call whose '(' is the next token (for boolean sub-parsing)."""
        assert self.ts.peek().kind == "lparen"
        start = self.ts.next().pos + 1
        depth, i, parts = 1, self.ts.i, []
        seg_start = start
        while depth > 0:
            tok = self.ts.toks[i]
            if tok.kind == "eof":
                raise ParseError("unbalanced parentheses")
            if tok.kind == "lparen":
                depth += 1
            elif tok.kind == "rparen":
                depth -= 1
                if depth == 0:
                    parts.append(self.src[seg_start:tok.pos])
            elif tok.kind == "comma" and depth == 1:
                parts.append(self.src[seg_start:tok.pos])
                seg_start = tok.pos + 1
            i += 1
        self.ts.i = i
        return [p.strip() for p in parts]

    def func(self, up: str, raw: str) -> Expr:
        if up == "IF":
            cond_src, a_src, b_src = self._slice_args() if True else ("", "", "")  # noqa: SIM108
            cond = self._bool(cond_src)
            a, b = self._sub(a_src), self._sub(b_src)
            # Airtable idiom IF({F} = BLANK(), d, {F}) -> blankvalue
            if (cond is not None and cond.op == "is_blank" and a.op == "num" and b.op == "field" and b.field == cond.field):
                return Expr(op="blankvalue", field=b.field, value=a.value)
            if cond is None:
                return Expr(op="num", value=0.0)
            return Expr(op="if", cond=cond, args=[a, b])
        if up == "BLANK" and self.system == "at":
            parts = self._slice_args()
            if parts and parts != [""]:
                raise ParseError("BLANK() takes no arguments")
            return Expr(op="blank")
        if up in ("BLANKVALUE", "NULLVALUE"):
            parts = self._slice_args()
            if len(parts) != 2:
                raise ParseError(f"{up} takes 2 arguments")
            f = self._sub(parts[0])
            d = self._sub(parts[1])
            if f.op != "field" or d.op != "num":
                raise ParseError(f"{up}(field, number) expected")
            return Expr(op="blankvalue", field=f.field, value=d.value)
        if up in ("MIN", "MAX"):
            parts = self._slice_args()
            if len(parts) != 2:
                raise ParseError(f"{up} takes 2 arguments in this subset")
            return Expr(op=up.lower(), args=[self._sub(parts[0]), self._sub(parts[1])])
        self._slice_args()
        self._uns(up if self.system == "sf" else raw.upper())
        return Expr(op="num", value=0.0)

    def _sub(self, src: str) -> Expr:
        p = _NumParser(src, self.fields, self.system)
        e = p.parse()
        for u in p.unsupported:
            self._uns(u)
        return e

    def _bool(self, src: str) -> Optional[Node]:
        out = sf_formula.parse_sf_formula(src, self.fields) if self.system == "sf" else at_formula.parse_at_formula(src, self.fields)
        if out.node is None:
            for u in out.unsupported or [f"condition:{out.error}"]:
                self._uns(u)
        return out.node


def parse_numeric(formula: str, fields: dict[str, FieldSpec], system: str) -> ExprOutcome:
    p = _NumParser(formula, fields, system)
    try:
        e = p.parse()
    except ParseError as ex:
        return ExprOutcome(None, p.unsupported, str(ex))
    if p.unsupported:
        return ExprOutcome(None, p.unsupported)
    return ExprOutcome(e)
