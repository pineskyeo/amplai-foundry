"""Answer-lookup attempts in a turn's raw provider events (operator decision (C), 2026-10-08).

A trial must solve its task from the workspace. Three kinds of attempt to find the answer
elsewhere are recorded with their evidence, and a trial with one counts as failed
(``meta_harness/local_executor.py``):

- ``outside_workspace_search``: a search command whose root is outside ``/workspace``: ``find``,
  ``fd``, ``tree``, recursive ``grep``, ``rg``/``ag``/``ack``, ``ls -R``, any ``locate``; plain
  ``ls`` only of ``/`` or the home directory; Claude ``Grep``/``Glob`` with such a path.
  ``/tmp`` is not outside: it is the container's own empty tmpfs (``sandbox/container.py``
  ``--tmpfs /tmp``), so it cannot hold an answer; nor is Codex's own skill directory
  ``/home/agent/.codex/skills``, which Codex's instructions point the model at; nor is a mount the
  turn was given itself (``inside``: a read-only turn's ``/amplai-input/...`` mounts, e.g. the
  other apps of a multi-app planner turn, ``runtime/execution/readonly_turn.py``).
- ``git_history``: reading git history beyond the base commit: ``git fsck``, ``git reflog``,
  ``git log``/``rev-list``/``shortlog``/``whatchanged``/``show-branch`` with ``--all``,
  ``--reflog``, ``-g``, ``--branches``, ``--tags``, ``--remotes`` or ``--glob``,
  ``git cat-file --batch-all-objects``, listing a ``.git`` directory recursively, or reading
  ``.git/objects``, ``.git/logs`` or ``.git/lost-found``. ``git status``, ``git diff``,
  ``git show`` and ``git log`` of ``HEAD`` are ordinary work and never match. A ``cat-file`` of
  one named object is not matched: the trial workspace holds only the base commit's objects
  (``sandbox/git_workspace.py``), so an unknown id can only come from a matched enumeration.
- ``web_search``: a web search or fetch tool call: a Codex item whose type names web search, an
  item or tool named ``web.run``/``web__run``/``web_search``/``browser...``; a Claude
  ``WebSearch``/``WebFetch`` tool use or ``server_tool_use``, or a Claude ``result`` whose usage
  counts ``server_tool_use`` web requests.

Only what the agent asked for is read (a command, a tool name and its input), never a command's
output. A command is read as a shell script: ``&&``, ``||``, ``;``, ``|``, newlines, ``$(...)`` and
backticks separate commands; ``cd`` moves the directory later relative paths resolve from;
``bash -c``/``sh -c`` scripts, ``sudo``/``env``/``timeout``/``xargs`` prefixes and heredoc bodies
are handled. Not detected (no false positives were preferred to more coverage): searches written
inside another language (``python -c "os.walk('/')"``), and a ``cd`` in one Claude ``Bash`` call
followed by a relative search in a later call.

The Codex ``exec --json`` item shapes beyond ``command_execution`` (``command``, ``cwd``) are not
recorded for 0.155.1 (no stored raw stream holds one, §14 Q14), so a non-command item is matched
by its type and its name fields, never by its output text.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections.abc import Iterable
from typing import Any

WORKSPACE = "/workspace"  # sandbox/container.py: --mount dst=/workspace, --workdir /workspace
HOME = "/home/agent"  # sandbox/container.py: HOME=/home/agent (the native home mount)
SCRATCH = ("/tmp",)  # sandbox/container.py: --tmpfs /tmp, the run's own empty scratch
# Codex's own skill files, which its instructions point the model at (the pilot rollout's skill
# root ``r0 = /home/agent/.codex/skills/.system``): reading them is not a lookup
AGENT_TOOL_ROOTS = (HOME + "/.codex/skills",)
KINDS = ("outside_workspace_search", "git_history", "web_search")
EVIDENCE_MAX = 300  # characters of one evidence line
DISPATCH_MAX = 20  # evidence entries kept per dispatch
WITHHELD = "[withheld: the command text holds a secret pattern or a credential]"

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
# a prefix that runs the rest of its argv as the command (its own options skipped)
PREFIXES = frozenset({"sudo", "env", "command", "exec", "nohup", "nice", "time", "stdbuf",
                      "timeout", "xargs", "builtin"})  # fmt: skip
PREFIX_ARG_OPTIONS = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-U", "-r", "-t"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "-i", "-l"}),
}
READERS = frozenset({"cat", "less", "more", "head", "tail", "ls", "find", "tree", "du", "xxd",
                     "od", "hexdump", "strings", "zcat", "gzip", "gunzip", "pigz", "zlib-flate",
                     "file", "wc", "stat", "cp", "tar", "base64", "sort", "grep", "rg", "ag",
                     "ack", "fd", "fdfind"})  # fmt: skip
GIT_HISTORY_DIRS = frozenset({"objects", "logs", "lost-found"})
GIT_ENUMERATORS = frozenset({"log", "rev-list", "shortlog", "whatchanged", "show-branch"})
GIT_ALL_FLAGS = ("--all", "--reflog", "-g", "--walk-reflogs", "--branches", "--tags",
                 "--remotes", "--glob", "--alternate-refs", "--indexed-objects")  # fmt: skip
GIT_GLOBAL_ARG = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                            "--exec-path", "--super-prefix", "--config-env"})  # fmt: skip
LOCATE = frozenset({"locate", "plocate", "mlocate", "slocate"})
# grep options that take the next word as their argument
GREP_ARG_SHORT = frozenset("efmABCdD")
GREP_ARG_LONG = frozenset({"--regexp", "--file", "--max-count", "--after-context",
                           "--before-context", "--context", "--directories", "--devices",
                           "--include", "--exclude", "--exclude-dir", "--exclude-from",
                           "--label", "--group-separator"})  # fmt: skip
RG_ARG_SHORT = frozenset("efgtTmABCjMErd")
RG_ARG_LONG = frozenset({"--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
                         "--type-add", "--type-clear", "--max-count", "--after-context",
                         "--before-context", "--context", "--threads", "--max-columns",
                         "--encoding", "--engine", "--replace", "--max-depth", "--sort",
                         "--sortr", "--color", "--colors", "--path-separator", "--pre",
                         "--pre-glob", "--max-filesize", "--ignore-file", "--dfa-size-limit",
                         "--regex-size-limit", "--context-separator", "--field-match-separator",
                         "--field-context-separator", "--hostname-bin", "--hyperlink-format",
                         "--max-columns-preview"})  # fmt: skip
FD_ARG_SHORT = frozenset("eEtdxXSojc")
TREE_ARG_SHORT = frozenset("LPIoHT")
FIND_GLOBAL = frozenset({"-H", "-L", "-P"})
FIND_GLOBAL_ARG = frozenset({"-D"})
# a Codex item or tool name that is a web search or fetch (web.run, web__run, web_search, ...)
WEB_TYPE = re.compile(r"web[_.]?search|web(\.|__)run|web[_.]?fetch", re.IGNORECASE)
WEB_NAME = re.compile(
    r"web[_.]?search|web[_.]?fetch|(^|[._:/-])web(\.|__|_)?run($|[._(])|^web$|^browser",
    re.IGNORECASE,
)
WEB_CALL = re.compile(r"\bweb__run\b|\bweb\.run\b|\bweb_search\b|\bweb_fetch\b", re.IGNORECASE)
CLAUDE_WEB_TOOLS = frozenset({"WebSearch", "WebFetch"})
CODEX_NAME_FIELDS = ("name", "tool", "server", "tool_name")
CODEX_INPUT_FIELDS = ("input", "arguments", "code")


def _finding(kind: str, rule: str, evidence: str, source: str) -> dict[str, str]:
    text = " ".join(evidence.split())
    if len(text) > EVIDENCE_MAX:
        text = text[: EVIDENCE_MAX - 1] + "…"
    return {"kind": kind, "rule": rule, "evidence": text, "source": source}


# -- shell scripts -------------------------------------------------------------------------------
def _strip_heredocs(script: str) -> str:
    """The script without heredoc bodies (their lines are data, not commands)."""
    out: list[str] = []
    ending: list[tuple[str, bool]] = []
    for line in script.split("\n"):
        if ending:
            word, tabs = ending[0]
            if (line.lstrip("\t") if tabs else line) == word:
                ending.pop(0)
            continue
        out.append(line)
        for match in re.finditer(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z0-9_.-]+)\2", line):
            ending.append((match.group(3), bool(match.group(1))))
    return "\n".join(out)


def _unquoted(script: str) -> str:
    """Newlines and backticks outside quotes become ``;``, comments and line continuations go;
    quoted text is kept as it is."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(script):
        c = script[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(script):
                out.append(script[i + 1])
                i += 1
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(script):
            if script[i + 1] != "\n":
                out.append(c + script[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (not out or out[-1] in " \t\n;&|()"):
            while i < len(script) and script[i] != "\n":
                i += 1
            continue
        elif c in "\n`":
            out.append(" ; ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _segments(script: str) -> list[list[str]]:
    """The simple commands of a script, each as its words (redirections removed)."""
    text = _unquoted(_strip_heredocs(script))
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: read the words as they stand
        tokens = text.split()
    segments: list[list[str]] = [[]]
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token and set(token) <= set("<>&") and ("<" in token or ">" in token):
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()  # the fd number of 2>/dev/null
            skip = True  # the redirection target
            continue
        if token and set(token) <= set(";&|()"):
            segments.append([])
            continue
        segments[-1].append(token)
    return [s for s in segments if s]


def _home_path(token: str) -> str:
    for prefix in ("${HOME}", "$HOME"):
        if token == prefix or token.startswith(prefix + "/"):
            return HOME + token[len(prefix) :]
    if token == "~" or token.startswith("~/"):
        return HOME + token[1:]
    if token.startswith("~"):  # ~user
        return "/home/" + token[1:]
    return token


def _resolve(token: str, cwd: str) -> str | None:
    """The absolute path a word names from ``cwd``; None when a variable leaves it unknown."""
    token = _home_path(token)
    for prefix in ("${PWD}", "$PWD"):
        if token == prefix or token.startswith(prefix + "/"):
            token = cwd + token[len(prefix) :]
    if "$" in token or not token:
        return None
    path = token if token.startswith("/") else posixpath.join(cwd, token)
    return posixpath.normpath(path) if path != "//" else "/"


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _outside(path: str, inside: tuple[str, ...] = ()) -> bool:
    return not any(
        _inside(path, root) for root in (WORKSPACE, *SCRATCH, *AGENT_TOOL_ROOTS, *inside)
    )


def _roots(inside: Iterable[str]) -> tuple[str, ...]:
    """The absolute, normalized paths of ``inside`` (the turn's own mounts); others are ignored,
    and ``/`` never is one (it would make every search inside)."""
    out = []
    for root in inside:
        if isinstance(root, str) and root.startswith("/"):
            path = posixpath.normpath(root)
            if path not in {"/", "//"}:
                out.append(path)
    return tuple(out)


def _git_part(path: str) -> str | None:
    """``objects``/``logs``/``lost-found`` when ``path`` lies in that part of a ``.git``
    directory, ``""`` when it is a ``.git`` directory itself or another part of one, else None."""
    parts = path.split("/")
    if ".git" not in parts:
        return None
    rest = parts[parts.index(".git") + 1 :]
    if rest and rest[0] in GIT_HISTORY_DIRS:
        return rest[0]
    return ""


def _options(
    args: list[str], short_arg: frozenset[str], long_arg: frozenset[str]
) -> tuple[set[str], list[str]]:
    """(flags, positional words) of ``args`` for a getopt-style command; ``--`` ends options."""
    flags: set[str] = set()
    positional: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--":
            positional += args[i + 1 :]
            break
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            flags.add(name)
            if "=" not in word and name in long_arg:
                i += 1
        elif word.startswith("-") and len(word) > 1:
            for j, letter in enumerate(word[1:]):
                flags.add("-" + letter)
                if letter in short_arg:
                    if j == len(word) - 2:
                        i += 1  # its argument is the next word
                    break  # the rest of this word is its argument
        else:
            positional.append(word)
        i += 1
    return flags, positional


def _command_words(words: list[str]) -> list[str]:
    """The words of the command a prefix (``sudo``, ``env``, ``timeout`` ...) and leading
    ``NAME=value`` assignments run."""
    i = 0
    while i < len(words):
        word = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            i += 1
            continue
        name = posixpath.basename(word)
        if name not in PREFIXES:
            break
        i += 1
        takes = PREFIX_ARG_OPTIONS.get(name, frozenset())
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in takes else 1
        if name == "timeout" and i < len(words):
            i += 1  # the duration
    return words[i:]


def _search(name: str, args: list[str]) -> tuple[list[str], bool] | None:
    """(search roots, recursive) of a search command, or None when ``name`` is not one."""
    if name == "find":
        i = 0
        while i < len(args) and (args[i] in FIND_GLOBAL or args[i] in FIND_GLOBAL_ARG
                                 or args[i].startswith("-O")):  # fmt: skip
            i += 2 if args[i] in FIND_GLOBAL_ARG else 1
        roots = []
        for word in args[i:]:
            if word.startswith("-") or word in {"(", "!", ")"}:
                break
            roots.append(word)
        return roots or ["."], True
    if name in {"grep", "egrep", "fgrep", "rgrep"}:
        flags, positional = _options(args, GREP_ARG_SHORT, GREP_ARG_LONG)
        recursive = name == "rgrep" or bool(
            flags & {"-r", "-R", "--recursive", "--dereference-recursive"}
        ) or any(a in {"--directories=recurse", "-drecurse"} for a in args)  # fmt: skip
        if not recursive:
            return None
        pattern_given = bool(flags & {"-e", "-f", "--regexp", "--file"})
        paths = positional if pattern_given else positional[1:]
        return paths or ["."], True
    if name in {"rg", "ag", "ack", "ack-grep"}:
        flags, positional = _options(args, RG_ARG_SHORT, RG_ARG_LONG)
        pattern_given = bool(flags & {"-e", "-f", "--regexp", "--file", "--files"})
        paths = positional if pattern_given else positional[1:]
        return paths or ["."], True
    if name in {"fd", "fdfind"}:
        _flags, positional = _options(args, FD_ARG_SHORT, frozenset())
        return positional[1:] or ["."], True
    if name == "tree":
        _flags, positional = _options(args, TREE_ARG_SHORT, frozenset())
        return positional or ["."], True
    if name == "ls":
        flags, positional = _options(args, frozenset(), frozenset())
        return positional or ["."], bool(flags & {"-R", "--recursive"})
    if name == "du":
        _flags, positional = _options(args, frozenset("dBt"), frozenset())
        return positional or ["."], False
    return None


def _git(args: list[str], source: str, text: str) -> list[dict[str, str]]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        word = args[i]
        i += 2 if word in GIT_GLOBAL_ARG else 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1 :]
    if sub in {"fsck", "fsck-objects", "reflog", "lost-found"}:
        return [_finding("git_history", f"git {sub}", text, source)]
    if sub in GIT_ENUMERATORS:
        for word in rest:
            if word == "--":
                break
            flag = word.split("=", 1)[0]
            if flag in GIT_ALL_FLAGS:
                return [_finding("git_history", f"git {sub} {flag}", text, source)]
    if sub == "cat-file" and "--batch-all-objects" in rest:
        return [_finding("git_history", "git cat-file --batch-all-objects", text, source)]
    return []


def _simple(
    words: list[str], cwd: str, source: str, text: str, depth: int, inside: tuple[str, ...]
) -> list[dict[str, str]]:
    words = _command_words(words)
    if not words:
        return []
    name = posixpath.basename(words[0])
    args = words[1:]
    if name in SHELLS:
        for j, word in enumerate(args):
            if re.fullmatch(r"-[a-z]*c[a-z]*", word) and j + 1 < len(args):
                return scan_command(
                    args[j + 1], cwd=cwd, source=source, depth=depth + 1, inside=inside
                )
        return []
    if name == "git":
        return _git(args, source, text)
    if name in LOCATE:
        return [_finding("outside_workspace_search", name, text, source)]
    found: list[dict[str, str]] = []
    search = _search(name, args)
    if search is not None:
        roots, recursive = search
        for root in roots:
            path = _resolve(root, cwd)
            if path is None:
                continue
            part = _git_part(path)
            if part or (part == "" and recursive):
                found.append(_finding("git_history", f"{name} .git/{part}".rstrip("/"), text,
                                      source))  # fmt: skip
                break
            if not _outside(path, inside):
                continue
            if recursive and name != "du":
                found.append(_finding("outside_workspace_search", f"{name} {root}", text, source))
                break
            if name == "ls" and path in {"/", HOME, "/root", "/home"}:
                found.append(_finding("outside_workspace_search", f"ls {root}", text, source))
                break
    if not found and name in READERS:
        for word in args:
            if word.startswith("-"):
                continue
            path = _resolve(word, cwd)
            part = _git_part(path) if path is not None else None
            if part:
                found.append(_finding("git_history", f"{name} .git/{part}", text, source))
                break
    return found


def scan_command(
    command: Any,
    *,
    cwd: str = WORKSPACE,
    source: str = "command",
    depth: int = 0,
    inside: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Findings of one command: a shell script string, or an argv list (``bash -lc <script>``
    is read as its script). ``inside``: the turn's own mounts, which are not outside."""
    if depth > 3:
        return []
    roots = _roots(inside)
    if isinstance(command, list) and all(isinstance(w, str) for w in command):
        words = list(command)
    elif isinstance(command, str):
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
    else:
        return []
    if (
        len(words) >= 3
        and posixpath.basename(words[0]) in SHELLS
        and re.fullmatch(r"-[a-z]*c[a-z]*", words[1])
    ):
        script = words[2]
    elif isinstance(command, str):
        script = command
    else:
        script = shlex.join(words)
    found: list[dict[str, str]] = []
    here = cwd
    for segment in _segments(script):
        head = _command_words(segment)
        if head and head[0] == "cd":
            target = head[1] if len(head) > 1 else "~"
            moved = _resolve(target, here) if target != "-" else None
            here = moved or here
            continue
        text = " ".join(segment)
        found += _simple(segment, here, source, text if here == cwd else f"cd {here}; {text}",
                         depth, roots)  # fmt: skip
    return found


# -- provider events -----------------------------------------------------------------------------
def _cwd(value: Any) -> str:
    if isinstance(value, str):
        path = value.removeprefix("file://")
        if path.startswith("/"):
            return posixpath.normpath(path)
    return WORKSPACE


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _codex(event: dict[str, Any], inside: tuple[str, ...]) -> list[dict[str, str]]:
    item = event.get("item")
    if not isinstance(item, dict):
        kind = event.get("type")
        if isinstance(kind, str) and WEB_TYPE.search(kind):  # a web event outside any item
            return [_finding("web_search", f"codex event {kind}", kind, kind)]
        return []
    item_type = item.get("type")
    source = f"{event.get('type')}/{item_type}"
    if item_type == "command_execution":
        return scan_command(
            item.get("command"), cwd=_cwd(item.get("cwd")), source=source, inside=inside
        )
    named = [str(item[f]) for f in CODEX_NAME_FIELDS if isinstance(item.get(f), str)]
    inputs = [_text(item[f]) for f in CODEX_INPUT_FIELDS if f in item]
    action = item.get("action")
    query = str(item["query"]) if isinstance(item.get("query"), str) else ""
    if isinstance(action, dict) and isinstance(action.get("query"), str):
        query = str(action["query"])
    evidence = " ".join([str(item_type), *named, query, *(i[:200] for i in inputs)])
    if isinstance(item_type, str) and WEB_TYPE.search(item_type):
        return [_finding("web_search", f"codex item {item_type}", evidence, source)]
    web_name = next((n for n in named if WEB_NAME.search(n)), None)
    if web_name is not None:
        return [_finding("web_search", "codex tool " + web_name, evidence, source)]
    if any(WEB_CALL.search(i) for i in inputs):
        return [_finding("web_search", "codex tool call in input", evidence, source)]
    return []


def _claude(event: dict[str, Any], inside: tuple[str, ...]) -> list[dict[str, str]]:
    kind = event.get("type")
    found: list[dict[str, str]] = []
    if kind == "result":
        usage = event.get("usage")
        server = usage.get("server_tool_use") if isinstance(usage, dict) else None
        if isinstance(server, dict):
            for key in ("web_search_requests", "web_fetch_requests"):
                count = server.get(key)
                if type(count) is int and count > 0:
                    found.append(_finding(
                        "web_search", f"claude result {key}",
                        f"result.usage.server_tool_use.{key}={count}", "result",
                    ))  # fmt: skip
        return found
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if kind != "assistant" or not isinstance(blocks, list):
        return []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in {"tool_use", "server_tool_use"}:
            continue
        name = block.get("name")
        given = block.get("input")
        payload: dict[str, Any] = given if isinstance(given, dict) else {}
        source = f"assistant/{block.get('type')}/{name}"
        if not isinstance(name, str):
            continue
        if name in CLAUDE_WEB_TOOLS or WEB_NAME.search(name):
            evidence = " ".join([name, *(str(payload.get(k)) for k in ("query", "url")
                                         if isinstance(payload.get(k), str))])  # fmt: skip
            found.append(_finding("web_search", f"claude {name}", evidence, source))
        elif name == "Bash":
            found += scan_command(payload.get("command"), source=source, inside=inside)
        elif name in {"Grep", "Glob"}:
            roots = [payload.get("path")]
            pattern = payload.get("pattern")
            if name == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~", "$")):
                roots.append(pattern)  # an absolute Glob pattern searches from its own root
            for root in roots:
                if not isinstance(root, str):
                    continue
                resolved = _resolve(root, WORKSPACE)
                if resolved is None:
                    continue
                part = _git_part(resolved)
                if part is not None:
                    found.append(_finding("git_history", f"claude {name} .git", _text(payload),
                                          source))  # fmt: skip
                    break
                if _outside(resolved, inside):
                    found.append(_finding("outside_workspace_search", f"claude {name} {root}",
                                          _text(payload), source))  # fmt: skip
                    break
    return found


def scan_event(provider: str, event: Any, *, inside: Iterable[str] = ()) -> list[dict[str, str]]:
    """Findings of one raw decoded provider event (``codex`` or ``claude``); never raises.
    ``inside``: absolute mount paths the turn was given, which are not outside the workspace."""
    if not isinstance(event, dict):
        return []
    try:
        roots = _roots(inside)
        if provider == "codex":
            return _codex(event, roots)
        if provider == "claude":
            return _claude(event, roots)
    except Exception:  # an odd event is no evidence; it never stops the stream
        return []
    return []


def scan_turn(
    provider: str,
    events: Iterable[Any],
    literals: Iterable[str] = (),
    *,
    inside: Iterable[str] = (),
) -> list[dict[str, str]]:
    """The findings of a whole turn's decoded events (a read-only turn, which decodes its own
    stream, ``runtime/execution/readonly_turn.py``): each event through ``scan_event``, evidence
    with a secret or one of ``literals`` withheld, at most ``DISPATCH_MAX``, no duplicates."""
    secrets = [s for s in literals if s]
    roots = _roots(inside)
    kept: list[dict[str, str]] = []
    for event in events:
        found = scan_event(provider, event, inside=roots)
        if found:
            merge(kept, withheld(found, secrets))
    return kept


def withheld(findings: Iterable[dict[str, str]], literals: Iterable[str]) -> list[dict[str, str]]:
    """``findings`` whose evidence holds a secret pattern (``runtime/evidence/cas.py``
    ``scan_secrets``, which would refuse the trial receipt) or one of ``literals`` (the turn's own
    credential values, IC-28) with the evidence replaced by ``WITHHELD``."""
    from amplai_foundry.runtime.evidence.cas import scan_secrets

    secrets = [s for s in literals if s]
    out = []
    for finding in findings:
        text = finding["evidence"]
        if scan_secrets(text.encode()) or any(s in text for s in secrets):
            finding = {**finding, "evidence": WITHHELD}
        out.append(finding)
    return out


def merge(kept: list[dict[str, str]], found: Iterable[dict[str, str]]) -> bool:
    """Add new findings to ``kept`` (at most ``DISPATCH_MAX``, no duplicates); True when one was
    added."""
    added = False
    for finding in found:
        if len(kept) >= DISPATCH_MAX:
            break
        key = (finding["kind"], finding["rule"], finding["evidence"])
        if any((k["kind"], k["rule"], k["evidence"]) == key for k in kept):
            continue
        kept.append(dict(finding))
        added = True
    return added
