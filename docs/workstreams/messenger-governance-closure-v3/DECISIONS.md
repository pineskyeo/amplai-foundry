# Messenger Governance Closure V3 Decisions

## D-001 — State Authority

- Status: accepted
- Decision: SQLite가 active definition pointer와 mutable governance state의 authority다.
- Source: `AMP-SPEC-MGC-003#Authoritative-State`

## D-002 — Definition Storage

- Status: accepted
- Decision: Proposal definition과 Apply input은 immutable content-addressed object다.
- Source: `AMP-SPEC-MGC-003#Immutable-Definition-Contract`

## D-003 — Canonical Publish

- Status: accepted
- Decision: Worker는 staging만 수행하고 Publish Coordinator가 Git ref CAS를 소유한다.
- Source: `AMP-SPEC-MGC-003#Publish-Coordinator`

## D-004 — Review Gate

- Status: accepted
- Decision: 각 implementation item은 subagent code/contract/operations review를 통과한다.
- Source: 사용자 요청

## D-005 — Reachable Migration Immutability

- Status: accepted
- Decision: 이미 도달 가능한 schema migration body와 checksum은 변경하지 않고 보강은 새 additive migration으로 적용한다.
- Source: MGC-011 Slice 4 Package 4.2a compatibility review

## D-006 — Attested Rollback Provenance

- Status: accepted
- Decision: destination rollback provenance는 import Audit/Outbox 생성 전에 실제 이전 cursor와 command에 결합해 attest하며, 동시 attestation이 없는 predecessor evidence는 신뢰하지 않는다.
- Source: MGC-011 Slice 4 Package 4.2b1 operations review

## D-007 — Versioned Rollback Evidence Compatibility

- Status: accepted
- Decision: reachable v28 rollback evidence는 `scope_version=1`로 그대로 보존한다.
  신규 synthetic hold provenance와 exact deletion은 additive v29의
  `scope_version=2`로만 생성한다. v1 synthetic hold는 삭제하지 않고 exact migration
  evidence로 검증하며 terminal rolled-back gate가 mutation을 차단한다.
- Source: MGC-011 Slice 4 Package 4.2b2 compatibility review

## D-008 — Forward Recovery Eligibility And Evidence

- Status: accepted
- Decision: activation 이후 `v3_definition_revision` recovery plan은 `draft` 또는
  `changes_requested` Proposal만 허용한다. applied/dependent root와 unresolved synthetic
  approval hold는 차단하며, activated migration의 immutable definition/import command/Audit/Outbox
  graph는 계속 reconciliation한다.
- Source: MGC-011 Slice 4 Package 4.2c1 Contract·Evidence·Ops review

## D-009 — Atomic Forward Recovery Execution

- Status: accepted
- Decision: activation 후 definition correction은 authenticated recovery plan과 exact
  activation/lifecycle root를 검증한 뒤 하나의 transaction에서 Proposal CAS, immutable
  definition revision, Audit, Outbox와 digest-bound recovery result를 함께 commit한다.
  Recovery evidence는 exact definition revision에 결합하며 startup reconciliation이
  command, item, result, Audit, Outbox와 historical definition provenance 변조를 차단한다.
- Source: MGC-011 Slice 4 Package 4.2c2 Contract·Evidence·Ops review

## D-010 — V2 Migration Closure

- Status: accepted
- Decision: MGC-011 A1–A17은 `c3d635f`에서 PASS다. Activation 후 legacy
  definition correction은 active transaction의 repository-local recovery scope와 durable
  command evidence를 함께 요구하며 public 또는 committed-incomplete command 우회를
  차단한다. MGC-012 Slack Reference Adapter로 진행한다.
- Source: MGC-011 final Contract·Evidence·Ops gate

## D-011 — Slack Authentication Boundary

- Status: accepted
- Decision: Slack message button ingress는 raw form body의 `v0` HMAC과 5-minute
  timestamp를 deserialize 전에 검증한다. Installation identity는 기존 Authority 계약과
  동일한 단일 `workspace_id:api_app_id` 형식이다. Parser는 strict UTF-8, duplicate-key
  rejection과 body/depth/node/string budget을 적용하며 unsupported surface는 fail-closed한다.
- Source: MGC-012 Package 1 Contract·Evidence·Ops review

