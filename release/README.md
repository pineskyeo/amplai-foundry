# RC-01 conformance closure (V3-061)

`rc01-conformance-closure.json` 은 `design-reference` 의 35 REQ / 62 task / 112 test-catalog id 를
**실행된** junit 결과(`specs/013-amplai-v3/junit/`) 와 git revision 에 묶은 ledger 다. 생성기는
`src/amplai_foundry/distribution/closure.py`. 규칙 (design/21 §1, §4):

- catalog id 는 test 함수 본문에 적힌 것만 그 함수의 실행 결과와 연결한다. 파일 docstring 의 언급은 prose 다.
- `local_pass` 는 로컬 자동 evidence 다. live provider / container / physical holdout qualification 이 아니다.
- REQ 는 listed test 전부가 local_pass 일 때만 `fully_verified`. 하나라도 not_run 이면 partial.
- 설계 패키지 검사(schema/fixture/link)는 runtime pass 로 세지 않는다.
- `remaining_blockers` 에 not_run id 와 EXTERNAL_QUALIFICATION_PENDING 9건이 그대로 남는다.
- `release_status` 는 `rc_candidate_not_final` 이다. FINAL 선언은 V3-062 human gate 뒤다.

`eval/test-catalog-status.json` 은 같은 ledger 의 test 축만 뽑은 view 다. 재생성:
`pytest tests/v3 tests/e2e --junitxml=specs/013-amplai-v3/junit/rc01-v3-e2e.xml` 후
`tests/v3/test_rc01_conformance_closure.py` 가 파일과 현재 계산을 대조한다.
