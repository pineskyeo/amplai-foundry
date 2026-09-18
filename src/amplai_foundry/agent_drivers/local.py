"""Reference executable worker. It is explicitly a deterministic driver, not an LLM."""

from __future__ import annotations

import json
from pathlib import Path

from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Conflict, Hold
from amplai_foundry.sandbox.local import DataSandbox


class RecipeDriver:
    driver_id = "local-recipe"
    version = "3.0.0"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def run(self, runtime, worker, dispatch: dict, recipe: dict) -> dict:
        run_id = dispatch["run_id"]
        lease = dispatch["lease"]
        workspace = self.root / run_id
        sandbox = DataSandbox(workspace)
        session = "recipe:" + run_id
        runtime.start(worker, dispatch, session)
        fingerprint = digest(recipe)
        journal = self.root / (run_id + ".journal.json")
        if journal.exists():
            saved = json.loads(journal.read_text())
            if saved["recipe_digest"] != fingerprint:
                raise Conflict("DRIVER_REPLAY", "Dispatch recipe changed")
            if saved["status"] != "completed":
                raise Hold(
                    "DRIVER_RECOVERY",
                    "Interrupted declarative execution needs reconciliation before replay",
                )
        else:
            journal.write_bytes(
                canonical({"status": "started", "recipe_digest": fingerprint, "session": session})
            )
            sandbox.execute(recipe["operations"])
            journal.write_bytes(
                canonical({"status": "completed", "recipe_digest": fingerprint, "session": session})
            )
        outputs = {}
        for name, port in recipe["outputs"].items():
            data = sandbox.read(port["path"])
            outputs[name] = runtime.artifacts.admit(
                worker.scope, data, port["media_type"], trust="worker"
            )
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microunits": 0,
            "currency": "USD",
            "status": "measured",
            "source_ref": None,
        }
        return runtime.output_ready(
            worker,
            run_id,
            lease["lease_id"],
            lease["fencing_token"],
            outputs,
            usage=usage,
            process_stopped=True,
        )

    def probe(self) -> dict:
        return {
            "driver_id": self.driver_id,
            "version": self.version,
            "kind": "deterministic",
            "native_delegation": False,
        }
