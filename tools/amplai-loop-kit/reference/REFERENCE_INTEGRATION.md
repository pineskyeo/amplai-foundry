# Reference Integration

This package was designed against an existing AMPLAI Loop V2 repository rather than as a
parallel framework.  It adds to what that repository already has instead of replacing it.

## Existing capabilities retained

- `.ai-team` Loop V2 runtime and policies
- `/design` and `/work` as the only public development entry points
- Knowledge Readiness, Context Pack, Evidence Trace, deterministic verifier, repair, converge, review
- existing Claude permissions and existing SessionStart/SessionEnd hooks

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
loop policy 의 `amplai-decision-async-runtime` rule 이 이 경로를 high-risk 로 잡는다.

## Additive integration points

- `/work`: Evidence/Decision resolution, cross-app Work creation, durable terminal transition
- `/design`: DRAFT CR/Work/Question design without auto-execution
- autonomy policy: machine-enforced AUTO/CHALLENGE/HUMAN minimum
- workflow: async cross-app extension
- handoff: Project Store projection for cross-app delivery; session checkpoint for same-app continuity
- Loop policy JSON: high-risk route for runtime/installer changes and automatic-action prohibitions
- Claude settings: additive SessionStart and SessionEnd commands; existing permissions/hooks preserved

## Architecture boundary

```text
Current
  Human → App AMPLAI Runtime (/design, /work)
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
