"""Small HTTP client shared by CLI and Hermes. Transport retries do not create work."""
from __future__ import annotations
import os
from pathlib import Path
from urllib.parse import quote
from amplai_foundry.agent_drivers.http import BoundHttp
from amplai_foundry.runtime.contracts.identity import new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault

class AmplaiClient:
    def __init__(self, base_url: str, token: str, *,transport=None):
        if not token or len(token) < 32: raise Hold("CLIENT_AUTH", "Set AMPLAI_TOKEN_FILE or an explicit server credential")
        self.http = BoundHttp(base_url, headers={"Authorization": "Bearer " + token}, allow_local=True, transport=transport, timeout=120)
    def call(self, method: str, path: str, *,payload=None, key=None, version=None):
        if "://" in path or path.startswith("//") or ".." in path.split("/"):
            raise RuntimeFault("CLIENT_PATH", "Request cannot leave the configured control plane")
        headers = {}
        if method != "GET": headers["Idempotency-Key"] = key or new_id("request")
        if version is not None: headers["If-Match"] = '"' + str(version) + '"'
        kwargs = {"headers": headers}
        if payload is not None: kwargs["json"] = payload
        # No retry after a timeout: return the same request key for explicit reconciliation.
        try:
            response = self.http.client.request(method, path.lstrip("/"), **kwargs)
        except Exception as exc:
            raise Hold("CLIENT_OUTCOME_UNKNOWN", "Request outcome is unknown; reuse the same idempotency key after checking the goal/command receipt", details={"key": headers.get("Idempotency-Key")}) from exc
        if response.status_code >= 300:
            try: error = response.json()
            except ValueError: error = {"code": "HTTP_ERROR", "message": "Control plane returned HTTP " + str(response.status_code)}
            raise RuntimeFault(error.get("code", "API_ERROR"), error.get("message", "Request rejected"), outcome=error.get("outcome", "rejected"))
        return response.json()
    def submit(self, text: str, *,mode="work", target_hints=None, key=None, external_message_id=None):
        return self.call("POST", "api/v3/intents", payload={"text": text, "mode": mode, "target_hints": target_hints or [], "external_message_id": external_message_id}, key=key)
    def goal(self, goal_id): return self.call("GET", "api/v3/goals/" + quote(goal_id, safe=""))
    def hermes_submit(self, text, *,workspace_id, channel_id, message_id, mode="work"):
        from amplai_foundry.runtime.contracts.identity import digest
        # Transport identity deduplicates delivery; app resolution remains server-owned.
        identity = digest({"workspace": workspace_id, "channel": channel_id, "message": message_id})
        return self.submit(text, mode=mode, key="hermes:" + identity, external_message_id=message_id)
    def close(self): self.http.close()
