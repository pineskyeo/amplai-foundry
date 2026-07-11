# Knowledge Rules

## Atomic Knowledge

- 하나의 note는 하나의 핵심 질문 또는 주장에 집중한다.
- Note 하나만 읽어도 핵심 의미를 이해할 수 있게 작성한다.
- 독립적으로 바뀔 내용은 별도 note로 분리한다.
- 한 문장뿐인 note와 여러 독립 질문을 섞은 거대 note를 만들지 않는다.
- 넓은 주제는 Map/MOC에서 여러 atomic note를 연결한다.

## Canonical Source

현재 canonical source는 Markdown + Git이다. Obsidian과 AI는 동일한 file을 읽는다. 사람용 복제 문서나 AI 전용 복제 문서를 만들지 않는다.

## Provenance

Active 공식 지식은 하나 이상의 `source_refs`를 가진다. `source` kind는 provenance 시작점이므로 이 규칙의 유일한 예외다. Source note는 원문 위치와 수집 context를 사람이 확인할 수 있게 설명한다.

## Change Safety

기존 결정을 조용히 덮어쓰지 않는다. 대체 결정은 `supersedes` relation을 사용하고 이전 결정은 `superseded_by`를 기록한다. 삭제보다 lifecycle state를 사용한다.

공식 지식 변경 자동화는 현재 금지다. 향후 Memory Candidate와 Proposal이 도입돼도 review/apply gate를 통과해야 한다.
