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
  wave별 review에 3 lens를 전부 쓸지 여부. 미결은 D-019가 닫았고 항목 4의 반복 단위는
  D-019가 정정한다.

## D-019 — Package 3 Pipeline Binding

- Status: accepted
- Decision: D-018의 미결을 닫고 파이프라인 실행 형태를 아래로 고정한다.

  1. **feature 디렉터리는 MGC-012 전체를 담는다.** `spec.md`는 A1–A15를 전부 싣고
     plan·taskify 대상은 Package 3다. Package 1·2는 완료 상태로 표시만 한다.
     slice 4가 slice 3의 transport Protocol을 실제 호출로 채우므로 같은 계약을 두
     디렉터리로 쪼개지 않는다.
  2. **디렉터리는 `specs/001-mgc-012-slack-reference-adapter/`다.** `001`은
     `create-new-feature.sh:get_highest_from_specs`가 매기는 지역 일련번호라 workstream
     순서와 무관하다. slug에 `mgc-012`를 넣어 `docs/workstreams/`와의 연결을 디렉터리
     이름 수준에서 유지한다.
  3. **task ID prefix는 `MGC-012`다.** ID는 `MGC-012-T001` 형식이고
     `validate_task_manifest.py:38`의 `^[A-Z][A-Z0-9_-]*-T[0-9]{3,}$`를 통과한다.
     prefix를 파싱하는 코드는 없다. Package 4도 같은 prefix로 번호를 이어 쓴다.
  4. **wave마다 3 lens를 전부 쓴다.** 검토 범위는 그 wave의 diff로 한정한다. lens
     이름은 `workflow.yml`의 contract·failure-recovery·regression을 쓰고
     `CURRENT_ITEM.md` Review Team의 Slack 특화 서술을 각 lens의 focus로 넣는다. lens를
     고르는 판단 자체가 틀리는 것이 Package 2 round 5에서 실제로 일어났다 — 3 lens가
     0건을 낸 diff에서 regression lens가 blocker를 찾았다.
  5. **`/speckit-specify`를 돌리지 않는다.** `create-new-feature.sh`로 디렉터리와
     `.specify/feature.json`만 만들고 `spec.md` 본문은 파생으로 직접 쓴다. 생성기는
     자연어 설명에서 새 문장을 만드는데 A1–A15는 frozen이라 어긋남을 애초에 만들지
     않는다. script를 건너뛰면 `feature.json`이 없어 `/speckit-plan`이 feature를 못 찾는다.
  6. **반복 단위는 implement다.** clarify·plan·taskify는 Package 3 전체에 한 번 돌고
     implement만 wave 단위로 돌며 wave 끝마다 review한다. D-018 항목 4의 "wave 단위로
     네 skill을 돈다"는 성립하지 않는다 — wave는 `/taskify` 산출물
     (`index.yaml`의 `waves` → `taskify_to_tasks_md.py:135`)이라 taskify 전에는
     존재하지 않는다.
  7. **Slack API 사실은 `research.md`에 고정한다.** `plan-template.md:52`가 Phase 0
     산출물로 지정한 파일이다. receipt 필드와 error code를 공식 문서 URL과 조회 날짜로
     인용한다. `spec.md`에 넣지 않는다 — 원본이 외부 `SPEC.md`와 `CURRENT_ITEM.md`
     둘뿐이라는 파생 규칙을 깬다. `contracts/`의 Protocol 정의는 이 사실을 입력으로 쓴다.
  8. **Package 4는 이번 plan·taskify 범위 밖이다.** Slack test workspace 구성과
     credential 경로를 지금 모른다. `blocked` task로 넣어도 acceptance를 추정으로
     써야 하므로 넣지 않는다. Package 3 gate 통과 후 같은 manifest에 새 ID로 append한다.
  9. **`/speckit-clarify`를 건너뛴다.** 이 세션이 대체한다. spec은 A1–A15가 frozen이라
     모호성이 방법 쪽에 있었고 위 여덟 항목이 그걸 닫았다. clarify는 답을 `spec.md`에
     써넣어 파생 규칙에 예외를 뚫는다.
- Source: 2026-08-03 `/grill-me` 세션 2회차. 미결 없음. Package 4의 Slack test
  workspace 구성과 credential 경로는 Package 3 gate 후에 정한다.

## D-020 — Slack Failure Classification Is Retry-Biased

