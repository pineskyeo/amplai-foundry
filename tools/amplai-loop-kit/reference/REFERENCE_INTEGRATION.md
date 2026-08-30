# Reference Integration

This package integrates with an existing AMPLAI Loop V2 repository instead of creating a parallel
framework. It preserves the host-neutral workflow and adds Claude Code/Codex adapters at the edge.

## Existing capabilities retained

- `.ai-team` Loop V2 runtime and policies
- `design` and `work` as the only public development meanings
- Knowledge Readiness, Context Pack, Evidence Trace, deterministic verifier, repair, converge, review
- existing Claude permissions/hooks and existing Codex hook fields

## Added owned runtime files

```text
scripts/amplai_runtime.py
scripts/amplai.py
scripts/amplai_supervisor.py
scripts/amplai_hook.py
.ai-team/runtime/async-policy.json
.ai-team/runtime/schemas/*.json
.ai-team/runtime/DECISION_ASYNC_PROTOCOL.md
.ai-team/runtime/LOCAL_SUPERVISOR.md
.ai-team/runtime/INSTALLATION.md
tests/ai/test_amplai_async_runtime.py
tests/ai/test_amplai_kit_regressions.py
tests/ai/test_amplai_kit_installer.py
```

Kit 원본은 `tools/amplai-loop-kit/`에 벤더링한다. installer 테스트가 이 경로를 쓰고,
loop policy의 `amplai-decision-async-runtime` rule이 이 경로를 high-risk로 잡는다.

## Additive integration points

- `work`: Evidence/Decision resolution, cross-app Work creation, durable terminal transition
- `design`: DRAFT CR/Work/Question design without auto-execution
- autonomy policy: machine-enforced AUTO/CHALLENGE/HUMAN minimum
- workflow: async cross-app extension
- handoff: Project Store projection for cross-app delivery; checkpoint for same-app continuity
- Loop policy JSON: high-risk route and automatic-action prohibitions
- Claude settings: additive SessionStart/SessionEnd; existing permissions/hooks preserved
- Codex hooks: additive SessionStart/SessionEnd; existing fields/hooks preserved
- skills: `.agents/skills` is the single canonical tree; `.claude/skills` is an exact symlink mirror

## Host adapter boundary

```text
Claude Code  /work, /design ─┐
                             ├─ host-neutral Contract / Work / Evidence / Decision
Codex        $work, $design ─┘
```

Runner-specific command construction, result/session parsing, lifecycle hook output and invocation spelling
belong at the adapter edge. Product/domain logic must not import Claude/Codex concepts.

## Architecture boundary

```text
Current
  Human → App AMPLAI Runtime (design, work)
                    ↕
              Git Project Store
                    ↕
          deterministic Local Supervisor

Future
  Human → Hermes → Global AMPLAI
                    ↓
       same Project Store / Work protocol
                    ↓
             App AMPLAI Runtimes
```

The Local Supervisor is intentionally not the V3 distributed execution fabric. It does not plan goals,
coordinate merges, publish Git state, deploy, or perform production actions.
