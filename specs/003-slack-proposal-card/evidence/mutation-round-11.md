# Mutation Measurement — Round 11 (MGC-012-P5-T008)

**Completed**: 2026-08-13

전용 격리 복제본에서 mutation 43종을 실행했다. **29 killed / 14 survived, kill rate 67%**
(round 10 은 36종 중 12 killed, 33%).

| Check | Result |
|---|---|
| Baseline (시작) | 1202 passed, 4 deselected in 116.90s |
| Baseline (종료) | 1202 passed, 4 deselected in 112.22s |
| 복제본 대 source `src/`·`tests/` | byte-identical |

## Method

round 10 의 방법론 결함(격리 복제본 동시 접근)을 고쳤다. 이번 복제본은 regression lens 전용이고
다른 작업이 접근하지 않았다. 전 mutation 을 **line index 로 적용**했다 — anchor 문자열은 해당
줄 안 유일성 확인에만 썼다. round 10 에서 비유일 anchor 로 건너뛴 항목이 없다.

SURVIVED 판정은 full suite green 으로만 내렸다. 매 mutation 후 복원하고 `read_bytes()` 비교로
복원을 검증했다. journal 은 mutation 1건마다 즉시 append 했다.

## Open Items Settled

`MGC-012-P5-T008` 이 요구한 넷을 모두 닫았다.

1. **주장된 over-grading 2건 — 둘 다 확인.** round 10 `C-12`(P0)와 `C-13`(P1)은 과대평가였다.
   각각 guard 하나만 / 둘 다 제거하는 대조 실험으로 증명했다.
2. **도달 가능성 — 둘 다 도달 불가.** `result_identity_exceeds_body_limit` 는 sqlite `INTEGER`
   가 19자리 상한을 주므로 `project_limit` 하한이 111 이다. token-consume rowcount guard 는
   같은 transaction 의 선행 SELECT 와 `BEGIN IMMEDIATE` 가 전제를 막는다.
3. **미측정 subsystem — 11 mutation.** `migrations.py` 6종 중 3 killed(50%),
   `slack_http.py` 5종 **전부 killed**(100%).
4. **새 surface — 둘 다 방어된다.** T004 fix 되돌리기와 T007 정렬 되돌리기가 각각 즉시 잡힌다.

판정과 등급은 `3lens-review-round-11.md` 에 있다. 아래는 원본 journal 이다.

## Journal

| N | file:line | mutation | result | reverted |
|---|---|---|---|---|
| M01 | slack_cards.py:85 | render_review 의 _clear_exception_frames 를 no-op 으로 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:85 -> error`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_scrubs_frames_on_its_real_failure_path`
    - failing tests: 1
    - elapsed: 17s
| M02 | slack_cards.py:209 | action_set.view.state != 'issued' guard 무력화 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:209 -> if False and action_set.view.state != "issued":`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_a_non_issued_action_set`
    - failing tests: 1
    - elapsed: 17s
| M03 | slack_cards.py:211 | len(action_set.issued) != 3 guard 무력화 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:211 -> if False and len(action_set.issued) != 3:`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_an_action_set_without_exactly_three_actions`
    - failing tests: 1
    - elapsed: 16s
| M04 | slack_cards.py:214 | actions != set(DecisionAction) guard 무력화 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:214 -> if False and actions != set(DecisionAction):`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_three_actions_that_are_not_the_contract_set`
    - failing tests: 1
    - elapsed: 30s
| M05 | slack_cards.py:224 | binding 검사에서 allowed_actor_ref.actor_id 성분 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:224 -> or False`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_an_action_set_bound_to_another_reviewer`
    - failing tests: 1
    - elapsed: 29s
| M06 | slack_cards.py:225 | binding 검사에서 bound_channel_ref 성분 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:225 -> or False`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_an_action_set_bound_to_another_channel`
    - failing tests: 1
    - elapsed: 24s
| M07 | slack_cards.py:226 | binding 검사에서 expires_at 성분 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:226 -> or False`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_an_expiry_that_does_not_match_the_view`
    - failing tests: 1
    - elapsed: 20s
