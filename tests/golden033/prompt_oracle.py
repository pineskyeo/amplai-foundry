# ruff: noqa
# mypy: ignore-errors
# Copied from src/amplai_foundry/runtime/execution/loop.py at c9f896a (FEEDBACK_TAIL :36,
# UPSTREAM_PATCH :43, prompt :522-600, _implementer_role :602-616) and prompts.py at c9f896a
# (IMPLEMENTER_BASELINE :24-31, render :66-69); do not edit. Work 033 S0 golden oracle G1,
# adapted only to take plain arguments instead of self (the app and the looked-up bundle are
# passed in).

from typing import Any

FEEDBACK_TAIL = 3000
UPSTREAM_PATCH = 60_000  # characters of an upstream patch shown to a downstream node


# Byte-for-byte the IMPLEMENTER role text the loop built before bundles existed (Work 018).
IMPLEMENTER_BASELINE: tuple[str, ...] = (
    "You are the IMPLEMENTER for AMPLAI. The current directory is a copy of the "
    "{app_id} repository at commit {base_commit} (no .git, network limited).",
    "Make the change below. Do not commit. When you are done, run the acceptance "
    "commands yourself; the result is judged by running them on a clean copy with "
    "exactly your file changes applied.",
)


def render(lines: list[str] | tuple[str, ...], *, app_id: str, base_commit: str) -> list[str]:
    return [
        line.replace("{app_id}", app_id).replace("{base_commit}", base_commit) for line in lines
    ]


def oracle_prompt(
    contract: dict[str, Any],
    plan: dict[str, Any],
    feedback: list[dict[str, Any]] | None,
    *,
    app: Any,  # the AppConfig of the node's app (adapted: the caller resolves it)
    role_lines: list[str],  # implementer_role(...) (adapted: the caller resolves it)
    node: dict[str, Any] | None = None,
    upstream: list[tuple[str, str]] | None = None,
) -> str:
    commands = {v.id: " ".join(v.argv) for v in app.verifiers}
    if plan.get("mode") == "design":
        commands = {"design": "the design document check (sections, sources, paths)"}
        lines = [
            "You are the DESIGNER for AMPLAI. The current directory is a copy of the "
            f"{app.app_id} repository at commit {plan['base_commit']} (no .git, network "
            "limited).",
            f"Write one design document: {plan['design_dir']}design.md. Change nothing "
            "else: no source, test or configuration file. Do not commit.",
            "Use exactly these '## ' sections: Goal, Current State, Options, Decision, Risks, "
            "Implementation Plan, Sources.",
            "Ground every statement about the current system in this repository and cite it "
            "as path:line (for example src/pkg/module.py:42); at least 3 citations, and "
            "every citation must name a committed regular file here exactly (not a symlink, "
            "not the design directory). Implementation Plan lists the steps for a later "
            "work goal; do not implement them.",
            "",
            "Objective: " + contract["objective"],
        ]
    else:
        lines = [
            *render(
                role_lines,
                app_id=app.app_id,
                base_commit=plan["base_commit"],
            ),
            "",
            "Objective: " + contract["objective"],
        ]
    draft = plan["draft"]
    if draft.get("in_scope"):
        lines.append("In scope: " + "; ".join(draft["in_scope"]))
    if contract["non_goals"]:
        lines.append("Do not: " + "; ".join(contract["non_goals"]))
    lines += ["Constraints: " + "; ".join(c["statement"] for c in contract["constraints"])]
    mapping = plan.get("acceptance_map")
    if node is not None and len(plan.get("work_items") or []) > 1:
        lines.append(f"This part of the goal, in {app.app_id}: {node['objective']}")
    lines.append("Acceptance (each must pass):")
    if mapping and node is not None:
        for ac in node["acceptance_ids"]:
            entry = mapping[ac]
            lines.append(f"- {ac}: {entry['statement']}  command: `{commands[entry['verifier']]}`")
    else:  # plans recorded before work items (Work 018)
        for a, d in zip(contract["acceptance"], draft["acceptance"], strict=False):
            lines.append(f"- {a['id']}: {a['statement']}  command: `{commands[d['verifier']]}`")
    for upstream_app, patch in upstream or []:
        lines += [
            "",
            f"The {upstream_app} change this builds on is already verified (do not redo it; "
            "it is applied in its own repository, not in this directory):",
            "```diff\n" + patch[-UPSTREAM_PATCH:] + "\n```",
        ]
    if feedback:
        lines += ["", "The previous attempt did not pass. The directory already contains "
                  "that attempt's changes. Fix what failed:"]  # fmt: skip
        for o in feedback:
            if o["outcome"] == "pass":
                continue
            tail = o["details"].get("stdout_tail", "") + o["details"].get("stderr_tail", "")
            lines.append(f"- {o['acceptance_id']} {o['outcome']}: {o['reason']}")
            for key in ("outside", "missing_sections", "unresolved_sources"):
                if o["details"].get(key):  # the design check says exactly what to fix
                    lines.append(f"  {key}: " + ", ".join(map(str, o["details"][key][:20])))
            if tail:
                lines.append("```\n" + tail[-FEEDBACK_TAIL:] + "\n```")
    return "\n".join(lines)


def implementer_role(plan: dict[str, Any], bundle: dict[str, Any] | None) -> list[str]:
    """The IMPLEMENTER role lines from the goal's fixed composition (D-089 S1).

    A composition whose prompt bundle ref is not a prompt bundle (planned before bundles
    existed) gets the built-in baseline, which is the same text.
    """
    ref = (plan.get("composition") or {}).get("ref")
    if not ref:
        return list(IMPLEMENTER_BASELINE)
    # adapted: the caller looked the bundle up; None is the RuntimeFault (store miss) branch
    if bundle is None:
        return list(IMPLEMENTER_BASELINE)
    return list(bundle["implementer"])  # adapted: validate() returns exactly this
