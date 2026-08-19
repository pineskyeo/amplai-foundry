# AMPLAI V2 Knowledge Index

`.ai-team/knowledge`는 프로젝트 지식의 복사본 저장소가 아니다. 여기에는 `/work`가 매 작업마다
올바른 자료를 다시 고를 수 있도록 **index, 상태, provenance metadata**만 둔다.

## Memory separation

| 종류 | 질문 | Canonical 자산 |
|---|---|---|
| Stable Knowledge | 시스템은 원래 어떻게 동작하는가 | `AGENTS.md`, `CLAUDE.md`, architecture/domain docs, ontology |
| Decision Memory | 왜 이렇게 선택했는가 | `docs/decisions/`, feature plan/ADR, `decisions.index.json` |
| Work Memory | 이번 작업은 어디까지 됐는가 | `specs/<work>/`의 contract/context/progress/verification/handoff |
| Evidence | 그 사실의 근거는 무엇인가 | code/test/API/log/git/verifier result, `claims.jsonl`의 evidence link |

`deprecated`, `superseded`, `rejected` 항목은 기본 Context Pack에서 제외한다. 지식 충돌을
Agent가 조용히 선택하지 않고 conflict로 표면화한다.