## D-012 — Slack Ack Budget Is A Config Invariant, Not A Deadline

- Status: accepted
- Decision: A9는 두 가지로만 강제한다. 첫째, `BoundedIngressAck` 생성자가
  `ingress busy_timeout_ms < ack budget`을 요구하고 위반 시 구성 자체를 거부한다. 둘째,
  전체 synchronous path를 측정해 budget 초과 시 success ack를 non-success로 강등한다.
  진행 중인 request를 중단하지는 않는다. HTTP layer가 이 package 범위 밖이라 hard
  deadline을 걸 대상이 없다. Review 실측에서 write 경합 시 `submit()` 한 번이 2641ms로
  budget 3000ms의 88%를 `accept()` 안에서 소비했다. 정확한 timeout 값은 SPEC에 따라
  Activation Record가 보유하며 MGC-015에서 정한다.
- Source: MGC-012 Package 2 Evidence·Ops review

## D-013 — Slack Does Not Redeliver Interactive Payloads

- Status: accepted
- Decision: Slack은 interactivity request를 재시도하지 않는다. 3초 안에 200을 받지
  못하면 사용자에게 error를 표시한다. `X-Slack-Retry-Num` 3회 backoff는 Events API 계약이며
  interactivity에 적용되지 않는다. 따라서 `BUDGET_EXCEEDED` 응답은 Slack 자동 재전송으로
  수렴하지 않는다. 다만 durable commit은 이미 끝났으므로 background worker가 decision을
  완료한다. 사용자가 버튼을 다시 누르면 `action_ts`가 달라져 새 command가 되고 token이
  이미 consumed라 `recovery_hold`로 간다. 이 재클릭 경로는 Package 3의 Slack message
  projection이 버튼을 갱신해 제거한다.
- Source: https://docs.slack.dev/interactivity/handling-user-interaction/

## D-014 — Provider Outbox Destination Is Per Channel

- Status: accepted
- Decision: provider outbox destination은 `provider:{provider}:{channel_digest}`로
  channel 단위이고 `source_state_revision` CAS는 해당 destination의 마지막 event와
  비교한다. 반면 `supersession_key`는 `proposal-card:{proposal_id}`로 proposal 단위다.
  같은 Slack channel의 두 번째 proposal decision은 `OUTBOX_SOURCE_REVISION_CONFLICT`로
  실패하고 retry budget을 소진한 뒤 `dead_letter`에 도달한다. Package 2는 이 오류가
  worker를 탈출하지 않도록 봉쇄하고 `IngressService.stranded()`로 관측 가능하게만 한다.
  destination granularity 자체의 수정은 Package 3 Slack message projection 범위다.
- Source: MGC-012 Package 2 구현 중 발견, `tests/test_slack_ack_boundary.py`
  `test_projection_conflict_does_not_strand_a_leased_command`

## D-015 — Ingress Replay Precedes Authority Creation

- Status: accepted
- Decision: `decide_ingress_in_transaction`은 durable ingress row와 idempotency result를
  먼저 조회하고, 기존 결과가 없을 때만 Authority를 생성한다. SPEC Replay Precedence
  1–7 순서를 그대로 따른다. Replay 비교는 resolved Authority의 actor/channel 대신 durable
  ingress identity(`request_fingerprint`, `proposal_id`, `action`)에 결합한다. 이미 commit된
  decision이 finalize 실패 후 reclaim될 때 그 사이의 permission 회수나 binding 변경으로
  재거부되지 않는다. Replay 경로는 mutation을 하지 않으므로 lease fencing을 건너뛰어도
  안전하다. Lease를 잃은 worker는 뒤이은 finalize에서 `LEASE_LOST`를 받는다.
- Source: MGC-012 Package 2 round 2 failure/recovery review, `decisions.py`
  MGC-006 이후 존재했으나 worker가 없어 도달 불가였다.

## D-016 — Event Errors Are Terminal By Default