- Status: accepted
- Decision: MGC-012 Package 3 wave 1 review 결과를 아래로 고정한다.

  1. **Slack error code가 없는 실패는 status code로 가르지 않고 전부 retryable이다.**
     Slack은 application error를 HTTP 200 + `ok: false`로 준다. 진짜 HTTP 4xx는 429
     하나뿐이고 그건 별도 규칙이 잡는다. 따라서 code 없는 4xx는 거의 전부 proxy·WAF·
     load balancer가 낸 것이고 그건 transient다. 구현이 처음에 code 없는 non-429 4xx를
     terminal로 좁혔으나 reviewer 셋이 모두 반대했다.

     판단 근거는 비대칭이다. 영구 실패를 retryable로 잘못 분류해도 attempt를 소진하면
     같은 dead letter와 operator hold에 도달한다 — 기본값이면 약 75초 손해다. 반대로
     transient를 terminal로 분류하면 destination 전체가 즉시 멈추고, 그 hold는
     `governance_operator_holds`의 `CHECK (resolved_at IS NULL)` 때문에 되돌릴 수 없다.
     **retryable이 보수적인 쪽이다.** D-016의 fail-closed는 재시도가 도움이 안 된다는
     증거가 있는 Slack governance code에 적용되고, Slack body 없는 HTTP status는 Slack이
     처리했다는 증거가 없으므로 그 범주가 아니다.

  2. **error code는 정규화 후 비교한다.** 앞뒤 공백 제거, 소문자화, 빈 문자열은 code
     없음과 동일. `is None`만 검사하면 `""`가 allowlist를 못 만나고 terminal로 떨어져
     분류가 통째로 뒤집힌다. transport가 응답 JSON의 기본값으로 `""`를 넘기는 것은
     흔한 구현이다.

  3. **terminal code 문자열이 Slack 원인을 담는다** — `SLACK_PROJECTION_TERMINAL_ERROR:{code}`.
     dispatcher는 예외 객체를 버리고 `code` 문자열만 dead letter와 operator hold에 적는다.
     `governance/` 아래에 logging이 없어 원인이 다른 곳에도 안 남는다. 원인이 없으면
     `invalid_auth`, `channel_not_found`, `msg_blocks_too_long`, `not_in_channel`이 전부
     같은 row가 되는데 operator의 복구 행동은 넷이 다 다르다.

     error code column에 CHECK나 길이 제약이 없음을 확인했다 — `last_error_code`
     (`migrations.py:269`), dead letter `error_code` (`:614`), operator hold `reason_code`
     (`:625`). 문자열을 비교하는 기존 handler는 **있다** — `events.py:2644`, `:2681`,
     `:2845`, `:2888` 넷이고 전부 `last_error_code = 'OUTBOX_LEASE_EXPIRED'`만 본다.
     terminal 경로는 `unreconcilable=True`라 `retry_wait`를 거치지 않고 네 곳 전부
     `state`가 `pending` 또는 `retry_wait`인 행만 보므로 만나지 않는다.

     저장될 접미사는 허용 밖 문자를 `_`로 바꾸고 64자로 자른다. **분류에는 이 형식을
     적용하지 않는다** — 적용하면 형식이 이상한 terminal code가 code 없음이 되어 항목 1의
     retryable 경로로 새고, 그 경로는 항목 6대로 원인을 남기지 않는다. 다듬되 버리지
     않는다. 원본은 예외 객체에 진단용으로 남긴다.

  4. **transport Protocol에 의무 둘을 명시한다.** signature로 강제할 수 없어 계약 문서와
     docstring에 적는다. (a) 모든 실패를 `SlackTransportError`로 감싼다 — 다른 예외가
     새면 분류가 아예 돌지 않고 무조건 재시도가 된다. (b) 호출 시간을
     `OutboxConfig.lease_seconds`보다 짧게 묶는다 — lease 만료 후 실패하면 `fail()`이
     lease conflict로 터져 terminal 판정이 통째로 버려지고 dead letter도 hold도 안 생긴다.

  5. **`Retry-After` 잔여 위험을 수용하되 하한 상수는 두지 않는다.** 기본값이면 재시도
     예산이 75초에 소진된다 (대기 5·10·20·40). `Retry-After`가 그보다 크면 Slack이
     기다리라고 한 창 안에서 attempt를 다 쓰고 되돌릴 수 없는 hold에 도달한다. rate
     limit이 app/workspace 범위라 같은 app의 다른 트래픽과 예산을 공유한다.

     **그 위험이 실제로 발생하는지는 판정할 수 없다.** Slack은 `Retry-After`의 상한을
     문서화하지 않는다. 문서화된 숫자는 30초 예시 하나뿐이다 (research S4, 2026-08-05
     재조회). 이 Decision의 이전 판은 "30~60초가 흔하다"를 근거로 하한 상수를 두었으나
     **그 문장은 S4에 없었다.** wave 1 round 4 review에서 드러났고 상수와 함께 뺐다.
     근거 없는 숫자를 기계적 관문으로 만들면 만족시킨 쪽이 안전하다고 잘못 믿는다.

     대신 test가 기본 schedule을 사실로 고정한다 — 대기 `[5, 10, 20, 40]`, 합 75.
     Package 4가 `OutboxConfig`를 만들 때 이 schedule과 실제 관측한 `Retry-After`를 함께
     입력으로 쓴다. R-007의 dispatcher 계약 변경 금지는 유지한다.

  6. **retryable 경로의 원인 소실은 wave 2로 넘긴다.** terminal은 3에서 닫혔으나
     retryable로 분류된 실패가 attempt를 소진하면 `deliver_next`의 generic handler가
     예외를 버리고 `OUTBOX_DELIVERY_FAILED` 상수만 남긴다 (`events.py:2864`). 항목 1의
     결정이 이 경로를 넓혔으므로 같은 Package 안에서 닫아야 한다. 고치려면 `events.py`를
     바꿔야 하는데 그건 MGC-012-T001의 `forbidden_paths`다. T002에 명시적 항목으로
     넘긴다 — destination이 재감싸서 해결되면 dispatcher를 안 건드려도 된다.
