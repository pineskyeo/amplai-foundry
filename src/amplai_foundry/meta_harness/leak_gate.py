"""Leak gate: validation and holdout tokens must not appear in proposals (D-098, §10.4).

``build_index`` runs at corpus freeze over validation and holdout cases only and collects task
ids, hidden test file names and ``def test…`` names, and reference-only identifiers (identifiers
of the reference files that appear neither in the base tree nor in the task text).

``LeakGate`` is the only reader of ``leak-index`` records. ``Store.get`` has no kind ACL
(``runtime/storage/store.py:326-342``), so the gate itself refuses an actor holding
``harness.propose`` (``LEAK_INDEX_ACL``, §2.7). The index never enters a proposer scratch
directory; findings never carry a token (dashboard rule, §2.16).
"""

from __future__ import annotations

import builtins
import io
import keyword
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import digest, now
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.storage.store import Scope, Store

if TYPE_CHECKING:
    from .corpus_v2 import CorpusV2

Ref = dict[str, Any]

TOKEN_KINDS = ("task_id", "hidden_test_name", "reference_only_identifier")
INDEXED_SPLITS = ("validation", "holdout")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A reference-only identifier is a leak only when it is distinctive (§10.4, as refined 2026-10-01):
# a compound name (with "_" or mixed case, not a dunder). Single plain words ("result", "total")
# matched ordinary English in legitimate proposals: 235 tokens from the 20 Work 030 tasks alone.
# Keywords, builtins and standard-library module names are never tokens. Task ids and hidden test
# names stay blocking whatever their shape.
KEYWORDS = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)
COMMON_NAMES = KEYWORDS | frozenset(dir(builtins)) | frozenset(sys.stdlib_module_names)
COMPOUND = re.compile(r"^(?!__)(?=[A-Za-z0-9_]*(?:[A-Za-z0-9]_[A-Za-z0-9]|[a-z][A-Z]))")
BOUNDARY_BEFORE, BOUNDARY_AFTER = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"


@dataclass(frozen=True)
class LeakHit:
    token: str
    kind: str
    case_id: str
    where: str  # JSON pointer inside the scanned value


def _words(data: bytes | str) -> set[str]:
    text = data.decode(errors="replace") if isinstance(data, bytes) else data
    return set(WORD.findall(text))


def _python_names(data: bytes) -> set[str]:
    """NAME tokens of a Python file (comments and strings are not identifiers)."""
    try:
        return {
            tok.string
            for tok in tokenize.tokenize(io.BytesIO(data).readline)
            if tok.type == tokenize.NAME
        }
    except (tokenize.TokenError, SyntaxError):
        # Unparseable: every word counts, which can only add tokens.
        return _words(data)


def _reference_identifiers(files: dict[str, bytes]) -> set[str]:
    names: set[str] = set()
    for name, data in files.items():
        if name.endswith(".py"):
            names |= _python_names(data)
    return {n for n in names - COMMON_NAMES if COMPOUND.match(n)}


def build_index(corpus: CorpusV2, splits: dict[str, str]) -> dict[str, Any]:
    """Tokens of the validation and holdout cases of ``corpus`` under ``splits`` (§10.4).

    Returns ``{"tokens": [...], "built_at"}``; the freeze adds schema, scope and corpus_ref.
    """
    from .corpus_v2 import hidden_test_functions

    base_words: dict[str, set[str]] = {}
    entries: set[tuple[str, str, str, str]] = set()
    for task in corpus.tasks:
        split = splits.get(task.task_id)
        if split not in INDEXED_SPLITS:
            continue
        hidden = {**task.hidden, **task.hidden_alt}
        found = {(task.task_id, "task_id")}
        found |= {(PurePosixPath(name).name, "hidden_test_name") for name in hidden}
        found |= {(name, "hidden_test_name") for name in hidden_test_functions(hidden)}
        if task.base_id not in base_words:
            root = corpus.root / corpus.bases[task.base_id]["dir"]
            words: set[str] = set()
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        words |= _words(path.read_bytes())
            base_words[task.base_id] = words
        known = base_words[task.base_id] | _words(task.contract_text())
        reference = {**task.reference, **task.reference_alt}
        found |= {
            (name, "reference_only_identifier")
            for name in _reference_identifiers(reference) - known
        }
        entries |= {(token, kind, task.task_id, split) for token, kind in found}
    tokens = [
        {"token": token, "kind": kind, "case_id": case_id, "split": split}
        for token, kind, case_id, split in sorted(entries, key=lambda e: (e[2], e[1], e[0]))
    ]
    return {"tokens": tokens, "built_at": now()}


