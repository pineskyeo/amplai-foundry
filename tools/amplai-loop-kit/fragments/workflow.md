<!-- AMPLAI-ASYNC-BEGIN -->
## Decision & Async Cross-App Extension

연결 순서:

```text
Goal/Contract
  → Primary Agent
  → Open Question
  → Evidence Resolver (DIRECT|LOCAL|PARALLEL)
  → Decision Resolver (AUTO|CHALLENGE|HUMAN)
  → Implement/Verify
  → Decision/Evidence 기록
  → cross-app Work가 있으면 WAITING/READY dependency loop
```

공용 Project Store의 상태가 세션/agent 대화보다 우선한다. Handoff는 target Work의 projection이며,
Supervisor는 READY/lease/dependency/app capacity만 다룬다. V2에서는 한 앱 한 active Work를 기본으로
하고 global planning, workspace/merge scheduling, publish/deploy는 구현하지 않는다.
<!-- AMPLAI-ASYNC-END -->
