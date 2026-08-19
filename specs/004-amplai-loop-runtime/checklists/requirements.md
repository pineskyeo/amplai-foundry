# Specification Quality Checklist: AMPLAI Loop Runtime Adoption

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-19
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Validation Notes

**1차 검증에서 고친 것.**

초안은 `/work`, `.ai-team/`, `loopctl`, `registry.json`, `rdflib` 같은 구현 이름을 그대로
썼다. spec 은 WHAT/WHY 이고 HOW 가 아니므로 전부 걷어냈다.

| 고치기 전 | 고친 뒤 |
|---|---|
| "`/work` 와 `/design` 둘로 줄인다" | "진입점은 정확히 둘 — 끝까지 수행하는 것, 설계만 확정하는 것" |
| "`.ai-team/verifiers/registry.json` 에 7 check 등록" | "검증은 이름 있는 묶음으로 실행할 수 있어야 하고 기존 항목 전부가 포함" |
| "rdflib·ontology·MCP 제외" | "의미망·추론 계층 제외" (Assumptions 에 구체 명칭) |
| "`CLAUDE.md` 를 adapter 로" | "어느 문서가 실질 규칙인지 명확해야 한다" |

구현 이름이 필요한 곳은 **Assumptions** 로 내렸다. 그쪽은 판단 근거를 적는 자리라 cortex
commit 해시 같은 사실이 있어야 한다.

**[NEEDS CLARIFICATION] 를 쓰지 않은 이유.**

이 feature 의 큰 선택 둘은 spec 작성 **전에** 사용자 Decision 으로 닫혔다 (`D-046`).
절차 주도권을 controller 에 넘기는 것과 의미망 계층을 제외하는 것이다. 남은 모호함은
설계 단계에서 정할 것들이라 spec 에 marker 를 남기지 않았다.

**Scope 경계.**

`FR-017` 이 "제품 동작은 바뀌지 않는다" 로 경계를 그었다. 이 feature 는 개발 절차와 저장소
상단 구조만 바꾼다. `src/amplai_foundry/` 는 대상이 아니다.

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`