- Status: accepted
- Decision: `GovernanceEventError`는 integrity assertion이 지배적이므로 worker에서
  terminal을 기본값으로 분류하고 `AUDIT_SEQUENCE_CONFLICT`, `OUTBOX_SEQUENCE_CONFLICT`,
  `OUTBOX_SOURCE_REVISION_CONFLICT`만 재시도한다. 새 event code는 목록에 없으므로
  자동으로 `recovery_hold`가 된다. SPEC은 DB integrity failure에 즉시 fail-closed를
  요구하므로 hash chain 오류에 governed mutation을 재시도하지 않는다. D-014의
  `OUTBOX_SOURCE_REVISION_CONFLICT` 재시도 승인은 유지한다.
- Source: MGC-012 Package 2 round 2 failure/recovery review

## D-017 — Stranded Ingress Is Observable Without A Worker

- Status: accepted
- Decision: `IngressService.stranded()`는 `claim_next`가 다시 claim 하지 않을 command를
  정확히 반환한다. `dead_letter`, `recovery_hold`, attempt를 소진한 `retry_wait`, 그리고
  attempt를 소진한 채 lease가 만료된 `leased`를 포함한다. `dead_letter` 전이가
  `claim_next` 내부 sweep에서만 일어나므로 worker가 멈춘 상태에서도 가려지지 않는다.
  Lease가 살아 있는 command는 소유 worker가 아직 finalize 할 수 있으므로 제외한다.
  Storage 무결성 오류는 재시도하지 않는다. `sqlite_errorcode`의 primary byte가
  `SQLITE_CORRUPT` 또는 `SQLITE_NOTADB`면 `INGRESS_STORE_CORRUPT`로 즉시
  `recovery_hold`한다. 나머지 `sqlite3.DatabaseError`는 transient로 보고 retry budget을
  유지한다. `IngressDecisionWorker.committed_decision()`은 stranded command가 이미
  decision을 commit 했는지 답한다. 결과는 command의 credential과 action에 결합하므로
  같은 replay key 아래의 외부 row를 이 command의 decision으로 보고하지 않는다.
- Source: MGC-012 Package 2 round 3·4 contract·failure-recovery·regression review

## D-018 — Package 3 Scope And Method

- Status: accepted
- Decision: Package 3는 아래 넷으로 고정한다.

  1. **D-014를 범위에서 뺀다.** `provider:{provider}:{channel_digest}`는 세 곳에서
     생성되고 그중 둘이 `reconcile_connection`의 startup 검증이다. 형식을 바꾸면 이미
     커밋된 outbox row가 `_destination_manifest_digest` 불일치로 거부되므로 evidence
     migration이 필요하다. 별도 item으로 분리한다.
  2. **실제 network 호출을 하지 않는다.** `SlackProjectionDestination`은 주입받은
     transport Protocol을 부른다. dependency는 `pydantic`·`pyyaml`·`typer` 셋을
     유지한다. 실제 호출은 slice 4 Slack reference E2E가 맡는다. Slack API의 receipt
     필드와 error code는 구현 전에 공식 문서로 고정한다.
  3. **`reconcile()`은 message marker read-back으로 판정한다.** 전송 메시지에
     event 식별자를 담고 reconcile은 channel을 조회해 그 marker를 찾는다.
     `YamlProjectionDestination`이 자기 write를 알아보는 것과 같은 관계다. remote가
     유일한 진실 원천이라 two-phase 상태 불일치가 없다. 따라서 transport Protocol은
     send와 read를 모두 갖는다.
  4. **전 파이프라인을 쓰되 wave마다 review한다.** `/speckit-clarify` →
     `/speckit-plan` → `/taskify` → `/speckit-implement`를 wave 단위로 돌리고 각 wave
     종료 시 subagent 3인 review를 넣는다. `taskify_to_tasks_md.py`가 taskify의
     execution wave를 speckit Phase로 매핑하므로 phase는 vertical slice다. Package 2에서
     자기 검증 후 독립 review가 다섯 번 모두 blocker를 찾았다.

  `spec.md`는 `AGENTS.md ↔ constitution.md`와 같은 파생 문서로 만든다. 상단에 외부
  `SPEC.md`(sha 명시)와 `CURRENT_ITEM.md` A11의 파생이며 어긋나면 원본이 이긴다고
  적는다. `plan-template.md`가 `spec.md`를 입력으로 요구해 건너뛸 수 없다.
- Source: 2026-08-03 `/grill-me` 세션. 미결: feature 디렉터리 이름과 task ID prefix,
  wave별 review에 3 lens를 전부 쓸지 여부.
