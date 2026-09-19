"""Host surface and SpecKit internalization (design/13 §1, design/02 §3, V3-035).

Two public entries stay ``work`` and ``design``. Every other skill is an internal
capability. SpecKit is lowered into ``packs/spec`` as a digest-pinned projection of the
repository sources; the projection is verified, never edited in both directions. The
retirement map records dispositions for grill-me/grilling/eli12 with live references and
``automatic_delete: false`` — deletion stays a V3-060 human decision.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import digest_bytes
from amplai_foundry.runtime.errors import Hold, RuntimeFault

PUBLIC_ENTRIES: tuple[str, ...] = ("work", "design")
SPEC_SKILLS: tuple[str, ...] = (
    "speckit-specify",
    "speckit-clarify",
    "speckit-plan",
    "taskify",
    "speckit-analyze",
    "speckit-converge",
    "speckit-checklist",
    "speckit-constitution",
    "speckit-implement",
    "speckit-taskstoissues",
)
AUX_UTILITIES: tuple[str, ...] = ("grill-me", "grilling", "eli12")
GOVERNING_DOCS: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "README.md", ".ai-team/README.md")
RETIREMENT: dict[str, dict[str, Any]] = {
    "grilling": {
        "disposition": "merge_questioning_rules_into_design_question_policy",
        "replacement": "design question policy (design/05 §2, one consolidated question)",
        "default_install": False,
    },
    "grill-me": {
        "disposition": "alias_of_grilling_retire_together",
        "replacement": "grilling disposition",
        "default_install": False,
    },
    "eli12": {
        "disposition": "utility_pack_excluded_from_default_install",
        "replacement": "optional utility pack selected by the user",
        "default_install": False,
    },
}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def read_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip().strip('"')
    return values


class HostSurface:
    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root).resolve()
        self.skills = self.root / ".agents" / "skills"
        self.mirror = self.root / ".claude" / "skills"
        if not self.skills.is_dir():
            raise RuntimeFault("SKILL_ROOT", "Repository has no .agents/skills source of truth")

    # -- inventory ---------------------------------------------------------------
    def inventory(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for directory in sorted(p for p in self.skills.iterdir() if p.is_dir()):
            skill = directory / "SKILL.md"
            if not skill.is_file():
                continue
            front = read_frontmatter(skill.read_text(encoding="utf-8"))
            codex = directory / "agents" / "openai.yaml"
            implicit = None
            if codex.is_file():
                m = re.search(r"allow_implicit_invocation:\s*(true|false)", codex.read_text())
                implicit = m.group(1) == "true" if m else None
            link = self.mirror / directory.name
            mirror_ok = (
                link.is_symlink()
                and link.readlink() == Path("../../.agents/skills") / directory.name
            )
            out[directory.name] = {
                "user_invocable": front.get("user-invocable") == "true",
                "disable_model_invocation": front.get("disable-model-invocation") == "true",
                "codex_implicit_invocation": implicit,
                "claude_mirror_exact": mirror_ok,
                "digest": digest_bytes(skill.read_bytes()),
            }
        return out

    def verify(self) -> dict[str, Any]:
        inventory = self.inventory()
        public = sorted(n for n, v in inventory.items() if v["user_invocable"])
        if public != sorted(PUBLIC_ENTRIES):
            raise Hold(
                "PUBLIC_SURFACE",
                "Exactly work and design may be user-invocable",
                details={"public": public},
            )
        exposed = sorted(
            n
            for n, v in inventory.items()
            if n not in PUBLIC_ENTRIES
            and n not in AUX_UTILITIES
            and (v["user_invocable"] or v["codex_implicit_invocation"] is True)
        )
        if exposed:
            raise Hold(
                "INTERNAL_EXPOSED",
                "Internal capabilities must not be user-invocable or implicitly invoked",
                details={"skills": exposed},
            )
        drift = sorted(n for n, v in inventory.items() if not v["claude_mirror_exact"])
        if drift:
            raise Hold(
                "MIRROR_DRIFT",
                "Claude mirror must be an exact symlink set of .agents/skills",
                details={"skills": drift},
            )
        return {
            "public_entries": public,
            "internal": sorted(
                n for n in inventory if n not in PUBLIC_ENTRIES and n not in AUX_UTILITIES
            ),
            "utilities": sorted(n for n in inventory if n in AUX_UTILITIES),
            "count": len(inventory),
        }

    # -- spec pack projection ------------------------------------------------------
    def _live_refs(self, name: str) -> list[str]:
        refs: list[str] = []
        pattern = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
        candidates = [self.root / doc for doc in GOVERNING_DOCS]
        candidates += sorted(self.skills.glob("*/SKILL.md"))
        for path in candidates:
            if not path.is_file():
                continue
            if path.parent.name == name and path.parent.parent == self.skills:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    refs.append(f"{path.relative_to(self.root)}:{number}")
        return refs

    def retirement_map(self) -> dict[str, Any]:
        entries = []
        for name in AUX_UTILITIES + SPEC_SKILLS:
            skill = self.skills / name / "SKILL.md"
            base: dict[str, Any] = RETIREMENT.get(
                name,
                {
                    "disposition": "internalized_into_packs_spec",
                    "replacement": "packs/spec/skills/" + name + "/SKILL.md",
                    "default_install": True,
                },
            )
            entries.append(
                {
                    "skill": name,
                    "path": str(skill.relative_to(self.root)),
                    "old_digest": digest_bytes(skill.read_bytes()) if skill.is_file() else None,
                    "owner": "Migration Lead (V3-060)",
                    "live_refs": self._live_refs(name),
                    "required_test_ids": ["T-092", "T-098"],
                    "automatic_delete": False,
                    **base,
                }
            )
        return {
            "schema_version": "1.0",
            "kind": "skill_retirement_map",
            "public_entries": list(PUBLIC_ENTRIES),
            "note": "Dispositions are recommendations for V3-060; nothing here deletes files.",
            "entries": entries,
        }

    def render_spec_pack(self, packs_root: str | Path) -> dict[str, Any]:
        pack = Path(packs_root) / "spec"
        if not (pack / "PACK.json").is_file():
            raise Hold("PACK_MANIFEST_MISSING", "packs/spec/PACK.json must exist before rendering")
        (pack / "skills").mkdir(parents=True, exist_ok=True)
        (pack / "context").mkdir(parents=True, exist_ok=True)
        surface: dict[str, Any] = {
            "schema_version": "1.0",
            "kind": "spec_surface_map",
            "source_of_truth": ".agents/skills (D-046); packs/spec is a generated projection",
            "files": {},
        }
        for name in SPEC_SKILLS:
            source = self.skills / name / "SKILL.md"
            if not source.is_file():
                raise Hold("SPEC_SOURCE_MISSING", f"Missing SpecKit source {name}")
            data = source.read_bytes()
            target = pack / "skills" / name / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            surface["files"][f"skills/{name}/SKILL.md"] = {
                "source": str(source.relative_to(self.root)),
                "digest": digest_bytes(data),
            }
        (pack / "context" / "host-surface.json").write_text(
            json.dumps(
                {
                    "public_entries": list(PUBLIC_ENTRIES),
                    "host_syntax": {"claude": "/work, /design", "codex": "$work, $design"},
                    "internal_capabilities": list(SPEC_SKILLS),
                    "kit_payload_projection": (
                        "V3-050 (S12) generates .agents/skills into the Kit payload"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        (pack / "context" / "retirement-map.json").write_text(
            json.dumps(self.retirement_map(), ensure_ascii=False, indent=2) + "\n"
        )
        (pack / "context" / "surface-map.json").write_text(
            json.dumps(surface, ensure_ascii=False, indent=2) + "\n"
        )
        return surface

    def verify_spec_pack(self, packs_root: str | Path) -> dict[str, Any]:
        pack = Path(packs_root) / "spec"
        map_path = pack / "context" / "surface-map.json"
        if not map_path.is_file():
            raise Hold("SURFACE_MAP_MISSING", "packs/spec has no surface-map.json; render first")
        surface = json.loads(map_path.read_text())
        drifted: list[dict[str, str]] = []
        for relative, entry in surface["files"].items():
            source = self.root / entry["source"]
            projected = pack / relative
            source_digest = digest_bytes(source.read_bytes()) if source.is_file() else None
            projected_digest = digest_bytes(projected.read_bytes()) if projected.is_file() else None
            if source_digest != entry["digest"] or projected_digest != entry["digest"]:
                drifted.append(
                    {
                        "file": relative,
                        "pinned": entry["digest"],
                        "source": source_digest or "missing",
                        "projected": projected_digest or "missing",
                    }
                )
        if drifted:
            raise Hold(
                "SURFACE_DRIFT",
                "packs/spec projection differs from .agents/skills; re-render, never hand-edit",
                details={"drifted": drifted},
            )
        return {"files": len(surface["files"]), "status": "in_sync"}
