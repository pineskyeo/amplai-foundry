"""Measure the local egress profile and write its qualification ref.

Three probes run in throwaway containers on the profile's internal network with the same
hardening flags the sandbox uses (read-only root, cap-drop ALL, unprivileged uid). This is a
dedicated measurement, not the generated-code sandbox: ContainerSandbox itself refuses the
network until the ref this script writes says ``pass``.

    .venv/bin/python scripts/egress_qualify.py          # write local-egress-qualification.json
    .venv/bin/python scripts/egress_qualify.py --check  # re-probe, exit 1 unless pass, no write
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from amplai_foundry.sandbox.egress import EgressProfile, qualification_ref

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "deployment" / "local-egress.json"
CONTAINER = REPO / "deployment" / "local-container.json"
OUT = REPO / "deployment" / "local-egress-qualification.json"


def _curl(
    image: str, network: str, url: str, env: dict[str, str] | None, uid: str
) -> dict[str, Any]:
    argv = ["docker", "run", "--rm", "--network", network, "--read-only", "--cap-drop=ALL"]
    argv += ["--security-opt=no-new-privileges", "--user", uid, "--pids-limit", "32"]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    argv += [image, "curl", "-sS", "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}", url]
    run = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    return {
        "command": " ".join(argv),
        "rc": run.returncode,
        "http_code": run.stdout.strip()[-3:],
        "stderr": run.stderr.strip()[-200:],
    }


def probes(profile: EgressProfile, image: str, uid: str) -> dict[str, dict[str, Any]]:
    allowed = profile.allow[0]
    allowed_url = f"https://{allowed.rsplit(':', 1)[0]}/"
    denied_url = "https://example.com/"
    direct = _curl(image, profile.network, allowed_url, None, uid)
    tunnel = _curl(image, profile.network, allowed_url, profile.env(), uid)
    refused = _curl(image, profile.network, denied_url, profile.env(), uid)
    return {
        "direct_egress_denied": {
            "outcome": "pass" if direct["rc"] != 0 else "fail",
            "observation": f"rc={direct['rc']} http={direct['http_code']} {direct['stderr']}",
            "command": direct["command"],
        },
        "allowed_target_tunnels": {
            # any HTTP status proves the tunnel; the allowlisted origin decides what it answers
            "outcome": "pass" if tunnel["rc"] == 0 and tunnel["http_code"].isdigit() else "fail",
            "observation": f"rc={tunnel['rc']} http={tunnel['http_code']} {tunnel['stderr']}",
            "command": tunnel["command"],
        },
        "denied_target_refused": {
            "outcome": "pass" if refused["rc"] == 56 and "403" in refused["stderr"] else "fail",
            "observation": f"rc={refused['rc']} http={refused['http_code']} {refused['stderr']}",
            "command": refused["command"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    profile = EgressProfile.load(PROFILE)
    container = json.loads(CONTAINER.read_text())
    uid = f"{container['uid']}:{container['gid']}"
    checked_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = qualification_ref(
        profile,
        probes(profile, container["image"], uid),
        checked_at=checked_at,
        evidence=str(OUT.relative_to(REPO)),
    )
    ref["image"] = container["image"]
    ref["method"] = "scripts/egress_qualify.py: 3 curl probes in hardened throwaway containers"
    ref["qualification"] = "local development egress profile; not a production qualification"
    if not args.check:
        OUT.write_text(json.dumps(ref, indent=2) + "\n")
    print(json.dumps({k: ref[k] for k in ("outcome", "egress_profile", "checked_at")}))
    for check in ref["checks"]:
        print(f"  {check['name']:26} {check['outcome']:6} {check['observation']}")
    return 0 if ref["outcome"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