- Source: 2026-08-03 사용자 결정. MGC-012 Package 3 wave 1의 contract·failure-recovery·
  regression review 결과를 근거로 제시하고 사용자가 항목별로 선택했다. review 산출물은
  근거이지 승인 주체가 아니다 (D-004는 review를 gate로 규정한다). 항목 1·3·5의 선택지와
  그 대가는 사용자에게 제시된 뒤 확정됐다. wave 1 gate 결과는 이 Decision이 아니라
  CHECKPOINTS 기록에 남긴다.

## D-021 — Exhausted Retryable Failures Carry Their Cause

- Status: accepted
- Decision: D-020 항목 6이 wave 2로 넘긴 선택을 아래로 닫는다. `OutboxDispatcher`는 안
  고친다.

  `SlackProjectionDestination` 생성자가 `max_attempts`를 받는다. `send()`가
  `event.attempts >= max_attempts`인 호출에서 retryable 실패를 만나면 원래 예외 대신
  `SLACK_PROJECTION_RETRY_EXHAUSTED:{suffix}`를 code로 갖는 `OutboxReconcileError`를
  올린다. 그 미만이면 원래 예외를 그대로 올려 `retry_wait` 경로를 탄다.

  근거는 셋이다.

  1. `attempts`는 `claim_next`가 claim 시점에, `attempts < max_attempts`인 동안 증가시킨다
     (`events.py:2672`의 `CASE WHEN attempts < ?`). `send()` 안에서 보는 값이 현재 시도
     번호이고 상한을 넘지 않는다. 증가하지 않는 재claim은 lease 만료 replay 하나뿐인데
     그 경로는 `deliver_next`가 `send()` 앞에서 가로챈다 (`events.py:2843`-`2853`).
  2. `fail()`은 `exhausted or unreconcilable`을 같은 `_dead_letter`로 보낸다
     (`events.py:2778`). 마지막 attempt라면 state 전이가 **바뀌지 않는다.** 달라지는 것은
     dead letter와 operator hold에 적히는 error_code 하나다. 재시도 횟수를 줄이지 않는다.
  3. 이것이 없으면 연결 실패, timeout, `ratelimited`, `internal_error`가 전부
     `OUTBOX_DELIVERY_FAILED` 한 줄이 된다 (`events.py:2864`가 예외를 버리고 상수만
     남긴다). operator의 복구 행동은 넷이 다 다르다.

  **재분류가 아니라 소진 시점의 기록이다.** 분류 규칙 C-3는 그대로다. terminal 판정은
  attempt와 무관하게 여전히 즉시 DLQ다.

  `suffix`는 Slack code가 있으면 그 code다. transport층 실패라 code가 없으면 원인 예외의
  class 이름을 `transport_{name}`으로 넣는다. 둘 다 없으면 `no_slack_code`다. terminal
  경로의 `unknown`과 다른 문자열을 쓴다 — 같으면 두 사건이 같은 row가 된다. 형식 강제는
  `persisted_code_suffix`가 저장 직전에만 한다 (D-020 항목 3과 같다).

  **대가 하나를 명시한다.** `max_attempts`는 호출자가 dispatcher의
  `OutboxConfig.max_attempts`와 같게 줘야 하고 destination은 그것을 검증할 경로가 없다.
  더 크면 이 규칙이 안 돌아 원인이 그대로 사라진다. 더 작으면 남은 attempt를 두고
  되돌릴 수 없는 hold를 만든다. 대안이었던 `events.py` 수정은 MGC-008에서 gate PASS한
  dispatcher 계약을 건드리고 T002의 `forbidden_paths`다.
