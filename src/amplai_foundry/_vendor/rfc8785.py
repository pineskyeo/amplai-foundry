# SPDX-License-Identifier: Apache-2.0
# Adapted from trailofbits/rfc8785.py v0.1.4, _impl.py.
# Copyright (c) 2022 Trail of Bits, Inc. Portions derived from Andrew Rundgren.
# Source: https://github.com/trailofbits/rfc8785.py/tree/v0.1.4
# Changes: simplified annotations and formatting only; same canonicalization algorithm.
"""RFC 8785 JSON Canonicalization Scheme. Not Python sort_keys serialization."""

from __future__ import annotations

import math
import re
from io import BytesIO


class CanonicalizationError(ValueError):
    pass


class IntegerDomainError(CanonicalizationError):
    pass


class FloatDomainError(CanonicalizationError):
    pass


_ESCAPE = re.compile(r'[\x00-\x1f\\"\b\f\n\r\t]')
# The explicit character class below prevents double escaping in generated sources.
_ESCAPE = re.compile('[\x00-\x1f\\\\"\\b\\f\\n\\r\\t]')
_ESCAPE_DCT = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
# Initialized with actual control characters, rather than literal backslash-b strings.
_ESCAPE_DCT = {
    chr(92): chr(92) * 2,
    chr(34): chr(92) + chr(34),
    chr(8): r"\b",
    chr(12): r"\f",
    chr(10): r"\n",
    chr(13): r"\r",
    chr(9): r"\t",
}
for _i in range(32):
    _ESCAPE_DCT.setdefault(chr(_i), "\\u%04x" % _i)


def _string(value: str, sink: BytesIO) -> None:
    try:
        # Iterate characters to avoid regex portability and reject lone surrogates.
        encoded = "".join(_ESCAPE_DCT.get(c, c) for c in value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("input contains non-UTF-8 codepoints") from exc
    sink.write(b'"' + encoded + b'"')


def _float(value: float, sink: BytesIO) -> None:
    if math.isnan(value) or math.isinf(value):
        raise FloatDomainError("NaN and Infinity are outside the JCS domain")
    if value == 0:
        sink.write(b"0")
        return
    if value < 0:
        sink.write(b"-")
        _float(-value, sink)
        return
    text = str(value)
    exponent, exp = "", 0
    q = text.find("e")
    if q > 0:
        exponent = text[q:]
        if exponent[2:3] == "0":
            exponent = exponent[:2] + exponent[3:]
        text = text[:q]
        exp = int(exponent[1:])
    first, dot, last = text, "", ""
    q = text.find(".")
    if q > 0:
        first, dot, last = text[:q], ".", text[q + 1 :]
    if last == "0":
        dot, last = "", ""
    if 0 < exp < 21:
        first += last
        last = dot = exponent = ""
        q = exp - len(first)
        while q >= 0:
            q -= 1
            first += "0"
    elif -7 < exp < 0:
        last, first, dot, exponent = first + last, "0", ".", ""
        q = exp
        while q < -1:
            q += 1
            last = "0" + last
    sink.write((first + dot + last + exponent).encode("ascii"))


def dump(value: object, sink: BytesIO) -> None:
    if value is None:
        sink.write(b"null")
    elif isinstance(value, bool):
        sink.write(b"true" if value else b"false")
    elif isinstance(value, int):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise IntegerDomainError("integer exceeds the interoperable IEEE-754 domain")
        sink.write(str(value).encode("ascii"))
    elif isinstance(value, str):
        _string(value, sink)
    elif isinstance(value, float):
        _float(value, sink)
    elif isinstance(value, (list, tuple)):
        sink.write(b"[")
        for i, item in enumerate(value):
            if i:
                sink.write(b",")
            dump(item, sink)
        sink.write(b"]")
    elif isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalizationError("object keys must be strings")
        try:
            pairs = sorted(value.items(), key=lambda pair: pair[0].encode("utf-16be"))
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("invalid object key") from exc
        sink.write(b"{")
        for i, (key, item) in enumerate(pairs):
            if i:
                sink.write(b",")
            _string(key, sink)
            sink.write(b":")
            dump(item, sink)
        sink.write(b"}")
    else:
        raise CanonicalizationError("unsupported type: " + type(value).__name__)


def dumps(value: object) -> bytes:
    sink = BytesIO()
    dump(value, sink)
    return sink.getvalue()