def validate_index(value: dict[str, Any]) -> None:
    """Shape of a ``leak-index`` record (§2.7) before ``put`` and after ``get``."""
    tokens = value.get("tokens")
    if (
        value.get("schema") != "amplai.leak-index.v1"
        or not isinstance(value.get("corpus_ref"), dict)
        or not isinstance(value.get("built_at"), str)
        or not isinstance(tokens, list)
        or any(
            not isinstance(t, dict)
            or set(t) != {"token", "kind", "case_id", "split"}
            or not isinstance(t["token"], str)
            or not t["token"]
            or t["kind"] not in TOKEN_KINDS
            or not isinstance(t["case_id"], str)
            or t["split"] not in INDEXED_SPLITS
            for t in tokens
        )
    ):
        raise RuntimeFault("LEAK_INDEX", "Malformed leak-index record")


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _pattern(tokens: list[str], flags: int) -> re.Pattern[str] | None:
    if not tokens:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(tokens, key=lambda t: (-len(t), t)))
    return re.compile(f"{BOUNDARY_BEFORE}(?:{alternatives}){BOUNDARY_AFTER}", flags)


class LeakGate:
    def __init__(self, actor: Actor, store: Store, index_ref: Ref) -> None:
        if "harness.propose" in actor.permissions:
            raise Hold("LEAK_INDEX_ACL", "A proposer identity may not open the leak index")
        value = store.get(actor.scope, "leak-index", index_ref)
        validate_index(value)
        self.scope = actor.scope
        self.index_ref = dict(index_ref)
        # ids case-insensitively, identifiers (and file names) case-sensitively (§10.4)
        self._ids: dict[str, list[dict[str, str]]] = {}
        self._names: dict[str, list[dict[str, str]]] = {}
        for entry in value["tokens"]:
            if entry["kind"] == "task_id":
                self._ids.setdefault(entry["token"].lower(), []).append(entry)
            else:
                self._names.setdefault(entry["token"], []).append(entry)
        self._id_re = _pattern(list(self._ids), re.IGNORECASE)
        self._name_re = _pattern(list(self._names), 0)

    def _scan_text(self, text: str, where: str) -> list[LeakHit]:
        hits = []
        for pattern, table, fold in (
            (self._id_re, self._ids, True),
            (self._name_re, self._names, False),
        ):
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                key = match.group(0).lower() if fold else match.group(0)
                hits += [LeakHit(e["token"], e["kind"], e["case_id"], where) for e in table[key]]
        return hits

    def scan(self, value: Any) -> list[LeakHit]:
        """Every string of ``value`` (keys included), with the JSON pointer of where it sits."""
        hits: list[LeakHit] = []
        stack: list[tuple[tuple[str, ...], Any]] = [((), value)]
        while stack:
            path, item = stack.pop()
            if isinstance(item, str):
                hits += self._scan_text(item, _pointer(path))
            elif isinstance(item, dict):
                for key in reversed(list(item)):
                    child = (*path, str(key))
                    if isinstance(key, str):
                        hits += self._scan_text(key, _pointer(child))
                    stack.append((child, item[key]))
            elif isinstance(item, (list, tuple)):
                stack += [((*path, str(i)), v) for i, v in reversed(list(enumerate(item)))]
        unique = dict.fromkeys(hits)
        return list(unique)

    def findings(self, scope: Scope, proposal: dict[str, Any]) -> list[dict[str, Any]]:
        """3.0.0 finding objects (``common $defs.finding``), code ``LEAK_GATE``, blocking.

        The statement names the token kind and the JSON pointer, never the token or case id.
        """
        if scope != self.scope:
            raise RuntimeFault("SCOPE_MISMATCH", "The leak gate serves one scope")
        result = []
        for n, hit in enumerate(self.scan(proposal)):
            kind = hit.kind.replace("_", " ")
            result.append(
                {
                    "id": "leakgate-" + digest([hit.kind, hit.where, n])[7:31],
                    "severity": "blocking",
                    "code": "LEAK_GATE",
                    "statement": (
                        f"Leak gate: a {kind} of a validation or holdout task appears at "
                        f"{hit.where or '/'}"
                    ),
                    "source_refs": [dict(self.index_ref)],
                }
            )
        return result
