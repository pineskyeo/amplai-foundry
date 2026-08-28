---
name: speckit-converge
description: Compare implementation with the spec, plan, generated tasks.md, and taskify manifests, then capture remaining work in the manifest source of truth. Use after implementation to recover unfinished scope.
---

# Codex Adapter

Read [the canonical workflow](../../../.claude/skills/speckit-converge/SKILL.md) completely, then follow it.

In Codex, invoke skills with `$speckit-*` and `$taskify`. Treat `/speckit-*` and `/taskify` in the canonical workflow as the equivalent Claude invocation spelling. Update taskify manifests first and regenerate `tasks.md`; never append directly to the generated file.
