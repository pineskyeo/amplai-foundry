"""A query language for catalogue items: ``tag:gift price<20 -name:"blue mug" OR sku:TEA-*``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ParseError
from .models import Item

FIELDS = ("sku", "name", "tag", "price", "reorder")
_NUMERIC = ("price", "reorder")
_SPECIAL = ":<>"
_PRICE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_WHOLE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class Term:
    field: str | None
    op: str
    value: str


@dataclass(frozen=True)
class Not:
    child: Node


@dataclass(frozen=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    children: tuple[Node, ...]


Node = Term | Not | And | Or


class QueryError(ParseError):
    def __init__(self, column: int, reason: str) -> None:
        super().__init__(f"column {column}: {reason}")
        self.column = column


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def skip(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def word_at(self, i: int) -> str:
        j = i
        while j < len(self.text) and not (self.text[j].isspace() or self.text[j] in '()"'):
            j += 1
        return self.text[i:j]

    def at_or(self) -> bool:
        return self.word_at(self.i) == "OR"

    def parse(self) -> Node:
        self.skip()
        if self.i >= len(self.text):
            return And(())
        node = self.parse_or()
        self.skip()
        if self.i < len(self.text):
            raise QueryError(self.i + 1, "unexpected )")
        return node

    def parse_or(self) -> Node:
        parts = [self.parse_and()]
        while True:
            self.skip()
            if self.peek() and self.at_or():
                self.i += 2
                parts.append(self.parse_and())
            else:
                break
        if len(parts) == 1:
            return parts[0]
        flat: list[Node] = []
        for part in parts:
            flat.extend(part.children if isinstance(part, Or) else (part,))
        return Or(tuple(flat))

    def parse_and(self) -> Node:
        parts = [self.parse_unary()]
        while True:
            self.skip()
            if self.peek() in ("", ")") or self.at_or():
                break
            parts.append(self.parse_unary())
        if len(parts) == 1:
            return parts[0]
        flat: list[Node] = []
        for part in parts:
            flat.extend(part.children if isinstance(part, And) else (part,))
        return And(tuple(flat))

    def parse_unary(self) -> Node:
        self.skip()
        if self.peek() == "-":
            column = self.i + 1
            following = self.text[self.i + 1 : self.i + 2]
            if following == "" or following.isspace() or following in ")-":
                raise QueryError(column, "nothing to negate")
            self.i += 1
            return Not(self.parse_primary())
        return self.parse_primary()

    def quoted(self) -> str:
        start = self.i
        self.i += 1
        parts: list[str] = []
        while True:
            if self.i >= len(self.text):
                raise QueryError(start + 1, "unterminated quote")
            char = self.text[self.i]
            if char == '"':
                if self.text.startswith('""', self.i):
                    parts.append('"')
                    self.i += 2
                    continue
                self.i += 1
                break
            parts.append(char)
            self.i += 1
        following = self.peek()
        if following and not (following.isspace() or following == ")"):
            raise QueryError(self.i + 1, "unexpected character after quote")
        return "".join(parts)

    def parse_primary(self) -> Node:
        char = self.peek()
        if char == "" or char == ")":
            raise QueryError(self.i + 1, "operand expected")
        if char == "(":
            self.i += 1
            self.skip()
            node = self.parse_or()
            self.skip()
            if self.peek() != ")":
                raise QueryError(len(self.text) + 1, "missing )")
            self.i += 1
            return node
        if char == '"':
            return Term(None, ":", self.quoted())
        start = self.i
        word = self.word_at(start)
        if word == "OR":
            raise QueryError(start + 1, "operand expected")
        self.i = start + len(word)
        cut = next((k for k, c in enumerate(word) if c in _SPECIAL), None)
        if cut is None:
            self.no_quote_after_word()
            return Term(None, ":", word)
        field = word[:cut]
        if field not in FIELDS:
            raise QueryError(start + 1, f"unknown field {field!r}")
        op = word[cut]
        if op in "<>" and word[cut + 1 : cut + 2] == "=":
            op += "="
        if op != ":" and field not in _NUMERIC:
            raise QueryError(start + cut + 1, f"{field} takes only ':'")
        value_start = start + cut + len(op)
        value = word[cut + len(op) :]
        for k, c in enumerate(value):
            if c in _SPECIAL:
                raise QueryError(value_start + k + 1, f"unexpected {c!r}")
        if value == "":
            if self.peek() != '"':
                raise QueryError(value_start + 1, "missing value")
            value = self.quoted()
        else:
            self.no_quote_after_word()
        if field == "price" and not _PRICE.fullmatch(value):
            raise QueryError(value_start + 1, f"not an amount: {value!r}")
        if field == "reorder" and not _WHOLE.fullmatch(value):
            raise QueryError(value_start + 1, f"not a whole number: {value!r}")
        return Term(field, op, value)

    def no_quote_after_word(self) -> None:
        if self.peek() == '"':
            raise QueryError(self.i + 1, "unexpected quote")


def parse_query(text: str) -> Node:
    return _Parser(text).parse()


def _value(value: str) -> str:
    if (
        value
        and value != "OR"
        and not value.startswith("-")
        and not any(c.isspace() or c in '"()<>:' for c in value)
    ):
        return value
    return '"' + value.replace('"', '""') + '"'


def format_query(node: Node) -> str:
    if isinstance(node, Term):
        prefix = "" if node.field is None else node.field + node.op
        return prefix + _value(node.value)
    if isinstance(node, Not):
        inner = format_query(node.child)
        return "-" + (inner if isinstance(node.child, Term) else f"({inner})")
    if isinstance(node, And):
        return " ".join(
            f"({format_query(c)})" if isinstance(c, (Or, And)) else format_query(c)
            for c in node.children
        )
    return " OR ".join(
        f"({format_query(c)})" if isinstance(c, Or) else format_query(c) for c in node.children
    )


def _cents(value: str) -> int:
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int(frac.ljust(2, "0") or "0")


def _compare(left: int, op: str, right: int) -> bool:
    return {
        ":": left == right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }[op]


def _glob(pattern: str) -> re.Pattern[str]:
    parts = []
    for char in pattern:
        parts.append(".*" if char == "*" else "." if char == "?" else re.escape(char))
    return re.compile("".join(parts), re.DOTALL)


def matches(node: Node, item: Item) -> bool:
    if isinstance(node, Not):
        return not matches(node.child, item)
    if isinstance(node, And):
        return all(matches(c, item) for c in node.children)
    if isinstance(node, Or):
        return any(matches(c, item) for c in node.children)
    value = node.value.casefold()
    if node.field is None:
        return value in item.name.casefold() or value in item.sku.casefold()
    if node.field == "name":
        return value in item.name.casefold()
    if node.field == "sku":
        return _glob(value).fullmatch(item.sku.casefold()) is not None
    if node.field == "tag":
        return any(tag.casefold() == value for tag in item.tags)
    if node.field == "price":
        return _compare(item.price_cents, node.op, _cents(node.value))
    return _compare(item.reorder_level, node.op, int(node.value))
