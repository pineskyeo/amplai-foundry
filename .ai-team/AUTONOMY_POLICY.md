# Autonomy Policy

AMPLAI Loop Kit 이 이 파일을 생성했다. marker 밖 내용은 앱이 소유한다.

<!-- AMPLAI-ASYNC-BEGIN -->
## Machine-Enforced Decision Authority

공용 Work에 연결된 Open Question은 `.ai-team/runtime/async-policy.json`과 Project Store가 다음
minimum authority를 강제한다.

- `AUTO`: local, reversible, low impact이며 evidence가 있는 engineering decision.
- `CHALLENGE`: architecture, compatibility, persistence, performance architecture, cross-app contract,
  low reversibility 또는 high blast radius. 독립 reviewer의 ACCEPT evidence 필요.
- `HUMAN`: domain/product/production/safety/security/privacy/legal/destructive/public-contract owner
  decision. `approved_by`가 있는 human approval evidence 필요.

Agent는 minimum보다 높은 authority를 선택할 수 있지만 낮출 수 없다. Decision은 Question과 Evidence를
참조하고, 뒤집힌 Decision은 삭제하지 않고 supersede한다. Subagent 수가 아니라 evidence diversity를
우선하며 Primary Agent가 최종 판단 책임을 가진다.
<!-- AMPLAI-ASYNC-END -->
