# Lifecycle

## States

| Status | Meaning |
|---|---|
| `candidate` | 검토 중인 공식화 후보 상태 |
| `active` | 현재 유효한 공식 지식 |
| `superseded` | 새 지식이 대체한 상태 |
| `deprecated` | 사용 중단을 권고하는 상태 |
| `merged` | 다른 memory에 의미를 합친 상태 |
| `rejected` | 검토 후 채택하지 않은 상태 |
| `archived` | 현재 탐색 기본 범위에서 제외한 상태 |

## Invariants

- `superseded`는 `superseded_by`가 필요하다.
- `merged`는 `merged_into`가 필요하다.
- Active 공식 지식은 `source_refs`가 필요하다. `source` note는 예외다.
- `decision`이 `active` 또는 `superseded`이면 `## 결정` 또는 `## Decision` 본문이 필요하다.
- 해결되지 않은 `question`은 `active`다.
- 종료된 `question`은 관련 decision 또는 knowledge relation이 필요하다.
- `90-archive` directory의 note는 `archived`여야 한다.

## Replacement

새 Decision B가 이전 Decision A를 대체하면 B에 `supersedes -> A`를 추가한다. A는 `status: superseded`와 `superseded_by: B`를 기록한다. 두 변경을 같은 review 단위에서 검증한다.
