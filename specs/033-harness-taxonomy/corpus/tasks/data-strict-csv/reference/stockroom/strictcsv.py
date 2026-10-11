"""A strict CSV reader that reports where a file is malformed, and its canonical writer."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .errors import ParseError

LINE_ENDS = ("\r\n", "\n", "\r")


class CsvSyntaxError(ParseError):
    def __init__(self, line: int, column: int, reason: str) -> None:
        super().__init__(f"line {line}, column {column}: {reason}")
        self.line = line
        self.column = column
        self.reason = reason


def _check_delimiter(delimiter: str) -> None:
    if len(delimiter) != 1 or delimiter in '"\r\n\ufeff':
        raise ValueError(f"bad delimiter: {delimiter!r}")


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.line = 1
        self.column = 1

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def at_line_end(self) -> bool:
        return self.peek() in ("\r", "\n") and self.peek() != ""

    def advance(self) -> str:
        """Consume one character, or a whole ``\\r\\n``; returns what was consumed."""
        char = self.text[self.i]
        if char == "\r" and self.text.startswith("\r\n", self.i):
            self.i += 2
            self.line += 1
            self.column = 1
            return "\r\n"
        self.i += 1
        if char in "\r\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return char


def parse_csv(text: str, *, delimiter: str = ",") -> list[list[str]]:
    _check_delimiter(delimiter)
    if text.startswith("\ufeff"):
        text = text[1:]
    scan = _Scanner(text)
    records: list[list[str]] = []
    width = None
    while scan.i < len(text):
        if scan.at_line_end():
            scan.advance()
            continue
        start_line = scan.line
        record: list[str] = []
        while True:
            if scan.peek() == '"':
                open_line, open_column = scan.line, scan.column
                scan.advance()
                parts: list[str] = []
                while True:
                    if scan.i >= len(text):
                        raise CsvSyntaxError(open_line, open_column, "unterminated quoted field")
                    if scan.peek() == '"':
                        scan.advance()
                        if scan.peek() == '"':
                            scan.advance()
                            parts.append('"')
                            continue
                        break
                    parts.append(scan.advance())
                following = scan.peek()
                if following not in ("", delimiter, "\r", "\n"):
                    raise CsvSyntaxError(
                        scan.line, scan.column, "unexpected character after closing quote"
                    )
                record.append("".join(parts))
            else:
                parts = []
                while scan.peek() not in ("", delimiter, "\r", "\n"):
                    if scan.peek() == '"':
                        raise CsvSyntaxError(scan.line, scan.column, "quote in unquoted field")
                    parts.append(scan.advance())
                record.append("".join(parts))
            if scan.peek() == delimiter:
                scan.advance()
                continue
            break
        if width is None:
            width = len(record)
        elif len(record) != width:
            raise CsvSyntaxError(start_line, 1, f"expected {width} fields, found {len(record)}")
        records.append(record)
        if scan.at_line_end():
            scan.advance()
    return records


def _field(value: str, delimiter: str, alone: bool) -> str:
    if (
        delimiter in value
        or '"' in value
        or "\r" in value
        or "\n" in value
        or value.startswith("\ufeff")
        or (alone and value == "")
    ):
        return '"' + value.replace('"', '""') + '"'
    return value


def format_csv(
    rows: Iterable[Sequence[str]], *, delimiter: str = ",", line_end: str = "\r\n"
) -> str:
    _check_delimiter(delimiter)
    if line_end not in LINE_ENDS:
        raise ValueError(f"bad line end: {line_end!r}")
    out: list[str] = []
    width = None
    for row in rows:
        row = list(row)
        if not row:
            raise ValueError("a row needs at least one field")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ValueError("rows differ in length")
        alone = len(row) == 1
        out.append(delimiter.join(_field(v, delimiter, alone) for v in row) + line_end)
    return "".join(out)
