# Relationships

## Types

| Type | Meaning |
|---|---|
| `related_to` | 일반 의미 연관 |
| `depends_on` | target이 전제 또는 dependency |
| `supports` | target의 근거 또는 강화 요소 |
| `contradicts` | target과 양립하지 않는 주장 |
| `supersedes` | target을 대체하는 새 지식 |
| `implements` | target principle, concept 또는 decision의 구현 |
| `derived_from` | target에서 도출된 지식 |

## Direction

Relation은 현재 note에서 `target`으로 향하는 directed edge다. 역방향 검색은 `MemoryRepository.find_referencing()`이 제공한다.

## Structured Links And Wiki Links

Front Matter relation은 machine contract다. 본문의 `[[Wiki Link]]`는 사람이 Obsidian에서 탐색하는 reading link다. 중요한 관계는 둘 다 기록하되, lint integrity는 structured relation을 기준으로 판단한다.

`source_refs`, `superseded_by`, `merged_into`도 repository 안의 실제 ID를 가리켜야 한다. Self reference는 허용하지 않는다.
