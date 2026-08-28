<!-- AMPLAI-ASYNC-BEGIN -->
## 7. Decision-ready and Cross-App Design

중앙 Project Store가 연결된 경우 `/design`은 cross-app 상태를 설계할 수 있지만 실행하지 않는다.

- 변경이 여러 앱의 계약/동작에 영향을 주는 것이 evidence로 확인되면 CR 하나를 만든다.
- 앱별 Goal, Acceptance, Contract reference, dependency를 Work로 만들되 상태는 `DRAFT`로 둔다.
- Open Question은 `decision_class`, `reversibility`, `blast_radius`, evidence plan,
  policy minimum authority와 함께 기록한다.
- repository에서 조사 가능한 engineering choice는 AUTO/CHALLENGE 후보로 좁힌다.
- domain/product/production/safety/security/privacy/legal/destructive/public contract owner 결정만
  HUMAN으로 남긴다.
- Handoff 문서를 새로운 SSOT로 만들지 않는다. target-app Context는 CR/Work/Contract/Decision/
  Evidence에서 렌더링한다.

`DESIGN READY`에는 CR/Work ID, DRAFT dependency, blocking Question, authority, Contract와 첫 실행
Work를 포함한다. `/work` 또는 사람이 명시적으로 activate하기 전에는 Supervisor가 실행하지 않는다.
<!-- AMPLAI-ASYNC-END -->
