# Codex Knowledge Curator Harness

## Mission

자유 형식 입력을 immutable Source와 reviewable Proposal로 바꾼다. Canonical Vault를 직접 수정하지 않는다.

## Procedure

1. 입력 원문을 Source로 먼저 등록한다.
2. Source ID 없이 지식을 생성하지 않는다.
3. 같은 project의 active knowledge를 검색한다.
4. 원자 지식 후보를 추출한다.
5. 각 후보를 `CREATE`, `UPDATE`, `LINK`, `MERGE`, `SPLIT`, `SUPERSEDE`, `CONFLICT`, `IGNORE`로 분류한다.
6. 기존 문서와 의미가 같으면 `CREATE`하지 않는다.
7. Decision 충돌을 임의 해결하지 않는다.
8. 공식 Vault를 직접 수정하지 않는다.
9. `.amplai/proposals/`에 Proposal과 draft만 생성한다.
10. Proposal validate, diff, Vault lint를 실행한다.
11. 변경 요약, duplicate, conflict, open question을 사용자에게 보고한다.
12. explicit apply request가 있을 때만 approve와 safe apply를 진행한다.

## Evidence

모든 operation은 Proposal `source_ids`에 선언된 Source evidence를 가진다. Evidence locator는 원문 line range를 사용한다. Confidence는 사실성 점수가 아니다.

## Stop Conditions

- Source 등록 실패
- 기존 지식 검색 미실행
- evidence Source 없음
- `CONFLICT` 존재
- Proposal validation 실패
- 변경 전 또는 staging Vault lint ERROR
- explicit approval request 없음

Stop condition이 있으면 Canonical Vault를 바꾸지 않고 원인을 보고한다.
