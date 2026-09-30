"""Deterministic prompt sections from a context policy, and the feedback form (Work 033 S3, D-096).

The execution prompt keeps its role lines, objective, scope, acceptance and upstream text
(``loop.py`` prompt). The context sections follow the acceptance and upstream lines and precede
the feedback, each only when its component is enabled (interfaces.md §4.1, row ``loop.py:522-600``).
The v1 context policy adds no section and renders the feedback exactly as the loop did before
components (golden G1, ``tests/e2e/test_033_golden_prompt.py``).

The facts come from the plan record (``repo_facts``, written by ``LocalExecutionService.plan``
before the plan workspaces are discarded) and from the installed app, never from the agent.
Retrieval, investigation notes and plan steps are sections of later slices (S9/S10); a context
policy that switches retrieval on, or a feedback mode other than ``tail``, is refused rather than
silently ignored (their rendering is not specified yet, 확인 필요).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..errors import RuntimeFault
from .policies import ContextPolicy

# collected once at plan time with the largest bounds env_bootstrap allows (§2.2); a component
# version then shows a prefix of it (its own tree_depth and tree_max_entries)
TREE_DEPTH_MAX, TREE_ENTRIES_MAX = 3, 400
FEEDBACK_HEADERS = {
    # loop.py:588-589 before components, byte for byte
    "v1": "The previous attempt did not pass. The directory already contains that attempt's "
    "changes. Fix what failed:",
    # §2.2: the directory starts again from the base; the previous changes are not applied
    "v1_fresh": "The previous attempt did not pass. The directory starts again from the base "
    "commit; that attempt's changes are not applied. Fix what failed:",
}


def check_supported(context: ContextPolicy) -> None:
    """RuntimeFault COMPONENT_CONTENT for a context part this runtime cannot honour yet."""
    unsupported = []
    if context.retrieval and context.retrieval.get("enabled"):
        unsupported.append("retrieval (method path_keyword_v1 is not specified yet)")
    if context.feedback_form["mode"] != "tail":
        unsupported.append("feedback_form.mode " + context.feedback_form["mode"])
    if context.decider_l4 is not None:
        unsupported.append("decider L4 (S10)")
    if unsupported:
        raise RuntimeFault(
            "COMPONENT_CONTENT", "The execution loop does not honour these components yet",
            details=unsupported,
        )  # fmt: skip


def repo_tree(root: Path) -> tuple[list[str], bool]:
    """Relative paths under ``root`` breadth first (directories end in "/"), each level sorted.

    The workspace's own ``.git`` (a fresh repository, ``sandbox/git_workspace.py:175-177``) is
    left out; symlinks are listed, never followed. Returns (entries, truncated).
    """
    entries: list[str] = []
    level = [root]
    for depth in range(1, TREE_DEPTH_MAX + 1):
        below: list[Path] = []
        for folder in level:
            try:
                children = sorted(folder.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for child in children:
                if depth == 1 and child.name == ".git":
                    continue
                if len(entries) == TREE_ENTRIES_MAX:
                    return entries, True
                is_dir = child.is_dir() and not child.is_symlink()
                entries.append(child.relative_to(root).as_posix() + ("/" if is_dir else ""))
                if is_dir:
                    below.append(child)
        level = below
    return entries, False


def _depth(entry: str) -> int:
    return entry.rstrip("/").count("/") + 1


def _cap(lines: list[str], max_chars: int) -> list[str]:
    """At most ``max_chars`` characters of the section (the tree is last, so it is cut first)."""
    text = "\n".join(lines)
    return text[:max_chars].split("\n") if len(text) > max_chars else lines


def env_bootstrap(
    part: dict[str, Any] | None,
    *,
    facts: dict[str, Any] | None,
    commands: dict[str, str],
    quick: Iterable[str],
) -> list[str]:
    """The environment section: verifier commands, fast test command, tool versions, tree."""
    if not part or not part["enabled"]:
        return []
    wanted, facts = set(part["facts"]), facts or {}
    body: list[str] = []
    if "verifier_commands" in wanted and commands:
        body.append("Acceptance commands: " + "; ".join(f"{k}: `{v}`" for k, v in commands.items()))
    fast = [commands[q] for q in quick if q in commands]
    if "fast_test_command" in wanted and fast:
        body.append("Fast test command: " + "; ".join(f"`{c}`" for c in fast))
    tools = facts.get("tool_versions") or []
    if "tool_versions" in wanted and tools:
        body.append("Tool versions: " + "; ".join(f"{name}: {version}" for name, version in tools))
    tree = [e for e in facts.get("tree") or [] if _depth(e) <= part["tree_depth"]]
    if "repo_tree" in wanted and tree:
        shown = tree[: part["tree_max_entries"]]
        body.append(f"Repository tree (depth {part['tree_depth']}, directories end in /):")
        body += ["  " + e for e in shown]
        if len(shown) < len(tree) or facts.get("tree_truncated"):
            body.append("  (more entries not shown)")
    if not body:
        return []
    section = ["Environment facts (collected by AMPLAI from the base commit):", *body]
    return ["", *_cap(section, part["max_chars"])]


def memory_notes(part: dict[str, Any] | None, *, app_id: str, task_class: Any) -> list[str]:
    """Approved notes for this app, generic ones and those of the goal's task class."""
    if not part or not part["enabled"]:
        return []
    notes = [
        n["text"] for n in part["notes"]
        if n["app"] == app_id and n["task_class"] in (None, task_class)
    ]  # fmt: skip
    if not notes:
        return []
    return ["", f"Notes from earlier work in {app_id}:", *("- " + text for text in notes)]


