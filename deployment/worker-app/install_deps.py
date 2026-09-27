"""Install a pyproject's dependencies (+ extras) into /opt/app-venv; the package itself is not
installed, tests import it from the workspace (PYTHONPATH=src)."""

import subprocess
import sys
import tomllib

extras = [e for e in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if e]
with open("/opt/app/pyproject.toml", "rb") as f:
    project = tomllib.load(f)["project"]
deps = list(project.get("dependencies", []))
for extra in extras:
    deps += project.get("optional-dependencies", {}).get(extra, [])
subprocess.run(["/opt/app-venv/bin/pip", "install", "--no-cache-dir", *deps], check=True)
print("installed", len(deps), "requirements")