| M08 | decisions.py:411 | token expiry 경계 >= 를 > 로 (만료 순간 허용) | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:411 -> if processed_at > self._parse_timestamp(str(token[13])):`
    - decisive: `FAILED tests/test_decisions.py::test_a_decision_at_the_exact_expiry_instant_is_rejected`
    - failing tests: 1
    - elapsed: 17s
| M09 | review_cards.py:634 | _command_for_event digest/canonical 정합 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/review_cards.py:634 -> if False:`
    - decisive: `FAILED tests/test_review_cards.py::test_action_set_preparation_rejects_a_mismatched_event_payload_digest`
    - failing tests: 2
    - elapsed: 19s
| M10 | review_cards.py:340 | replay fingerprint 불일치 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/review_cards.py:340 -> if False and str(row[0]) != fingerprint:`
    - decisive: `FAILED tests/test_review_cards.py::test_a_replayed_request_with_a_different_fingerprint_conflicts`
    - failing tests: 1
    - elapsed: 20s
| M11 | events.py:119 | Review Card 채널이 Slack 이어야 한다는 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/events.py:119 -> if False and self.bound_channel_ref.provider is not ChannelProvider.SLACK:`
    - decisive: `FAILED tests/test_review_cards.py::test_review_payload_rejects_a_non_slack_channel`
    - failing tests: 1
    - elapsed: 20s
| M12 | events.py:126 | operation_counts 합계 == operation_count 불변 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/events.py:126 -> if False and sum(self.operation_counts.values()) != self.operation_count:`
    - decisive: `FAILED tests/test_review_cards.py::test_review_payload_rejects_a_broken_operation_count_total`
    - failing tests: 1
    - elapsed: 20s
| M13 | events.py:128 | remaining_operation_count 불변 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/events.py:128 -> if False and self.remaining_operation_count != self.operation_count - len(self.operation_titles):`
    - decisive: `FAILED tests/test_review_cards.py::test_review_payload_rejects_a_broken_remaining_operation_count`
    - failing tests: 1
    - elapsed: 21s
| M14 | events.py:1241 | review-card audit cardinality != 1 을 < 1 로 (중복 audit 허용) | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/events.py:1241 -> if len(audits) < 1:`
    - decisive: `FAILED tests/test_review_cards.py::test_reconcile_rejects_a_duplicated_review_card_audit`
    - failing tests: 1
    - elapsed: 20s
| M15 | slack_projection.py:897 | T004 fix 되돌리기: 평범한 Exception 도 interruption 으로 승격 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_projection.py:897 -> if True:`
    - decisive: `FAILED tests/test_review_cards.py::test_ordinary_prepare_exception_dead_letters_instead_of_escaping`
    - failing tests: 3
    - elapsed: 17s
| M16 | decisions.py:771 | raw_token in idempotency_key guard 만 제거 (2차 guard 유지) | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:771 -> if False and raw_token in idempotency_key:`
    - decisive: `1202 passed, 4 deselected in 122.64s (0:02:02)`
    - elapsed: 135s
| M17 | decisions.py:369 | _contains_persisted_secret guard 만 제거 (1차 guard 유지) | KILLED (full) | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:369 -> if False and self._contains_persisted_secret(connection, idempotency_key):`
    - decisive: `FAILED tests/test_apply_jobs.py::test_raw_apply_grant_cannot_persist_as_decision_idempotency_key`
    - failing tests: 2
    - elapsed: 137s
| M18 | decisions.py:369+771 | 두 secret guard 동시 제거 | KILLED (full) | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:369 -> if False and self._contains_persisted_secret(connection, idempotency_key):`
    - applied: `src/amplai_foundry/governance/decisions.py:771 -> if False and raw_token in idempotency_key:`
    - decisive: `FAILED tests/test_apply_jobs.py::test_raw_apply_grant_cannot_persist_as_decision_idempotency_key`
    - failing tests: 4
    - elapsed: 141s
| M19 | slack_cards.py:202 | _review_body 마지막 _truncate 만 제거 (_text_object 절단 유지) | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:202 -> return "\n".join((*fixed, *previews))`
    - decisive: `1202 passed, 4 deselected in 130.13s (0:02:10)`
    - elapsed: 135s
