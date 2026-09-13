"""Shared tokenizer for the Salesforce and Airtable formula subsets."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Tok:
    kind: str  # ident | number | string | field | op | lparen | rparen | comma | eof
    text: str
    pos: int


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<field>\{[^}]*\})                       # Airtable {Field Name}
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<number>\d+(?:\.\d+)?|\.\d+)
  | (?P<ident>[A-Za-z_$][A-Za-z0-9_.]*)
  | (?P<op>&&|\|\||<>|!=|<=|>=|==|=|<|>|!|&|\+|-|\*|/)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
    """,
    re.VERBOSE,
)


def tokenize(src: str) -> list[Tok]:
    out: list[Tok] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise ParseError(f"unexpected character {src[pos]!r} at {pos}")
        kind = m.lastgroup
        assert kind is not None
        if kind != "ws":
            out.append(Tok(kind, m.group(), pos))
        pos = m.end()
    out.append(Tok("eof", "", pos))
    return out


def unquote(s: str) -> str:
    q = s[0]
    body = s[1:-1]
    return body.replace("\\" + q, q).replace("\\\\", "\\")


class TokenStream:
    def __init__(self, toks: list[Tok]):
        self.toks = toks
        self.i = 0

    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind: str, text: str | None = None) -> Tok:
        t = self.next()
        if t.kind != kind or (text is not None and t.text != text):
            raise ParseError(f"expected {text or kind} at {t.pos}, got {t.text!r}")
        return t

    def accept(self, kind: str, text: str | None = None) -> Tok | None:
        t = self.peek()
        if t.kind == kind and (text is None or t.text == text):
            return self.next()
        return None

    def __iter__(self) -> Iterator[Tok]:
        return iter(self.toks[self.i:])
