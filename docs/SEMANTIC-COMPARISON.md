# Semantic Comparison Contract

Phase 1 semantic kernel은 외부 LLM이나 embedding 없이 구조화된 scope, claim, constraint와 보수적인 lexical signal을 비교한다. Project namespace 밖의 anchor는 비교 대상이 아니다.

| Relation | 의미 | 기본 동작 |
|---|---|---|
| `EXACT_DUPLICATE` | signature와 canonical statement 동일 | `IGNORE` |
| `SEMANTIC_DUPLICATE` | 구조화된 의미 동일, 표현 다름 | `LINK` |
| `REFINES` | 같은 주제에 constraint 추가 | review `UPDATE` |
| `CONFLICTS` | 같은 주제의 polarity 충돌 | `CONFLICT`, review |
| `NEW` | project 안에 유의미한 match 없음 | review `CREATE` |
| `UNCERTAIN` | 부분 유사, constraint overlap 또는 다중 후보 | `HOLD` |

가장 중요한 fail-closed 규칙은 애매함을 `NEW`로 승격하지 않는 것이다. 같은 강도의 후보가 여러 개이거나 부분 유사성만 있으면 `matched_target`을 추측하지 않고 `UNCERTAIN`을 반환한다.

`tests/fixtures/semantic-golden-set.yaml`은 여섯 relation에 걸친 30개 이상의 고정 사례다. comparator 변경은 이 fixture와 Intake E2E를 모두 통과해야 한다.

Phase 1의 descriptor는 ontology가 아니다. Domain class/relation/constraint와 재사용 가능한 reasoning은 Phase 2에서 별도 계약으로 확장한다.