| M20 | slack_cards.py:285 | _text_object 절단만 제거 (_review_body 절단 유지) | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:285 -> "text": text,`
    - decisive: `1202 passed, 4 deselected in 127.45s (0:02:07)`
    - elapsed: 132s
| M21 | slack_cards.py:202+285 | 두 절단 동시 제거 | KILLED (full) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:202 -> return "\n".join((*fixed, *previews))`
    - applied: `src/amplai_foundry/governance/slack_cards.py:285 -> "text": text,`
    - decisive: `FAILED tests/test_review_cards.py::test_review_body_stays_inside_the_rendered_limit`
    - failing tests: 1
    - elapsed: 126s
| M22 | slack_cards.py:178 | result_identity_exceeds_body_limit guard 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:178 -> if False and project_limit < 4:`
    - decisive: `1202 passed, 4 deselected in 123.19s (0:02:03)`
    - elapsed: 128s
| M23 | decisions.py:458 | token consume rowcount != 1 guard 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:458 -> if False and consumed.rowcount != 1:`
    - decisive: `1202 passed, 4 deselected in 130.03s (0:02:10)`
    - elapsed: 146s
| M24 | decisions.py:448 | proposal update rowcount != 1 guard 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/decisions.py:448 -> if False and updated.rowcount != 1:`
    - decisive: `1202 passed, 4 deselected in 132.05s (0:02:12)`
    - elapsed: 159s
| M25 | migrations.py:4136 | migration version 연속성 검사 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4136 -> if False and versions != tuple(range(1, len(migrations) + 1)):`
    - decisive: `1202 passed, 4 deselected in 111.29s (0:01:51)`
    - elapsed: 114s
| M26 | migrations.py:4193 | applied history version 연속성 검사 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4193 -> if False and applied_versions != tuple(range(1, len(applied_versions) + 1)):`
    - decisive: `1202 passed, 4 deselected in 109.74s (0:01:49)`
    - elapsed: 113s
| M27 | migrations.py:4200 | migration name/checksum 대조 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4200 -> if False:`
    - decisive: `FAILED tests/test_governance_store.py::test_migration_history_tampering_fails_closed`
    - failing tests: 1
    - elapsed: 3s
| M28 | migrations.py:4950 | store_kind metadata 검사 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4950 -> if False and metadata != ("amplai-governance",):`
    - decisive: `1202 passed, 4 deselected in 109.59s (0:01:49)`
    - elapsed: 112s
| M29 | migrations.py:4964 | apply_pending 의 active transaction 요구 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4964 -> if False and not connection.in_transaction:`
    - decisive: `FAILED tests/test_governance_store.py::test_migration_entrypoint_requires_active_transaction`
    - failing tests: 1
    - elapsed: 3s
| M30 | migrations.py:4971 | 이미 적용된 migration skip 경계 <= 를 < 로 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/migrations.py:4971 -> if migration.version < current:`
    - decisive: `FAILED tests/test_governance_store.py::test_initialize_creates_versioned_store_with_required_runtime_profile`
    - failing tests: 12
    - elapsed: 2s
| M31 | slack_http.py:1056 | self-check 응답 channel 불일치 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_http.py:1056 -> if False and result.channel != channel:`
    - decisive: `FAILED tests/test_slack_http.py::test_a_mismatched_response_channel_fails_closed_and_cleans_the_confirmed_identity`
    - failing tests: 3
    - elapsed: 5s
| M32 | slack_http.py:1245 | probe app_id 대조 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_http.py:1245 -> if False and probe.app_id != app_id:`
    - decisive: `FAILED tests/test_slack_http.py::test_a_missing_app_id_refuses_startup - Asse...`
    - failing tests: 6
    - elapsed: 5s
| M33 | slack_http.py:1271 | marker readback 성분 대조 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_http.py:1271 -> if False`
    - decisive: `FAILED tests/test_slack_http.py::test_a_field_changed_in_transit_refuses_startup[event_id]`
    - failing tests: 6
    - elapsed: 5s
