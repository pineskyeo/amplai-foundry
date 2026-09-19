# AMPLAI V2 Knowledge Index

`.ai-team/knowledge`는 프로젝트 지식의 복사본 저장소가 아니다. 여기에는 `/work`가 매 작업마다
올바른 자료를 다시 고를 수 있도록 **index, 상태, provenance metadata**만 둔다.

## Memory separation

| 종류 | 질문 | Canonical 자산 |
|---|---|---|
| Stable Knowledge | 시스템은 원래 어떻게 동작하는가 | `AGENTS.md`, `CLAUDE.md`, `docs/`의 domain 문서, `vault/`의 canonical note |
| Decision Memory | 왜 이렇게 선택했는가 | `docs/workstreams/*/DECISIONS.md`, feature spec/plan, `decisions.index.json` |
| Work Memory | 이번 작업은 어디까지 됐는가 | `specs/<work>/`의 contract/context/progress/verification/handoff |
| Evidence | 그 사실의 근거는 무엇인가 | code/test/API/log/git/verifier result, `claims.jsonl`의 evidence link |

`deprecated`, `superseded`, `rejected` 항목은 기본 Context Pack에서 제외한다. 지식 충돌을
Agent가 조용히 선택하지 않고 conflict로 표면화한다.

## Governing Inputs

Governing input은 현재 Work를 통제하는 명시적 instruction과 적용 가능한 active Decision이다.
Documentation policy의 governing_inputs가 instruction과 decision_ledger 역할, exact path와
security를 선언한다. Release guide의 review 상태와 분리하되, 원문의 보안과 폐기 상태를
우회하지 않는다. 필수 instruction은 검색 관련도가 낮아도 Context에서 빠지지 않는다.

과거 Decision ledger 전체를 current source로 넣지 않는다. Active index의 Decision ID와
path가 일치하는 항목, 그 Decision을 가리키는 typed evidence만 사용한다. Context 검증은
source·claim·Decision 전체 집합을 다시 계산한다. 항목 삭제 후 hash를 다시 만든 pack도 거부한다.

## Document Review Ownership

Documentation policy의 review_store가 문서 검토 metadata의 단일 저장소를 지정한다.
각 검토는 owner 원문, 의존성, 후보 코드, 방법과 근거의 hash에 묶인다. 새 검토를
기록해도 이전 검토는 이력으로 보존한다. Work의 문서 영향 분석은 여기서 재생성하는
view이며 별도 검토 정본이 아니다. 문서 원문과 canonical Vault는 이동하거나 복사하지
않는다. 이 attributed review는 사람 승인이나 독립 구현 리뷰를 대신하지 않는다.

검토 기록은 serialized UTF-8 기준 8 MiB 이내에서만 교체한다. 초과하면 기존 파일의
내용·권한을 보존하며, history를 자동 삭제하지 않는다. Impact view는 policy의
max_file_bytes 한도를 사용한다. 제한을 넘긴 출력은 성공으로 기록하지 않는다.
