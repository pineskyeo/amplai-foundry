"""Versioned prompt bundles: the class-A prompt surface of a HarnessComposition (D-089, S1).

A composition pins ``prompt_bundle_ref`` (design-reference/design/16_META_HARNESS.md:14). The
IMPLEMENTER role text of the execution prompt comes from that bundle, so a candidate prompt is a
new bundle in a new composition and a running goal keeps the prompt its composition fixed. Only
the role lines are the surface; objective, scope, acceptance and feedback stay built by code.

Two placeholders are filled by exact replacement (never ``str.format``): ``{app_id}`` and
``{base_commit}``. Any other brace in a bundle is refused, so a bundle cannot reach other data.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import RuntimeFault

KIND = "prompt-bundle"
BASELINE_ID = "implementer-baseline"
PLACEHOLDERS = ("{app_id}", "{base_commit}")
MAX_LINES, MAX_LINE = 20, 4000

# Byte-for-byte the IMPLEMENTER role text the loop built before bundles existed (Work 018).
IMPLEMENTER_BASELINE: tuple[str, ...] = (
    "You are the IMPLEMENTER for AMPLAI. The current directory is a copy of the "
    "{app_id} repository at commit {base_commit} (no .git, network limited).",
    "Make the change below. Do not commit. When you are done, run the acceptance "
    "commands yourself; the result is judged by running them on a clean copy with "
    "exactly your file changes applied.",
)


def bundle(bundle_id: str, implementer: tuple[str, ...] | list[str], source: str) -> dict[str, Any]:
    value = {
        "bundle_id": bundle_id,
        "surface": "implementer_role",
        "implementer": list(implementer),
        "source": source,
    }
    validate(value)
    return value


def validate(value: Any) -> list[str]:
    """The implementer role lines of a well-formed bundle."""
    if (
        not isinstance(value, dict)
        or set(value) != {"bundle_id", "surface", "implementer", "source"}
        or value["surface"] != "implementer_role"
        or not isinstance(value["implementer"], list)
        or not 1 <= len(value["implementer"]) <= MAX_LINES
    ):
        raise RuntimeFault("PROMPT_BUNDLE", "Unknown prompt bundle")
    for line in value["implementer"]:
        if not isinstance(line, str) or not line.strip() or len(line) > MAX_LINE:
            raise RuntimeFault("PROMPT_BUNDLE", "Bundle lines are nonempty bounded strings")
        rest = line
        for p in PLACEHOLDERS:
            rest = rest.replace(p, "")
        if re.search(r"[{}]", rest):
            raise RuntimeFault("PROMPT_BUNDLE", "Only {app_id} and {base_commit} may be filled")
    return list(value["implementer"])


def render(lines: list[str] | tuple[str, ...], *, app_id: str, base_commit: str) -> list[str]:
    return [
        line.replace("{app_id}", app_id).replace("{base_commit}", base_commit) for line in lines
    ]
