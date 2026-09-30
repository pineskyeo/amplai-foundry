"""Bundles (kits): products that are sold as a set of other products, possibly nested."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .errors import ParseError
from .inventory import Inventory
from .models import check_sku

_QUANTITY = re.compile(r"x([0-9]+)")


def _component(text: str, number: int) -> tuple[str, int]:
    tokens = text.split()
    if not tokens or len(tokens) > 2:
        raise ParseError(f"line {number}: bad component {text.strip()!r}")
    sku = check_sku(tokens[0])
    if len(tokens) == 1:
        return sku, 1
    match = _QUANTITY.fullmatch(tokens[1])
    if match is None or int(match.group(1)) < 1:
        raise ParseError(f"line {number}: bad quantity {tokens[1]!r}")
    return sku, int(match.group(1))


def parse_bundles(text: str) -> dict[str, dict[str, int]]:
    """``{bundle sku: {component sku: quantity}}`` from lines such as
    ``KIT-100 = MUG-001 x2 + TEA-010``."""
    bundles: dict[str, dict[str, int]] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, tail = line.partition("=")
        if not sep or "=" in tail:
            raise ParseError(f"line {number}: expected 'SKU = component + ...'")
        try:
            sku = check_sku(head.strip())
        except ParseError as exc:
            raise ParseError(f"line {number}: {exc}") from exc
        if sku in bundles:
            raise ParseError(f"line {number}: bundle {sku} defined twice")
        components: dict[str, int] = {}
        for part in tail.split("+"):
            try:
                component, quantity = _component(part, number)
            except ParseError as exc:
                if str(exc).startswith("line "):
                    raise
                raise ParseError(f"line {number}: {exc}") from exc
            if component in components:
                raise ParseError(f"line {number}: {component} listed twice in {sku}")
            components[component] = quantity
        bundles[sku] = components
    return bundles


def flatten(bundles: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Every bundle as quantities of base (non-bundle) SKUs, nested bundles expanded."""
    done: dict[str, dict[str, int]] = {}

    def expand(sku: str, path: list[str]) -> dict[str, int]:
        if sku in path:
            cycle = path[path.index(sku) :] + [sku]
            raise ParseError("bundle cycle: " + " -> ".join(cycle))
        if sku in done:
            return done[sku]
        total: dict[str, int] = {}
        for component, quantity in bundles[sku].items():
            if component in bundles:
                for base, count in expand(component, [*path, sku]).items():
                    total[base] = total.get(base, 0) + count * quantity
            else:
                total[component] = total.get(component, 0) + quantity
        done[sku] = total
        return total

    return {sku: dict(expand(sku, [])) for sku in bundles}


def max_buildable(
    bundles: Mapping[str, Mapping[str, int]], sku: str, inventory: Inventory
) -> int:
    """How many whole ``sku`` bundles the available stock of their base SKUs allows."""
    if sku not in bundles:
        raise ParseError(f"no bundle {sku}")
    base = flatten(bundles)[sku]
    return min(inventory.available(component) // quantity for component, quantity in base.items())
