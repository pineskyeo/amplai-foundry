# V2 Escalation Policy

동일 Work/Slice에서 같은 failure signature가 `max_repair_attempts`(기본 3회) 반복되면 같은 repair를 중단한다. `systematic-debugging`으로 reproduction, first bad boundary, root cause evidence를 고정한 뒤 올바른 layer로 이동한다.

| Classification | Evidence | Route |
|---|---|---|
| IMPLEMENTATION | Contract/acceptance/knowledge는 명확하고 code만 틀림 | 최소 repair 후 verify |
| TASK | Slice가 크거나 acceptance/eval 경계가 잘못됨 | taskify/slice 재구성 |
| PLAN | 현재 설계로 requirement 충족 불가 | plan 재작성 + 영향 분석 |
| SPEC | expected behavior를 하나로 판정할 수 없음 | `/design` clarify |
| REQUIREMENT | business/priority/ownership decision 필요 | human gate |
| ENVIRONMENT | toolchain/fixture/target이 없어 검증 불가 | BLOCKED + 필요한 환경/명령 |
| KNOWLEDGE | source-of-truth/active claim/ontology가 부족 또는 충돌 | Domain Discovery / conflict resolution |

Failure evidence:

- command/check/exit code
- stable signature and first bad boundary
- selected Context Pack and environment fingerprint
- attempted repairs and attempt count
- classification/escalation reason
- next verifier and required decision

`.specify/eval/history.jsonl`과 feature `evidence-trace.jsonl`에 재현 가능한 최소 evidence를 남긴다. raw log 전체를 중앙 state에 복사하지 않는다.
