---
name: speckit-implement
description: Implement an explicitly approved spec-kit plan by executing generated tasks.md entries against their taskify manifest contracts. Use only when the user directly requests implementation.
---

# Codex Adapter

Read [the canonical workflow](../../../.claude/skills/speckit-implement/SKILL.md) completely, then follow it.

In Codex, invoke skills with `$speckit-*` and `$taskify`. Treat `/speckit-*` and `/taskify` in the canonical workflow as the equivalent Claude invocation spelling. Validate manifests through `.agents/skills/taskify/scripts/validate_task_manifest.py`, regenerate `tasks.md`, and read each referenced YAML contract before implementation.
