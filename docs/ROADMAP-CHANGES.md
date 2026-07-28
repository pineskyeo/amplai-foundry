# Roadmap Change Contract

Roadmap은 stable phase ID, 순서, 상태, dependency, 완료 조건과 단조 증가 version을 가진 운영 상태다. Markdown 로드맵 입력은 현재 상태를 직접 덮어쓰지 않고 먼저 구조 분석 report 또는 `RoadmapChangeProposal`이 된다.

지원하는 변경은 `ADD`, `UPDATE`, `MOVE`, `CANCEL`, `SUPERSEDE`다. 삭제는 이력을 잃지 않도록 `CANCEL`로 변환한다.

모든 Proposal은 다음을 포함한다.

- base/proposed version
- before/after payload
- 직접·전이 dependency impact
- current focus 영향
- replan 필요 여부
- 생성자와 승인자

Apply는 명시적 승인과 base version 일치를 요구한다. roadmap별 lock 안에서 revision을
다시 확인하고 atomic replace하며, 성공하면 승인자와 applied proposal ID를 authoritative
roadmap에 기록한다. dependency가 완료된 가장 이른 non-terminal phase를
`current_focus`로 재계산한다. 오직 status-only Tracker 변경만 authority와 policy가
허용할 때 자동 승인·적용할 수 있다.

```bash
amplai-foundry roadmap diff desired.yaml \
  --current plans/amplai-master-roadmap.yaml
amplai-foundry roadmap show RMAP-...
amplai-foundry roadmap approve RMAP-... --approved-by reviewer
amplai-foundry roadmap apply RMAP-...
amplai-foundry roadmap next
```

Markdown의 `Phase N` heading을 stable ID에 하나로 매핑하지 못하면 `review_required` 또는 `hold`이며 authoritative roadmap을 변경하지 않는다.
