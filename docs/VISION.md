# Vision

## Purpose

AMPLAI Foundry는 사람이 검토한 프로젝트 지식을 Agent가 안전하게 재사용하게 만드는 Memory foundation이다. 첫 단계에서는 같은 Markdown을 사람과 AI가 읽고 Git으로 변경 이력을 관리한다.

## Product Boundary

Memory model이 제품의 중심이다. Folder와 YAML은 첫 저장 표현이며 영구 API가 아니다. 상위 capability는 `MemoryObject`와 `MemoryRepository`에 의존한다.

공식 지식은 concept, principle, decision, question, architecture, experiment와 source다. Agent run, step, event, tool call, checkpoint와 temporary candidate는 실행 메모리이며 이번 repository의 공식 Markdown에 저장하지 않는다.

## Success Signal

사람이 Obsidian에서 note를 탐색하고, Python tool이 같은 note의 schema, provenance, lifecycle과 link integrity를 offline에서 결정론적으로 검증하면 첫 가설이 성립한다.
