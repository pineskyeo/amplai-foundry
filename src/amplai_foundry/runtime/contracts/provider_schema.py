"""Provider transport schemas are projections; normative validation stays local."""

from __future__ import annotations

from copy import deepcopy

from ..errors import RuntimeFault
from .registry import BASE


class ProviderSchema:
    def __init__(self, contracts):
        self.contracts = contracts

    def inline(self, schema):
        def resolve(node, stack=()):
            if isinstance(node, list):
                return [resolve(x, stack) for x in node]
            if not isinstance(node, dict):
                return node
            if "$ref" in node:
                ref = node["$ref"]
                if not ref.startswith(BASE):
                    raise RuntimeFault(
                        "PROVIDER_SCHEMA_REF",
                        "Only pinned offline AMPLAI schema references may be expanded",
                    )
                if ref in stack:
                    raise RuntimeFault(
                        "PROVIDER_SCHEMA_CYCLE",
                        "Provider projection cannot expand recursive references",
                    )
                path, _, fragment = ref.partition("#")
                name = path.removeprefix(BASE).removesuffix(".schema.json")
                target = self.contracts.definitions[name]
                for part in fragment.lstrip("/").split("/") if fragment else []:
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                return resolve(target, stack + (ref,))
            return {
                k: resolve(v, stack)
                for k, v in node.items()
                if k not in {"$schema", "$id", "description", "title", "$defs"}
            }

        return resolve(deepcopy(schema))

    def structured_output(self, schema):
        result = self.inline(schema)

        def normalize(node):
            if isinstance(node, dict):
                # Keep normative nullable/required semantics; reject schemas that would
                # need permission or identity semantics changed for a vendor subset.
                if node.get("type") == "object":
                    props = node.get("properties", {})
                    if set(node.get("required", [])) != set(props):
                        raise RuntimeFault(
                            "PROVIDER_SCHEMA_OPTIONAL",
                            "Explicit nullable fields are required for this structured-output profile",
                        )
                    node["additionalProperties"] = False
                for value in node.values():
                    normalize(value)
            elif isinstance(node, list):
                for value in node:
                    normalize(value)

        normalize(result)
        return result
