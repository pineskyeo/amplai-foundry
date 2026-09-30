"""Cross-app check: summarize() must accept what amplai-foundry's `amplai ops version` prints.

Run by AMPLAI's integration check with both apps read-only under /amplai-input/apps/<app> and
PYTHONPATH pointing at amplai-foundry/src and amplai-demo-app.
"""

import json
import subprocess
import sys

from demo_app.version_report import summarize

printed = subprocess.run(
    [
        sys.executable,
        "-c",
        "from amplai_foundry.runtime.cli import app; app(['ops', 'version'])",
    ],
    capture_output=True,
    text=True,
    check=True,
).stdout
info = json.loads(printed)
line = summarize(info)
assert info["package_version"] in line, line
print(line)