- Source: 2026-08-05 사용자 결정. D-020 항목 6이 남긴 선택지 셋(contract C-2에
  `max_attempts` 추가 / scope 확대 후 `events.py` 수정 / 항목 6을 다시 미룸)을 제시하고
  사용자가 첫째를 골랐다. 반영 위치는 `contracts/slack-transport.md` C-2·C-3.1과
  `task-manifests/MGC-012-T002.yaml`이다.

## D-022 — Pre-Send Validation Failures Are Unreconcilable

- Status: accepted
- Decision: `ProjectionDestination.send()`의 두 사전 검증 실패를 `GovernanceEventError`가
  아니라 `OutboxReconcileError`로 던진다. `SlackProjectionDestination`과
  `YamlProjectionDestination` 둘 다 바꾼다.

  - `OUTBOX_DESTINATION_MISMATCH`
  - `OUTBOX_PAYLOAD_INTEGRITY_FAILURE`

  근거는 셋이다.

  1. `OutboxReconcileError`는 `GovernanceEventError`의 **자식**이다 (`events.py:24`-`35`).
     그래서 부모로 던지면 `deliver_next`의 `except OutboxReconcileError`
     (`events.py:2856`)가 못 잡고 generic handler로 떨어져 (`events.py:2864`) 원인이
     `OUTBOX_DELIVERY_FAILED` 상수로 덮인다. payload 손상(무결성 사건)과 destination
     배선 버그가 평범한 전달 실패와 같은 row가 된다.
  2. **재시도가 확정적으로 무의미하다.** `payload`와 `payload_digest`, `destination_ref`는
     event row의 불변 column이다. 5번을 더 시도해도 같은 값을 읽는다. 그 사이 약 75초를
     쓰고, 결말은 어차피 같은 dead letter다.
  3. 두 destination을 같이 바꾼다. 한쪽만 바꾸면 같은 조건을 두 destination이 다르게
     다루고, 그 차이는 계약이 아니라 사고다.

  이것은 D-021과 같은 결함의 나머지 절반이다. D-021이 retryable **분류** 경로를 닫았고
  이것이 **사전 검증** 경로를 닫는다.

  **대가.** `YamlProjectionDestination`은 `APPROVALS.md`의 `APR-002`(MGC-008, PASS,
  2026-07-30)가 덮는 subsystem이다. 그 승인 기록에 이 두 예외의 종류를 규정한 문장은
  없다 — wave 2 reviewer가 `docs/workstreams/messenger-governance-closure-v3/` 전체를
  훑어 확인했다. 그래서 `supersedes` 대상 Decision이 없고 이 항목이 최초 규정이다.
  gate PASS한 subsystem의 code를 바꾼 사실만 여기 남긴다.

  `projections.py`는 MGC-012-T002의 `forbidden_paths`였다. 사용자가 scope 확대를
  승인해 열었다. `events.py`는 열지 않았다 — 새 예외 class를 만들지 않고 기존
  `OutboxReconcileError`를 그대로 쓰므로 dispatcher 계약은 그대로다. `except
  GovernanceEventError`로 잡던 기존 호출자는 자식 class도 그대로 잡으므로 영향이 없다.

  `deliver_next`가 이 예외를 `unreconcilable=True`로 보내 만드는 operator hold는
  `governance_operator_holds`의 `CHECK (resolved_at IS NULL)` 때문에 되돌릴 수 없다.
  그것을 수용한다 — 두 조건은 transient가 아니라 확정된 결함이고, D-016의 fail-closed가
  적용되는 범주다.
- Source: 2026-08-05 사용자 결정. MGC-012 Package 3 wave 2 review의 failure/recovery lens가
  P1로 지적했고 reviewer가 실물 dispatcher로 재현했다. 선택지 셋(Slack만 변경 / 두
  destination 변경 / 수용 후 기록)을 제시하고 사용자가 둘째를 골랐다. review 산출물은
  근거이지 승인 주체가 아니다 (D-004). 기록은
  `CHECKPOINTS/MGC-012-package-3-wave-2-review-2026-08-05.md`다.
