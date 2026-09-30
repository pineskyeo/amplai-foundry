"""Plan the shortest upgrade path between two versions."""

from __future__ import annotations

from collections import deque


class NoUpgradePath(LookupError):
    """The target version cannot be reached from the current one."""


def _distances_to(target: str, steps: dict[str, list[str]]) -> dict[str, int]:
    """Number of edges from each version to ``target`` (versions that cannot reach it are absent)."""
    reverse: dict[str, set[str]] = {}
    for source, nexts in steps.items():
        for nxt in nexts:
            reverse.setdefault(nxt, set()).add(source)
    dist = {target: 0}
    queue = deque([target])
    while queue:
        node = queue.popleft()
        for prev in reverse.get(node, ()):
            if prev not in dist:
                dist[prev] = dist[node] + 1
                queue.append(prev)
    return dist


def plan_upgrade(current: str, target: str, steps: dict[str, list[str]]) -> list[str]:
    if current == target:
        return [current]
    dist = _distances_to(target, steps)
    if current not in dist:
        raise NoUpgradePath(f"no upgrade path from {current} to {target}")
    path = [current]
    while path[-1] != target:
        here = path[-1]
        path.append(min(nxt for nxt in steps[here] if dist.get(nxt) == dist[here] - 1))
    return path
