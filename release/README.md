# RC-01 conformance closure (V3-061)

`rc01-conformance-closure.json` 은 `design-reference` 의 35 REQ / 62 task / 112 test-catalog id 를
**실행된** junit 결과(`specs/013-amplai-v3/junit/`) 와 git revision 에 묶은 ledger 다. 생성기는
`src/amplai_foundry/distribution/closure.py`. 규칙 (design/21 §1, §4):

- catalog id 는 test 함수 본문에 적힌 것만 그 함수의 실행 결과와 연결한다. 파일 docstring 의 언급은 prose 다.
- `local_pass` 는 로컬 자동 evidence 다. live provider / container / physical holdout qualification 이 아니다.
- REQ 는 listed test 전부가 local_pass 일 때만 `fully_verified`. 하나라도 not_run 이면 partial.
- 설계 패키지 검사(schema/fixture/link)는 runtime pass 로 세지 않는다.
- `remaining_blockers` 에 not_run id 와 EXTERNAL_QUALIFICATION_PENDING 항목이 남는다. `specs/015-external-qualification/external-qualification-status.json` 이 DEV-03 의 9건 각각을 resolved/partial/unavailable 로 정제하며, resolved 가 아닌 항목만 pending 으로 실린다.
- `release_status` 는 기본 `rc_candidate_not_final` 이다. `.ai-team/policy/approvals.jsonl` 의 **commit 된**
  항목이 `gate=production_operation`, `proposal_id=V3-062`, `proposal_sha256=human-gate-package.md 의 digest`
  로 묶여 있을 때만 `final_declared` 가 되고, 그 승인은 `final_declaration` 에 그대로 실린다 (V3-062, 2026-09-21
  pinesky 승인). FINAL 이라도 수용한 external 항목은 `remaining_blockers` 에 그대로 남는다.

`eval/test-catalog-status.json` 은 같은 ledger 의 test 축만 뽑은 view 다. 재생성:
`pytest -m "not slack_e2e" tests/v3 tests/runtime_storage --junitxml=specs/013-amplai-v3/junit/rc01-v3-e2e.xml` 후
(`-m` 는 기본 deselect 되는 `visual` marker 를 포함시킨다. 자격 있는 browser 가 없는 host 에서는
그 case 들이 not_run 으로 남으며 skipped PASS 가 되지 않는다.)
`.venv/bin/python scripts/rc01_closure.py` 로 ledger 와 view 를 다시 쓰고,
`tests/v3/test_rc01_conformance_closure.py` 가 파일과 현재 계산을 대조한다.
