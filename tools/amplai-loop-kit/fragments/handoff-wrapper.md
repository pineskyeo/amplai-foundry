<!-- AMPLAI-ASYNC-BEGIN -->
## Cross-App Handoff Projection

중앙 Project Store가 연결된 cross-app 변경에서는 별도 handoff 문서를 source of truth로 쓰지 않는다.
`python3 scripts/amplai.py work context --id <WORK_ID> --format markdown`이 CR, target Work,
Contract, Decision, upstream Result/Evidence의 현재 상태를 렌더링한다.

같은 앱에서 세션만 바뀌는 경우는 handoff가 아니라 Work Context + session checkpoint/resume다.
기존 11개 항목 handoff는 Project Store를 쓰지 않는 legacy/사람 간 전달에만 사용한다.

Work의 `controller`, `runner_profile`, `base_ref`, `request_ref`는 Context view에서 확인한다. Slack/Hermes는
request/status route일 뿐이며 human activation과 managed worktree dispatch 권한을 대신하지 않는다.
<!-- AMPLAI-ASYNC-END -->