| M34 | slack_http.py:1034 | probe marker 형식 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_http.py:1034 -> if False and not _valid_probe_marker(probe_marker):`
    - decisive: `FAILED tests/test_slack_http.py::test_a_probe_marked_for_a_real_destination_is_refused_before_sending`
    - failing tests: 15
    - elapsed: 5s
| M35 | slack_http.py:1123 | probe marker key 집합 검사 제거 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_http.py:1123 -> if False and set(marker) != {"event_type", "event_payload"}:`
    - decisive: `FAILED tests/test_slack_http.py::test_every_malformed_probe_input_is_rejected_without_network[<lambda>1]`
    - failing tests: 1
    - elapsed: 5s
| M36 | ingress_worker.py:115 | T007 정렬 되돌리기: SafeInteractionOutcome.COMPLETED 부활 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/ingress_worker.py:115 -> UNAVAILABLE = "unavailable"
    COMPLETED = "completed"`
    - decisive: `FAILED tests/test_slack_ack_boundary.py::test_every_safe_outcome_value_has_a_producer`
    - failing tests: 1
    - elapsed: 13s
| M37 | ingress_worker.py:115 + slack_http.py:533 | COMPLETED enum + message table 동시 부활 | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/ingress_worker.py:115 -> UNAVAILABLE = "unavailable"
    COMPLETED = "completed"`
    - applied: `src/amplai_foundry/governance/slack_http.py:533 -> SafeInteractionOutcome.UNAVAILABLE: "This Proposal action could not be completed safely.",
    SafeInteractionOutcome.COMPLETED: "This Proposal action completed.",`
    - decisive: `FAILED tests/test_slack_ack_boundary.py::test_every_safe_outcome_value_has_a_producer`
    - failing tests: 1
    - elapsed: 13s
| M38 | slack_cards.py:46 | unsupported_result_status fail-closed 를 fail-open 으로 (미지원 status 를 approved 로 렌더) | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:46 -> title = _RESULT_TITLES.get(payload.proposal_status, "Proposal approved")`
    - decisive: `1202 passed, 4 deselected in 112.88s (0:01:52)`
    - elapsed: 116s
| M39 | review_cards.py:634 | ROOT_MISMATCH 비교에서 payload_json 성분만 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/review_cards.py:634 -> if str(row[1]) != digest or event.payload_digest != digest:`
    - decisive: `1202 passed, 4 deselected in 117.52s (0:01:57)`
    - elapsed: 134s
| M40 | events.py:1247 | REVIEW_CARD_AUDIT_MISMATCH 비교에서 actor_id 성분 제거 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/events.py:1247 -> or False`
    - decisive: `1202 passed, 4 deselected in 116.16s (0:01:56)`
    - elapsed: 135s
| M41 | review_cards.py:226 | review-card outbox cardinality guard 를 < 1 로 약화 | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/review_cards.py:226 -> if len(outbox) < 1:`
    - decisive: `1202 passed, 4 deselected in 112.03s (0:01:52)`
    - elapsed: 131s
| M42 | slack_cards.py:214 | actions != set(DecisionAction) 를 '비어있지 않음' 으로 약화 (round 10 M05 재현) | KILLED (targeted) | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:214 -> if not actions:`
    - decisive: `FAILED tests/test_review_cards.py::test_render_review_rejects_three_actions_that_are_not_the_contract_set`
    - failing tests: 1
    - elapsed: 16s
| M43 | slack_cards.py:178 | result_identity 경계 < 4 를 < 2 로 약화 (round 10 M11 재현) | SURVIVED | reverted+verified |
    - applied: `src/amplai_foundry/governance/slack_cards.py:178 -> if project_limit < 2:`
    - decisive: `1202 passed, 4 deselected in 114.78s (0:01:54)`
    - elapsed: 118s
