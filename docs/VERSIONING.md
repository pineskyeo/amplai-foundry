# Versioning And Migration Policy

AMPLAI Foundry는 제품 버전, 저장 계약 버전, 개별 객체 revision을 서로 다른 축으로 관리한다.

## Version Axes

| 축 | 위치 | 의미 |
|---|---|---|
| Package version | `pyproject.toml`, `__version__` | 배포 가능한 구현 묶음 |
| Schema version | YAML/JSON의 `schema_version` | 직렬화 계약의 major shape |
| Proposal version | `proposal_version` | Proposal artifact 계약 |
| Object revision | `MemoryObject.revision` | 같은 qualified memory의 optimistic concurrency |
| Roadmap version | `RoadmapDefinition.version` | 승인된 계획 상태의 순차 revision |
| Domain version | `module.yaml`, `domain.lock.yaml` | import 가능한 domain module 버전 |

## Compatibility Rules

- 같은 `schema_version` 안에서는 필드 의미를 조용히 바꾸지 않는다.
- optional 필드 추가는 기존 artifact를 계속 읽을 수 있어야 한다.
- 필수 필드 추가, enum 삭제, 의미 변경은 새 schema version과 migration이 필요하다.
- local ID의 전역 고유성은 더 이상 가정하지 않는다. 새 코드는 `MemoryRef(namespace, local_id)`를 사용한다.
- namespace 없는 기존 API는 local ID가 하나로 확정될 때만 호환 동작한다. 둘 이상이면 명시적으로 실패한다.
- 기존 pack-relative Proposal draft 경로는 읽되, 새 Proposal은 `drafts/<file>` 형식으로 저장한다.

## Migration Procedure

1. 기존 fixture와 committed schema를 보존한다.
2. 변환 전후 artifact 수, qualified ref, provenance와 content hash를 비교한다.
3. migration은 반복 실행해도 같은 결과를 내야 한다.
4. Canonical 의미를 바꾸는 migration은 Proposal과 승인 없이 실행하지 않는다.
5. `amplai-foundry verify`를 clean checkout에서 통과시킨다.
6. rollback은 이전 Git revision과 이전 schema reader로 가능해야 한다.

Phase 0 golden snapshot은 `tests/fixtures/golden-memory-contract.yaml`이며 parser 결과와 원본 bytes가 동시에 바뀌는 것을 탐지한다.

Project Store Work의 additive `controller`, `runner_profile`, `base_ref`, `request_ref` 필드는 기존 reader와
호환된다. Work activation token/receipt 및 Slack status outbox는 host-local durable state이고 Memory schema
version을 올리지 않는다.