def sections(
    context: ContextPolicy,
    *,
    plan: dict[str, Any],
    app: Any,
    commands: dict[str, str],
) -> list[str]:
    """Every enabled context section in a fixed order (env bootstrap, memory notes). Nothing is
    read for a part that is off, so the v1 policy touches only what the prompt read before."""
    lines: list[str] = []
    if context.env_bootstrap and context.env_bootstrap["enabled"]:
        lines += env_bootstrap(
            context.env_bootstrap,
            facts=(plan.get("repo_facts") or {}).get(app.app_id),
            commands=commands,
            # design goals verify the document only: the app's quick checks do not apply
            quick=() if plan.get("mode") == "design" else app.quick_verifiers,
        )
    if context.memory_notes and context.memory_notes["enabled"]:
        lines += memory_notes(
            context.memory_notes, app_id=app.app_id, task_class=plan["draft"].get("task_class")
        )
    return lines


def _detail_items(value: Any, limit: int) -> list[Any]:
    """The first ``limit`` items of one observation detail. A list, tuple or string is sliced
    as ``loop.py:587-599`` sliced it (G1); any other value (the suite's ``patch_bytes`` and
    ``seconds`` are numbers, ``verification/runtime/patch_commands.py:68-88``) is one item."""
    items = value if isinstance(value, (list, tuple, str)) else [value]
    return list(items[:limit])


def feedback(form: dict[str, Any], observations: list[dict[str, Any]]) -> list[str]:
    """The feedback section after a failed attempt; mode ``tail`` with v1 values is
    ``loop.py:587-599`` before components."""
    if form["mode"] != "tail":
        raise RuntimeFault("COMPONENT_CONTENT", "Only the tail feedback form is implemented")
    lines = ["", FEEDBACK_HEADERS[form["header"]]]
    for o in observations:
        if o["outcome"] == "pass":
            continue
        tail = o["details"].get("stdout_tail", "") + o["details"].get("stderr_tail", "")
        lines.append(f"- {o['acceptance_id']} {o['outcome']}: {o['reason']}")
        for key in form["detail_keys"]:
            if o["details"].get(key):  # the design check says exactly what to fix
                values = _detail_items(o["details"][key], form["detail_limit"])
                lines.append(f"  {key}: " + ", ".join(map(str, values)))
        if tail and form["tail_chars"]:  # tail[-0:] would be the whole text
            lines.append("```\n" + tail[-form["tail_chars"] :] + "\n```")
    return lines
