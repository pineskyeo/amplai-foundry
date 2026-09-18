"""Frontend/docs actual-render acceptance (design/14 §4-§5, V3-059).

Every final page is inspected against a three-way digest chain — release manifest →
view manifest → bytes on disk — plus structural checks that need no browser. The
browser layer (screenshots, geometry, keyboard flow) is run only when a qualified
renderer is installed; otherwise it is recorded as ``BROWSER_UNAVAILABLE`` and never
counted as a pass. Golden baselines are human-approved revisions kept in a registry the
builder cannot rewrite (T-059).
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import digest_bytes, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault

_SCRIPT = re.compile(r"<script\b", re.I)
_EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", re.I)
_CSP = re.compile(r"""<meta[^>]+http-equiv=["']Content-Security-Policy["']""", re.I)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headings: list[tuple[int, str]] = []
        self.ids: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self.lang: str | None = None
        self.title = ""
        self.has_main = False
        self.skip_link = False
        self._heading: int | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "main":
            self.has_main = True
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = a.get("href") or ""
            self.links.append(href)
            if "skip" in (a.get("class") or "") and href.startswith("#"):
                self.skip_link = True
        if tag == "img":
            self.images.append(a.get("src") or "")
        element_id = a.get("id")
        if element_id:
            self.ids.append(element_id)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = int(tag[1])
            self.headings.append((self._heading, ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._heading is not None and self.headings:
            level, text = self.headings[-1]
            self.headings[-1] = (level, (text + data).strip())


def inspect_page(path: Path) -> dict[str, Any]:
    """Structural evidence for one rendered page (design/14 §5: structure vs visual)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    outline = _Outline()
    outline.feed(text)
    findings: list[str] = []
    if not outline.title.strip():
        findings.append("missing_title")
    if not outline.lang:
        findings.append("missing_lang")
    if not outline.has_main:
        findings.append("missing_main")
    if not outline.skip_link:
        findings.append("missing_skip_link")
    levels = [lvl for lvl, _ in outline.headings]
    if not levels or levels[0] != 1:
        findings.append("first_heading_not_h1")
    if any(b - a > 1 for a, b in pairwise(levels)):
        findings.append("heading_level_jump")
    dupes = {i for i in outline.ids if outline.ids.count(i) > 1}
    if dupes:
        findings.append("duplicate_ids:" + ",".join(sorted(dupes)))
    if _SCRIPT.search(text):
        findings.append("script_present")
    if _EXTERNAL.search(text):
        findings.append("external_resource")
    if not _CSP.search(text):
        findings.append("missing_csp")
    anchors = [h for h in outline.links if h.startswith("#")]
    broken = sorted({h[1:] for h in anchors if h[1:] and h[1:] not in outline.ids})
    if broken:
        findings.append("broken_anchors:" + ",".join(broken))
    return {
        "page": path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "title": outline.title.strip(),
        "lang": outline.lang,
        "headings": outline.headings,
        "images": outline.images,
        "findings": findings,
        "outcome": "pass" if not findings else "fail",
    }


class RenderAcceptance:
    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root).resolve()

    # -- digest chain ---------------------------------------------------------
    def chain(self, view_dir: str | Path) -> dict[str, Any]:
        view = self.root / view_dir
        manifest_path = view / "manifest.json"
        if not manifest_path.is_file():
            raise Hold("VIEW_MANIFEST_MISSING", "Rendered view has no manifest.json")
        manifest = json.loads(manifest_path.read_bytes())
        drift: list[dict[str, str]] = []
        for entry in manifest["files"]:
            path = view / entry["path"]
            actual = sha256_file(path) if path.is_file() else "missing"
            if actual != entry["sha256"]:
                drift.append({"path": entry["path"], "manifest": entry["sha256"], "actual": actual})
        extra = sorted(
            p.name
            for p in view.iterdir()
            if p.is_file()
            and p.name != "manifest.json"
            and p.name not in {e["path"] for e in manifest["files"]}
        )
        release = self._release_for(manifest)
        release_drift: list[dict[str, str]] = []
        if release is not None:
            by_id = {d["doc_id"]: d for d in release["documents"]}
            for doc in manifest["documents"]:
                r = by_id.get(doc["doc_id"])
                if r is None:
                    release_drift.append({"doc_id": doc["doc_id"], "reason": "not_in_release"})
                    continue
                for key in ("source_sha256", "snapshot_sha256", "review_sha256"):
                    if r[key] != doc[key]:
                        release_drift.append({"doc_id": doc["doc_id"], "reason": key})
            if manifest.get("verification_sha256") != release["verification"]["sha256"]:
                release_drift.append({"doc_id": "*", "reason": "verification_sha256"})
        return {
            "view": str(Path(view_dir)),
            "release_id": manifest.get("release_id"),
            "audience": manifest.get("audience"),
            "files": len(manifest["files"]),
            "byte_drift": drift,
            "untracked_files": extra,
            "release_drift": release_drift,
            "outcome": "pass" if not (drift or extra or release_drift) else "fail",
        }

    def _release_for(self, manifest: dict[str, Any]) -> dict[str, Any] | None:
        release_id = manifest.get("release_id")
        if not release_id:
            return None
        policy = self.root / ".ai-team" / "policy" / "documentation.json"
        if not policy.is_file():
            return None
        for entry in json.loads(policy.read_bytes()).get("offline_views", {}).get("entries", []):
            candidate = self.root / entry["release_manifest"]
            if candidate.is_file():
                release: dict[str, Any] = json.loads(candidate.read_bytes())
                if release.get("release_id") == release_id:
                    return release
        return None

    # -- structure --------------------------------------------------------------
    def pages(self, view_dir: str | Path) -> list[dict[str, Any]]:
        view = self.root / view_dir
        manifest = json.loads((view / "manifest.json").read_bytes())
        return [
            inspect_page(view / e["path"]) for e in manifest["files"] if e["path"].endswith(".html")
        ]

    # -- browser layer (qualified renderer only) ----------------------------------
    def browser_layer(self, view_dir: str | Path, *, renderer: Any | None = None) -> dict[str, Any]:
        if renderer is None:
            try:
                import playwright  # noqa: F401
            except ImportError:
                return {
                    "status": "BROWSER_UNAVAILABLE",
                    "outcome": "not_run",
                    "reason": (
                        "playwright renderer is not installed on this host; not a skipped PASS"
                    ),
                    "pages": [],
                }
            from amplai_foundry.verification.runtime.visual import BrowserRenderer

            renderer = BrowserRenderer()
        view = self.root / view_dir
        manifest = json.loads((view / "manifest.json").read_bytes())
        results = []
        for entry in manifest["files"]:
            if not entry["path"].endswith(".html"):
                continue
            html = (view / entry["path"]).read_bytes()
            out = view.parent / (view.name + "-render") / entry["path"].replace(".html", "")
            try:
                for width, height in ((1280, 900), (390, 844)):
                    r = renderer.render(html, out / f"{width}x{height}", width=width, height=height)
                    results.append(
                        {
                            "page": entry["path"],
                            "viewport": [width, height],
                            "outcome": r["outcome"],
                            "screenshot_digest": r["screenshot_digest"],
                        }
                    )
            except Hold as exc:
                return {
                    "status": exc.code,
                    "outcome": "not_run",
                    "reason": exc.message,
                    "pages": results,
                }
        return {
            "status": "rendered",
            "outcome": "pass" if all(r["outcome"] == "pass" for r in results) else "fail",
            "pages": results,
        }

    # -- golden governance -------------------------------------------------------------
    def golden_guard(
        self, registry_path: str | Path, actual: dict[str, str], *, actor_role: str
    ) -> dict[str, Any]:
        """Baselines are human-approved; a builder may neither pass by rewriting them nor by
        skipping the compare (T-059, INV-15)."""
        registry_file = self.root / registry_path
        if not registry_file.is_file():
            raise Hold("GOLDEN_REGISTRY_MISSING", "No human-approved golden registry")
        registry = json.loads(registry_file.read_bytes())
        if registry.get("approved_by_role") != "human" or not registry.get("approval_ref"):
            raise Hold("GOLDEN_UNAPPROVED", "Golden registry lacks a human approval reference")
        recorded = registry.get("registry_sha256")
        body = {k: v for k, v in registry.items() if k != "registry_sha256"}
        if (
            recorded
            != digest_bytes(
                json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )[7:]
        ):
            raise Hold("GOLDEN_TAMPERED", "Golden registry bytes differ from its sealed digest")
        if actor_role != "verifier":
            raise RuntimeFault("GOLDEN_ACTOR", "Only the trusted verifier compares against goldens")
        mismatches = sorted(k for k, v in registry["goldens"].items() if actual.get(k) != v)
        missing = sorted(k for k in registry["goldens"] if k not in actual)
        return {
            "outcome": "pass" if not mismatches else "fail",
            "mismatches": mismatches,
            "missing_actuals": missing,
            "approval_ref": registry["approval_ref"],
            "aesthetic_acceptance": "not_assessed",
            "compared_at": now(),
        }

    @staticmethod
    def seal_registry(goldens: dict[str, str], *, approval_ref: str) -> dict[str, Any]:
        body = {
            "schema_version": "1.0",
            "approved_by_role": "human",
            "approval_ref": approval_ref,
            "goldens": dict(sorted(goldens.items())),
        }
        sealed = digest_bytes(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )[7:]
        return {**body, "registry_sha256": sealed}

    # -- suite ------------------------------------------------------------------------
    def run(self, views: list[str], *, renderer: Any | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {"schema_version": "3.0.0", "views": [], "generated_at": now()}
        for view in views:
            chain = self.chain(view)
            pages = self.pages(view)
            browser = self.browser_layer(view, renderer=renderer)
            report["views"].append({"chain": chain, "pages": pages, "browser": browser})
        structural = all(
            v["chain"]["outcome"] == "pass" and all(p["outcome"] == "pass" for p in v["pages"])
            for v in report["views"]
        )
        browser_ok = all(v["browser"]["outcome"] == "pass" for v in report["views"])
        report["structural_outcome"] = "pass" if structural else "fail"
        report["browser_outcome"] = (
            "pass"
            if browser_ok
            else (
                "not_run"
                if all(v["browser"]["outcome"] == "not_run" for v in report["views"])
                else "fail"
            )
        )
        report["outcome"] = (
            "pass"
            if structural and browser_ok
            else (
                "inconclusive" if structural and report["browser_outcome"] == "not_run" else "fail"
            )
        )
        report["qualification"] = (
            "structure/digest chain locally verified; browser layer requires a qualified renderer"
        )
        return report
