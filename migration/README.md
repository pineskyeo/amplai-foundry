# Legacy retirement proposal (V3-060)

`retirement-proposal.json` 은 **삭제 목록이 아니다**. 후보마다 path / old_digest / owner / reason /
replacement / live_refs(static·tests·dynamic_entry) / backup_ref / required_test_ids / blockers 를 기록한
제안서다 (design/19 §4, design/25 §2·§4). 생성기는 `src/amplai_foundry/migration/retirement.py`.

- live ref 가 하나라도 있으면 `blocked`. 이름이 legacy_* 라는 이유로는 폐기하지 않는다 (T-092).
- `src/amplai_foundry/governance/`, `vault/`, `docs/decisions|workstreams/`, approvals 는 protected prefix 다.
- `proposed` 가 되어도 실제 삭제는 `destructive_change` human gate 와 별도 승인 Work 다.
- design-reference/migration/component-map.csv 의 disposition 은 권장 조치이며 여기서는 참고로만 붙인다.

재생성: `.venv/bin/python -m pytest tests/v3/test_rc01_legacy_retirement.py` 가 현재 tree 로 다시 계산해
파일과 비교한다.
