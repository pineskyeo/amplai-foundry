# Proposal Summary

## Source

`SRC-20260725-9FEFBEAF`은 AMPLAI, Hermes, Claude Code와 Codex의 역할, Project-owned Memory 원칙과 장기 architecture 방향을 정리한 사용자 제공 문서다.

## Proposed Changes

- AMPLAI AI OS, Hermes Work Manager, Implementation Agent, Governor를 candidate concept로 분리한다.
- “Project owns Memory”를 candidate principle로 기록한다.
- 역할 경계를 candidate architecture로 연결한다.
- operational memory와 canonical knowledge, Source of Truth 범위를 open question으로 기록한다.
- `MAP-0002`에 신규 후보와 질문을 연결한다.

## Non-Changes

- `.amplai/` directory 구조를 canonical layout으로 채택하지 않는다.
- Session JSONL 저장을 Decision으로 확정하지 않는다.
- Jira/session state를 canonical `MemoryObject` 범위에 포함하지 않는다.
- 기존 Markdown+Git canonical source 결정을 대체하지 않는다.

## Review Focus

- AMPLAI를 AI OS로 정의하는 범위가 현재 product vision과 일치하는지 검토한다.
- Hermes의 권한, 실패 처리, audit와 approve/apply 경계를 결정한다.
- Codex와 Claude Code를 구현 역할로 표현하되 curator 등 다른 Harness 역할을 배제하지 않는지 검토한다.

