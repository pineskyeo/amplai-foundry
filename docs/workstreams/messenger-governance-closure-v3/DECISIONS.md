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

- Status: accepted (**항목 1은 superseded**)
- superseded_by: D-027 (항목 1만. 나머지 여덟은 유효하다)
- Decision: D-018의 미결을 닫고 파이프라인 실행 형태를 아래로 고정한다.

  1. ~~**feature 디렉터리는 MGC-012 전체를 담는다.**~~ **D-027이 뒤집었다.** Package 4는
     `specs/002-mgc-012-package-4-slack-reference-e2e/`를 쓴다. 아래 원문은 기록으로
     남긴다. **`spec.md`는 A1–A15를 전부 싣고**
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

  6. **retryable 경로의 원인 소실은 wave 2로 넘긴다.** (**닫힘 — D-021**.) terminal은 3에서 닫혔으나
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

## D-023 — First Delivery Needs A Not-Sent Proof That Reconcile Cannot Read From Slack

- Status: accepted
- Decision: `reconcile()` 의 판정 규칙을 셋에서 넷으로 늘리고, 첫 시도에는 Slack 을 아예
  조회하지 않는다. `plan.md` P-001을 좁힌다.

  **문제.** C-2.2의 세 규칙으로는 destination의 첫 Card가 나가지 못한다. `deliver_next`는
  첫 시도를 포함해 매번 `reconcile`을 먼저 부른다 (`events.py:2842`). `destination_sequence`
  가 1이면 "더 낮은 sequence marker"가 존재할 수 없어 규칙 2가 절대 안 맞고, 아직 안
  보냈으니 규칙 1도 안 맞는다. 그래서 규칙 3으로 떨어져 되돌릴 수 없는 hold가 된다.
  `YamlProjectionDestination`은 같은 자리를 file 부재 → `None`으로 처리한다
  (`projections.py:44`). Slack 쪽에는 그 대응물이 없었다.

  첫 시도를 건너뛰는 것만으로는 부족하다. 첫 Card가 일시적 transport 오류를 한 번만
  만나도 두 번째 시도에서 같은 자리에 떨어진다 — 재시도하라고 만든 분류가 재시도
  순간에 죽는다.

  **결정 넷.**

  1. **`reconcile()`이 local outbox state를 판정에 써도 된다.** D-018 항목 3의 "remote가
     유일한 진실 원천"을 이렇게 좁힌다 — *무엇이 실제로 나갔는지는 Slack만 안다. 보낼
     시도를 한 적이 있는지는 outbox row가 안다.* R-003이 기각한 것은 "local에 ts를 저장해
     그것으로 판정" 이었고 그 함정은 저장이 send **뒤에** 일어나는 데서 온다. `attempts`는
     `claim_next`가 자기 transaction에서 commit하므로 send보다 **먼저** durable하다. 방향이
     반대라 같은 함정이 아니다. `deliver_next` 자신도 이미 `last_error_code`와 `attempts`로
     판정을 가른다 (`events.py:2843`).
  2. **첫 시도면 Slack을 조회하지 않고 곧바로 `None`을 반환한다.** 판정 조건은
     `attempts == 1` **이고** `last_error_code is None` 둘 다다. 전자만 보면
     `max_attempts == 1` 구성에서 두 번째 claim이 1로 보인다 — `claim_next`의 증가가
     `CASE WHEN attempts < max_attempts` 라 상한에서 멈추기 때문이다 (`events.py:2672`).
     그 재claim은 `last_error_code = 'OUTBOX_LEASE_EXPIRED'`를 요구하므로 (`events.py:2644`)
     둘을 함께 보면 원천적으로 막힌다. Outbox row는 언제나 `attempts=0`,
     `last_error_code=NULL`로 생성되고 (`events.py:2358`-`2359`) 이 값을 되돌리는 곳은 `mark_delivered` 하나뿐인데 (`events.py:2729`) 그 row 는 `delivered`라
     `claim_next`가 다시 claim 하지 않는다.

     조회를 건너뛰는 것은 부수 효과가 아니라 의도다. 첫 시도에 찾을 marker는 정의상
     존재하지 않고, `conversations.history`는 Tier 2다 (research S4). 대부분의 Card는 첫
     시도에 성공하므로 평상시 history 조회가 0회가 된다 (R-002의 비용 우려를 닫는다).
  3. **"채널을 끝까지 훑었는데 없음"을 미전송의 증거로 인정한다.** 규칙이 넷이 된다.

     | 발견한 것 | 판정 |
     |---|---|
     | 이 event의 marker | 전달 완료 |
     | 같은 destination_ref의 더 낮은 destination_sequence marker | 미전송 |
     | **`next_cursor`가 없어 history가 소진됨, 둘 다 없음** | **미전송** |
     | 상한까지 훑고 멈춤, 둘 다 없음 | 판정 불가 → hold |

     P-001이 막으려던 사고는 "Card가 채널에 **실제로 있는데** 조회 범위 밖이라 못 찾고
     재전송해 **두 장**이 되는 것"이다. 그 사고는 **범위를 다 못 본 경우**에만 성립한다.
     history가 소진됐다면 없는 것이 확정이고 두 장이 될 수 없다. Slack은 이 둘을
     `next_cursor` 유무로 구분해 준다 (C-1.2).
  4. **`limit=999`, `max_history_pages=5`.** limit은 상한을 그대로 쓴다 — Slack이 세는
     것은 message 수가 아니라 호출 수라 한 번에 꽉 채우는 쪽이 손해가 없다 (S2, S4).

     **5는 판단이지 측정이 아니다.** 근거로 쓸 수 있는 fact는 셋뿐이다 — page당 999 상한,
     Tier 2 분당 20+, 그리고 항목 2로 이 조회가 재시도 때만 일어난다는 것. 정작 필요한
     숫자인 "Card 한 장과 다음 Card 사이에 쌓이는 message 수"는 workspace에 달렸고
     **모른다.** 그래서 값 옆에 판단임을 명시하고, 상한에 걸려 생긴 hold는 다른 원인의
     hold와 error code로 구분되게 한다. 구분이 없으면 5가 작았다는 것을 알 방법이 없다.
     실측은 Package 4 몫이다.

  **P-001과의 관계.** P-001("사람이 지운 Card는 다시 보내지 않는다")을 폐기하지 않고
  좁힌다. history를 소진할 수 있는 작은 채널에서는 지워진 Card가 다시 나타난다. 큰
  채널에서는 상한에 걸려 종전대로 hold다. 저울은 한쪽으로 크게 기운다 — 좁히지 않으면
  정상적인 일시 오류 한 번이 되돌릴 수 없는 hold를 만들고, 좁히면 사람이 일부러 지운
  Card가 다시 뜬다. 후자는 다시 지우면 되고 전자는 되돌릴 수 없다. Card가 **두 장**이
  되는 일은 어느 쪽에서도 없다.

  `deliver_next`의 마지막 안전망은 그대로 남는다 — 최종 시도에서 `reconcile`이 `None`을
  반환하고 `last_error_code`가 `OUTBOX_LEASE_EXPIRED`면 send 없이
  `OUTBOX_POST_SEND_RECONCILE_REQUIRED`로 끝난다 (`events.py:2843`-`2853`).

  **`events.py`는 고치지 않는다.** 네 결정 전부 destination 안에서 닫힌다.
- Source: 2026-08-05 사용자 결정. `/speckit-analyze` 후속으로 T003 manifest를 고치던 중
  발견했고 OQ-004로 등록한 뒤 `/grill-me`로 네 갈래를 순서대로 물어 확정했다. 후보였던
  "destination row의 `delivered_sequence`를 본다"는 기각했다 — `OutboxEventView`에 그
  필드가 없어 `events.py`를 고쳐야 하고 (T003 `forbidden_paths`), 그 값은 **이전** event를
  말할 뿐 이 event가 posted됐는지에 답하지 못한다. OQ-001과 OQ-004를 함께 닫는다.

## D-024 — Reconcile Needs Our App Identity, And Unreadable Metadata Is Fail-Closed

- Status: accepted
- Decision: MGC-012-T003 구현과 wave 3 review 결과를 아래 둘로 고정한다.

  1. **`SlackProjectionDestination` 생성자가 `app_id`를 받는다.** C-2에 자리를 만든다.

     AC-04는 "다른 app이 심은 metadata를 배제하라"고 요구하는데 C-2에 우리 app id를 받을
     자리가 없었다. 배제는 그 값 없이는 원리적으로 불가능하다 — `SlackHistoryMessage`는
     `ts`·`metadata`·`app_id` 셋뿐이고, marker의 모양은 공개된 구조라 다른 app이 같은
     모양을 심을 수 있다 (research S3). 배제를 못 하면 남의 message를 우리 Card로
     확정하고 진짜 Card는 영영 안 나간다.

     D-018 항목 1은 어기지 않는다. 그것은 `provider:{provider}:{channel_digest}` 형식의
     동결이고 `destination_ref`는 그대로다.

     **절차 지적을 수용한다.** T002는 같은 종류의 contract 변경(`max_attempts`)을 D-021로
     남기고 사용자 선택을 받았다. 이번에는 구현 중에 넣고 manifest scope에만 적었다.
     wave 3 contract review가 Blocking-P2로 지적했고 맞다. 이 항목이 그 기록이다.

  2. **`event_payload`가 통째로 없는 우리 message를 만나면 fail-closed한다** —
     `SLACK_PROJECTION_METADATA_UNREADABLE`.

     C-1.2는 `read_history` 구현체가 `include_all_metadata=true`를 붙이도록 요구하지만
     signature로 강제할 수 없다. 안 붙이면 `event_type`만 오고 `event_payload`가 안 온다
     (research S3). 그러면 모든 marker가 안 읽히고, D-023 항목 3이 history 소진을
     미전송으로 판정하므로 **매 재시도마다 Card가 한 장씩 는다.** hold가 아니라 중복이라
     조용하다. wave 3 failure/recovery review가 P1로 지적했다.

     **지문으로 잡는다** — 우리 `app_id`가 보냈고 `event_type`도 우리 것인데
     `event_payload` 키가 없는 message. `build_slack_marker`는 그 키를 항상 넣으므로
     오탐이 없다. 사람이 Card를 지웠거나 이전 event가 전부 superseded인 경우와도 겹치지
     않는다 — 그 경우엔 우리 app의 message 자체가 없다.

     **기각한 대안**: "소진 시점에 우리 marker를 하나도 못 봤고 `destination_sequence > 1`
     이면 판정 불가로 본다" (review 제안). 정상 흐름에 부당한 hold를 만든다.
     `supersession_key`는 proposal 단위이고 destination은 channel 단위라, 어떤 채널의 첫
     proposal 결정이 dispatcher가 돌기 전에 superseded되면 sequence 2가 그 채널의 실제 첫
     전달이고 history에 우리 marker가 하나도 없다. 정상인데 되돌릴 수 없는 hold가 된다.

  함께 고친 것 셋. 전부 wave 3 review 반영이고 계약 변경이라 여기 적는다.

  - **`read_history`의 최신-우선 정렬을 C-1의 세 번째 의무로 명시한다.** 그 순서가 계약에
    없었고, 뒤집히면 하위 sequence marker를 우리 marker보다 먼저 만나 중복 Card가 난다.
    **Slack 공식 문서에서 이 사실을 확인하지 못했다** — 그래서 계약으로 적고 Package 4
    확인 항목으로 남긴다. destination은 page **안**에서는 우리 marker를 먼저 찾는 방식으로
    이 의존을 없앴다. page **사이**는 cursor를 우리가 만들지 않아 막을 수 없다.
  - **`event_id`는 같은데 `payload_digest`가 다른 marker를 만나면 즉시 멈춘다** —
    `SLACK_PROJECTION_MARKER_DIGEST_MISMATCH`. 계약은 원래 "상한까지 못 찾으면 hold"였는데
    D-023이 history 소진을 미전송으로 인정하면서 그 경로가 재전송으로 샜다. D-023이 "Card가
    두 장이 되는 일은 어느 쪽에서도 없다"고 약속했으므로 코드를 계약에 맞췄다.
  - **lease 예산 기준을 호출당에서 `reconcile()` + `send()` 전체로 바꾼다.** 한 번의
    `deliver_next`는 `read_history`를 최대 5회 부른 뒤 `post_message`를 한 번 부른다.
    호출 하나하나가 lease 안에 들어와도 합이 넘으면 `mark_delivered`가 lease conflict로
    터져 receipt를 잃는다.
- Source: 2026-08-05 MGC-012-T003 구현과 wave 3 review round 1. 항목 1은 구현 중 발견해
  넣은 뒤 review 지적을 받아 사후 기록한 것이다. 항목 2는 review가 P1로 제기한 문제에
  대해 review 제안을 기각하고 다른 해법을 택했다 — 기각 근거는 위에 적었다. review
  산출물은 근거이지 승인 주체가 아니다 (D-004). 기록은
  `CHECKPOINTS/MGC-012-package-3-wave-3-review-2026-08-05.md`다.

## D-025 — Wave 4 Rewrote Two Stale Acceptance Behaviours And Added Three More

- Status: accepted
- Decision: `MGC-012-T005`의 acceptance를 구현 중에 고쳐 쓰고 진행한 것을 사후 승인한다.
  loop stop_condition `acceptance_conflicts_with_approved_spec`에 해당하는 상태였으나
  멈추지 않았다.

  **실제로 바꾼 것은 다섯이다.** 제목이 "둘"이라고만 쓰면 범위를 축소하는 것이다.
  AC-02 재작성, AC-07 재작성, AC-08·AC-09·AC-10 신설, 그리고 `scope.include`와
  `implementation.guidance` 재작성.

  **왜 stop_condition이 성립했나.** 두 acceptance가 approved source와 어긋나 있었다.

  1. **AC-02 원문은 "지워진 Card는 무조건 hold"였다.** D-023 항목 3이 history 소진을
     미전송의 증거로 인정하면서 갈래가 둘로 갈렸는데 (`contracts/slack-transport.md` C-2.2
     표 3·4행, `spec.md` SC-005) manifest만 그 이전 판으로 남아 있었다.

     **결과는 조건부다.** D-023 항목 3은 이미 `slack_projection.py`에 들어가 있고
     (`test_exhausted_history_is_not_undecidable`) T005는 production code를 한 줄도 안
     고쳤다. 원문 AC-02를 문자 그대로 구현하면 당장 일어나는 일은 **test 하나가 실패하는
     것**이다. "모든 destination의 첫 Card가 영구 hold"는 구현자가 그 실패를 근거로
     `slack_projection.py`까지 되돌렸을 때 성립한다. 초판은 이 조건문을 무조건문으로
     격상해 썼다 (round 2 contract lens A-3).
  2. **AC-07 원문은 존재하지 않는 state를 읽으라고 적혀 있었다.** "다른 Provider의
     activation state"를 담는 table이 이 repo에 없다 — `governance_*` table을 훑어
     확인했다. `activation`이라는 이름이 붙은 것은 셋이고 전부 다른 것이다:
     legacy migration lifecycle, `governance_definition_revisions`의
     `activated_from_status`·`activated_at` (proposal activation), 그리고 actor permission
     `activation.manage`. 초판은 이것을 "legacy migration 도메인뿐"이라고 썼고 그건
     거짓이다 (round 2 contract lens A-1). 결론은 그대로다 — provider activation은 없다.
     Slack activation rollout은 `spec.md`가 Out Of Scope로 두고 Package 4로 미룬 것이다.

  **수정이 acceptance를 낮췄나.** 한 방향으로만 말할 수 없다. 정확히는 **주고받았다.**

  - **늘어난 것**: D-023 항목 4의 "구분되는 error_code"를 acceptance로 승격했다. 원문에
    없던 요구다. AC-08·AC-09·AC-10이 원문에 없던 갈래 셋을 덮는다.
  - **줄어든 것**: 원문 AC-02의 given은 "history의 그 message를 지운 상태"라 소진 갈래를
    **포함**했고 그 갈래도 hold를 요구했다. 새 AC-02는 상한 도달만 hold로 두고 소진
    갈래는 AC-08에서 재전송이다. **hold 요구 하나가 빠졌다.** 초판은 "원문이 요구하던
    hold 갈래는 하나도 빠지지 않았다"고 썼고 그건 거짓이다 (round 2 contract lens A-2).

    빠진 것이 정당한 이유는 D-023이 그렇게 결정했기 때문이다. 그러나 "요구가 늘기만
    했다"는 서술은 사실이 아니다.

  **선택지는 둘이 아니라 셋이었다.** 초판은 "멈춘다 vs 고친다"로 논증했는데 같은 repo에
  세 번째 선례가 있다 — `MGC-012-T003`은 검사 불가한 AC-05를 **고쳐 쓰지 않고** AC 안에
  caveat을 붙인 뒤 `completion.all_acceptance_passed: false`로 기록했다. 특히 AC-07에는
  그대로 적용할 수 있었다 (round 2 contract lens A-5).

  **왜 사용자 결정이 필요했나.** D-021과 D-022의 Source는 "**사용자 결정.** 선택지 셋을
  제시하고 사용자가 N번째를 골랐다"이고, D-022는 "**review 산출물은 근거이지 승인 주체가
  아니다 (D-004)**"를 명시한다. D-024가 지적받은 결함은 "기록이 없다"가 아니라 "사용자
  선택 없이 구현 중에 넣었다"였다. 초판 D-025는 그 문장을 인용해 놓고 `Status: accepted`로
  자가 승인했고, Source에 contract lens의 판정을 정당화 근거로 들었다 — D-004가 금지한
  바로 그것이다 (round 2 contract lens BP2-4-residual). 그래서 `proposed`로 내린 뒤
  선택지 셋을 제시해 결정을 받았다. 제시한 셋은 아래다.

  1. 사후 승인 — 정정을 그대로 두고 gate로 간다.
  2. AC-07만 T003 방식으로 — AC-02 정정은 승인하되 AC-07은 원문을 남기고 caveat만 붙인 뒤
     `all_acceptance_passed: false`로 기록한다.
  3. 전부 되돌리고 멈춘다 — acceptance를 원문으로 복원하고 stop_condition을 따른다.
     `slack_projection.py`의 D-023 구현까지 되돌려야 성립하므로 Package 3 재설계가 된다.

  **사용자가 1번을 골랐다.**

  **함께 닫은 것과, 그 과정에서 낸 새 결함.** wave 4 round 1 review가 낸 P1은
  `OUTBOX_POST_SEND_RECONCILE_REQUIRED`에 repo 전체 test가 없다는 것이었다. AC-09를
  추가해 닫았다.

  그런데 round 1은 그 경계를 **`attempts` 단독**으로 적었고 그것은 거짓이다. guard는
  `last_error_code == 'OUTBOX_LEASE_EXPIRED'` **AND** `attempts >= max_attempts` 둘의
  AND다 (`events.py:2844`-`2847`). round 2가 probe로 반증했다 — `max_attempts=2`에서
  send 성공 후 `OUTBOX_DELIVERY_FAILED`로 실패를 기록하고 Card를 지우면
  `attempts == max_attempts`인데도 guard가 안 걸리고 재전송한다. AC-08·AC-09 문언을
  고치고 반대쪽 경계를 AC-10과 test로 고정했다 (round 2 contract lens NEW-1).

- Source: **사용자 결정.** 선택지 셋을 제시하고 사용자가 첫째를 골랐다 (2026-08-07).
  근거는 2026-08-07 MGC-012-T004·T005 구현과 wave 4 three-lens review 2라운드다. round 2가
  이 항목 초판에서 거짓 3건과 범위 축소 1건, 누락된 선택지 1건을 찾아 위와 같이 고쳤고,
  자가 승인이라는 지적을 받아 `proposed`로 내린 뒤 결정을 받았다. **review는 근거이지
  승인 주체가 아니다 (D-004).**

## D-026 — Package 4 Gets A Real Slack Test Workspace, And Activation Isolation Stays A Check

- Status: accepted
- Decision: D-019 항목 8이 Package 3 gate 후로 미룬 두 가지를 여기서 정한다.

  **1. 테스트용 Slack workspace와 app을 새로 만든다.** 회사 workspace를 쓰지 않는다.
  그래서 Package 4는 fake transport가 아니라 **실제 Slack을 치는 reference E2E**를 갖는다.
  A15의 "Slack reference E2E"를 문언 그대로 충족한다.

  D-019 항목 8이 task를 안 만든 이유가 "acceptance를 추정으로 써야 한다"였고, 그 전제가
  이 결정으로 없어졌다. 다만 **workspace는 아직 만들어지지 않았다.** 존재가 acceptance의
  선행 조건이므로 plan 단계에서 그 생성을 external dependency로 잡는다.

  **2. Package 4의 "activation isolation"은 격리 검증뿐이다.** `ActivationEvidence`
  구조와 `Global Core → Provider → Project-Provider → Feature` 4단계 Gate machinery는
  **MGC-015 Activation Control**이 만든다. Package 4는 A14 — Slack adapter가 Telegram
  또는 다른 Provider의 상태를 바꾸지 않는 것 — 만 검증한다.

  근거는 queue다. `MGC-015 — Activation Control`이 별도 item으로 이미 서 있고,
  `CURRENT_ITEM.md` Out Of Scope가 "Slack activation rollout"을 뺀다. Gate machinery를
  Package 4로 당기면 두 item이 겹쳐 queue를 재정렬해야 한다.

  **결과 하나를 미리 적는다.** MGC-012-T005 AC-07은 activation state를 담는 table이 없어
  provider별 outbox destination row와 event state라는 **proxy**로 검증했다. 그 proxy는
  MGC-015가 activation state를 만들 때까지 유효하고, 만들어지면 그때 진짜 state를 읽는
  검증으로 바꾼다. Package 4가 아니라 MGC-015의 일이다.

  **미확인 하나가 남는다.** `conversations.history`에 필요한 OAuth scope를 공식 문서에서
  아직 확인하지 못했다 (`index.yaml` coverage.deferred `W3-history-oauth-scope`).
  `chat:write`만 있는 설치는 send는 성공하고 첫 재시도의 read에서 `missing_scope` →
  terminal → 되돌릴 수 없는 hold가 된다. **추측해서 적지 않는다.** `/speckit-plan`
  Phase 0에서 `research.md`에 공식 문서로 고정한 뒤 app 설치 절차에 넣는다.

  credential이 프로세스에 도달하는 경로도 아직 없다 — repo 전체에 Slack 설정을 읽는
  `os.environ`이 한 줄도 없고 `SlackInstallationPolicy`를 코드에서 직접 만들어 넘긴다.
  그 경로 설계는 plan의 몫이고 이 결정에서 정하지 않는다.

- Source: **사용자 결정.** 두 질문에 각각 "만들 수 있다"와 "격리 검증만"을 골랐다
  (2026-08-07). Package 3 gate `b0858c1` 직후 `/taskify` 시도에서 설계 원본 부재가 드러나
  물었다.

## D-027 — Package 4 Gets Its Own Feature Directory

- Status: accepted
- supersedes: D-019 항목 1 (나머지 여덟 항목은 유효하다)
- Decision: Package 4의 plan과 설계 산출물은 새 feature 디렉터리
  `specs/002-mgc-012-package-4-slack-reference-e2e/`에 둔다. `.specify/feature.json`을
  거기로 옮긴다. `specs/001-mgc-012-slack-reference-adapter/`는 Package 3의 기록으로
  그대로 얼린다.

  **왜 뒤집나.** D-019 항목 1은 "slice 4가 slice 3의 transport Protocol을 실제 호출로
  채우므로 같은 계약을 두 디렉터리로 쪼개지 않는다"였다. 그 걱정 자체는 지금도 옳다.
  뒤집는 이유는 다른 축이다 — **도구 정합성**이다.

  `plan.md`는 Package 3 전용이고 (`# Implementation Plan: MGC-012 Package 3`) 파일 바깥
  24곳이 참조한다. manifest 다섯이 `section: P-001`·`P-002`로 인용하고 gate·review
  checkpoint와 `quickstart.md`·`research.md`·`spec.md`도 인용한다. 덮어쓰면 gate 기록의
  추적성이 끊기고, 이름을 바꾸면 24곳을 고쳐야 한다.

  남는 선택은 같은 디렉터리에 `plan-package-4.md`를 두는 것인데, `check-prerequisites.sh`
  와 `setup-plan.sh`는 `plan.md`만 찾는다. 그러면 `/speckit-analyze`와 `/taskify`가
  **조용히 Package 3 plan을 읽는다.** 이 repo는 같은 함정을 이미 세 번 밟았다 — 존재하지
  않는 `/pinesky-workstream-gate`, 인자가 없어 실행조차 안 되던 `amplai-foundry lint`,
  그리고 Package 3 범위인 줄 모르고 시도한 2026-08-07의 `/taskify`. 문서가 도구보다
  앞서가면 조용히 틀린 것을 읽는다.

  **계약은 여전히 쪼개지 않는다.** `002/contracts/`는 `slack-transport.md`의 C-1을 다시
  쓰지 않는다. 001을 권위로 **참조**하고, Package 4 고유 계약(credential 주입 경로, E2E
  harness)만 새로 쓴다. 이 repo는 파생·참조 문서를 이미 그렇게 쓴다 —
  `AGENTS.md ↔ constitution.md`, `spec.md ↔ 외부 SPEC`. D-019 항목 1의 의도는 지켜진다.

  **유지되는 것 둘.** task ID prefix는 계속 `MGC-012`다 (D-019 항목 3). 002의 manifest는
  `MGC-012-T006`부터 이어 쓴다. 그리고 spec.md의 "Package 3 gate 통과 후 같은 manifest에
  새 ID로 append한다"는 문장은 이 결정과 어긋나므로 002 디렉터리를 가리키도록 고친다.

- Source: **사용자 결정.** 2026-08-07 `/speckit-plan` 실행 중 `IMPL_PLAN`이 Package 3
  plan을 가리키는 것이 드러나 물었다. 선택지 둘을 제시했고 사용자가 002를 골랐다.
  D-019 항목 1이 이를 뒤집는다는 사실을 뒤늦게 발견해 다시 확인했고 같은 답을 받았다.

## D-028 — The E2E Target Enters Through The Same Door As The Secrets

- Status: accepted
- Decision: T009가 두 가지를 함께 한다. 둘 다 원래 manifest의 `allowed_paths` 밖이라
  scope 확장이고, 사용자가 명시 승인했다.

  **1. channel ID와 app ID를 환경변수로 확정한다.** 이름은 `AMPLAI_SLACK_APP_ID`와
  `AMPLAI_SLACK_CHANNEL_ID`다. contracts H-4.1 표에 넣는다.

  T010의 선행 조건이었다 — `reconcile`이 `app_id`로 남의 message를 배제하므로
  (`slack_projection.py:495`) 값 없이는 실제 workspace를 칠 수 없고, `auth.test`가 주는
  `bot_id`는 **다른 값이라 대체할 수 없다**. T009가 여는 이유는 E2E fixture가 그 값을
  조립하는 자리이기 때문이다. T010에 미루면 harness가 credential 둘만 보고 "구성됨"으로
  판정해, 대상 없이 skip이 아니라 통과처럼 보이는 상태가 생긴다.

  **네 변수를 한 tri-state로 묶는다.** 넷 다 없으면 미구성(skip), 넷 다 있으면 구성,
  하나라도 빠지면 `ValueError`에 빠진 이름 전부. credential만 두 변수로 묶고 대상을
  따로 두면 token만 넣은 사람이 "skip"만 보고 뭘 빠뜨렸는지 모른다 — H-4.1이 이미
  금지한 상태다.

  **형식은 검사하지 않는다.** `A`·`C` 접두를 강제하지 않는다. Slack ID 형식을 공식
  문서로 확인하지 않았고, 확인 안 된 가정을 오류로 굳히면 private channel처럼 다른
  접두를 쓰는 경우를 막는다. 값이 틀린 `app_id`는 H-3의 기동 자가검사가 잡는다.

  **`load_slack_settings`는 `environ`을 요구한다.** 기본값을 주면 `os.environ`을 만지는
  지점이 둘이 되고, T007 AC-05가 AST로 지키는 "읽는 지점 하나"가 깨진다. 부르는
  쪽(composition root, E2E fixture)이 넘긴다. R-014의 "entrypoint 한 곳에서만 읽는다"와
  같은 말이다.

  **2. `W2-pytest-import-mode`를 같이 닫는다.** `pyproject.toml`에 `pythonpath = ["tests"]`
  를 넣는다. `test_slack_projection.py`가 `test_governance_events`를 top-level로 import
  하는데, 그 의존이 pytest의 prepend import mode 부수효과에 기대고 있었다. 같은 파일을
  여는 김에 명시로 바꾼다. `index.yaml` deferred에서 뺀다.

- Source: **사용자 결정.** 2026-08-08 T009 착수 전 `/speckit-implement` 직전에 선택지
  둘씩 제시했고, "같이 닫는다"와 "T009에서 더한다"를 골랐다. 후자는
  `loop.stop_conditions`의 `scope_boundary_must_expand`를 여는 승인이다.

## D-029 — The Readback Probe Gets An Address No Destination Owns

- Status: accepted
- Decision: `verify_marker_readback`이 보내는 probe의 `destination_ref`를
  `PROBE_DESTINATION_REF`(`provider:slack:readback-probe`)로 **강제한다.** 진짜 destination의
  값을 넘기면 보내기 전에 거부한다. 계약은 H-3.1이다.

  **왜 필요한가.** T008은 기동 시점에 probe를 실제 채널로 보내고 되읽어 marker 왕복을
  확인한다. 그 probe는 지워지지 않고 채널에 남는다. round 1 구현은 docstring이 호출자에게
  `build_slack_marker`로 만들라고 **적극적으로 지시**했고, 그러면 probe가 진짜 Card와
  구분되지 않는 marker를 달게 된다. wave 6 failure-recovery lens가 결과 둘을 실측했다.

  1. probe를 event N의 marker로 만들면 N의 재시도에서 `reconcile`이 probe를 N의 Card로
     읽는다. Card는 한 장도 안 나갔는데 outbox는 DELIVERED다. 사람이 보는 것은 자가검사
     문구뿐이다.
  2. probe의 `destination_sequence`가 더 낮으면 `_page_verdict`의 두 번째 loop이 "하위
     sequence를 먼저 만났다 → 미전송"으로 판정한다. probe는 재시작마다 새로 올라가므로
     **오래된 sequence를 가진 가장 새로운 message**이고, 이것이 `reconcile`이 기대는 순서
     불변을 정확히 깬다. 결과는 중복 Card다.

  **자가검사가 막으려던 실패를 자가검사가 만드는 상태였다.** production caller가 아직
  없어 latent였고, T010이 배선을 붙이는 순간 실현된다.

  **왜 이 방법인가.** `_our_marker`가 `destination_ref` 불일치 marker를 두 loop 모두에서
  배제한다(`slack_projection.py:708`). 그러니 이 한 값으로 probe가 `reconcile`의 시야에서
  빠지고, 나머지(`event_type`, 네 필드 모양)는 진짜와 같게 유지되므로 T008의 근거인
  "실제로 나가는 것과 같은 것을 검사한다"가 산다.

  **기각한 둘.** probe 전용 `event_type`은 확실히 격리되지만 H-3이 잡기로 한 결함 넷 중
  "`event_type` 개명"을 못 잡게 된다 — 자가검사 전용 모양을 만들면 그 모양만 검증된다.
  별도 probe 채널은 정작 쓰는 채널이 아닌 다른 채널의 왕복을 검사하게 되고 설정 변수가
  다섯으로 는다.

  **충돌할 수 없다.** 진짜 값은 `provider:{provider}:{sha256 hexdigest}`이고
  (`events.py:1442`) hexdigest는 소문자 hex 64자다. sentinel은 hex가 아닌 문자를 갖고
  길이도 다르다. 추정이 아니라 생성 코드에서 읽은 사실이다.

  **함께 미룬 것.** probe가 기동 실패마다 한 장씩 쌓이는 문제는 격리 뒤에도 남는다. 다만
  남는 피해가 채널 소음과 rate limit이고 정합성은 안 깨지므로 owner 있는 deferred로 둔다
  (`index.yaml` `probe-accumulation`). `chat.delete`로 지우는 안은 그 method의 OAuth scope를
  공식 문서로 확인하지 않아 지금 넣으면 추정으로 계약을 쓰게 된다.

- Source: **사용자 결정.** 2026-08-08 wave 6 review round 1의 P0를 놓고 선택지 셋을
  제시했고 "쓰지 않는 destination 번호"를 골랐다. probe 누적은 같은 자리에서 "P0 격리로
  충분, Advisory로 내림"을 골랐다.

## D-030 — The Self-Check Removes Its Own Probe

- Status: superseded
- superseded_by: D-031 (authoritative revision의 실질 변경은 readback 성공 뒤 delete-only
  실패의 startup 거부 조항뿐이다. 성공·실패 양쪽의 삭제 시도, readback 실패 시 거부,
  원래 원인 우선과 probe 격리 의무는 D-031이 다시 수용한다)
- supersedes: D-029의 "함께 미룬 것" 문단 (나머지 D-029는 유효하다)
- Decision: `verify_marker_readback`이 검사를 마치면 `chat.delete`로 probe를 지운다.
  **성공과 실패 양쪽에서 지운다.** ~~Readback이 성공했어도 지우지 못하면 기동을
  거부한다.~~ 이 delete-only 거부만 D-031이 뒤집었다. 계약은 H-3.2다.

  **D-029에서 내가 쓴 근거가 틀렸다.** 그 문서는 "격리로 정합성 위험은 없어졌고 남는 것은
  채널 소음과 rate limit"이라고 적었고, 사용자가 그 근거로 deferral을 골랐다. 사실이
  아니다. wave 6 review round 2에서 **두 lens가 독립으로** 같은 결함을 잡고 재현했다.

  `_our_marker`의 배제는 **판정에서만** 빼는 것이고 조회 예산에서는 못 뺀다. probe도
  `conversations.history` page를 그대로 차지한다. `reconcile`은
  `SLACK_MAX_HISTORY_PAGES(5) × SLACK_HISTORY_PAGE_LIMIT(999) = 4995`건까지만 훑고, 넘으면
  `SlackProjectionSearchCapError`로 판정 불가가 된다. 그것은 dead letter + **되돌릴 수 없는
  operator hold**다.

  실패 경로가 구체적이다. Card를 보낸 뒤 `mark_delivered` 전에 죽는다 → 자가검사가 실패하는
  상태라 supervisor가 재시작 loop을 돈다 → 재시작마다 probe가 한 장씩 쌓인다 → 원인을 고치고
  기동에 성공했을 때 진짜 Card가 probe 아래 묻혀 있다 → page cap → 그 destination 전체가
  hold다. `chat.postMessage`가 channel당 초당 1건이므로 4995장은 시간 단위다 (이 시간
  계산만 추정이고, 4995 상한과 그때의 판정은 reviewer가 실측했다).

  **`chat.delete`는 새 scope를 요구하지 않는다.** Slack 공식 문서가 bot token scope를
  `chat:write`로 적고, 같은 문서가 "this method may delete only messages posted by that
  bot"으로 대상을 우리 message로 한정한다. D-029가 이 안을 미룬 이유가 "scope를 확인하지
  않아 추정으로 계약을 쓰게 된다"였는데, 그 확인이 끝났다. 설치 절차는 안 바뀐다.

  **지우기 실패를 조용히 넘기지 않는다.** ~~검사가 통과했는데 삭제만 실패하면
  `SlackReadbackError`로 기동을 거부한다.~~ 이 판정은 D-031에서
  `DEGRADED_CLEANUP` + activation 허용으로 바뀌었다. 검사도 실패하고 삭제도 실패하면
  **원래 원인이 이긴다.** 삭제 실패는 안전한 보조 진단으로 보존한다. 삭제 실패가 원인을
  가리면 operator가 엉뚱한 곳을 고친다는 근거는 유효하다.

  **`build_probe_marker`를 함께 둔다.** D-029는 호출자가 `build_slack_marker`의 결과에서
  `destination_ref`만 바꾸도록 안내문으로 요구했다. round 1의 P0가 정확히 그런 안내문에서
  나왔으므로 안내가 아니라 함수로 준다. 보내기 전 거부 guard는 그대로 둔다 — 두 겹이다.

  **sentinel의 길이를 진짜와 맞춘다.** `provider:slack:` + `z` 64자다. 짧게 두면 probe의
  metadata가 진짜보다 작아지고, metadata 크기 상한이 두 값 사이에 있으면 자가검사는
  통과하는데 첫 진짜 Card가 `metadata_too_large`로 terminal이 된다. 그 상한은 아직 모른다
  (OQ-003) — 모르는 값을 사이에 두지 않는다. hex가 아니므로 충돌 불가 근거는 그대로다.

- Source: **사용자 결정.** 2026-08-08 round 2 결과를 보고 D-029의 근거가 틀렸다는 것을
  알린 뒤 다시 물었고, "chat.delete로 지우되 scope를 먼저 공식 문서로 확인"을 골랐다.
  확인 결과 새 scope가 필요 없었다.

## D-031 — Cleanup-Only Failure Degrades Without Heuristic Recovery

- Status: APPROVED
- supersedes: D-030 decision record. 실질 변경은 "readback 성공 뒤 probe 삭제만 실패해도
  startup 거부" 조항과 그에 해당하는 T008 acceptance뿐이며, 아래 Decision이 나머지
  D-030 의무를 명시적으로 다시 수용한다.
- Scope: MGC-012 Package 4 startup readback outcome, bounded probe cleanup lifecycle과
  `MGC-012-T008`·`MGC-012-T013`의 activation/no-post 경계.
- Decision: Readback이 완전하게 성공하고 현재 probe의 exact delete만 실패하면
  `DEGRADED_CLEANUP`을 반환하고 Slack worker activation을 허용한다. 정상으로 숨기지 않으며,
  production composition root 한 곳이 각 startup 평가에서
  `SLACK_PROBE_CLEANUP_DEGRADED` 진단을 정확히 한 번 출력한다. Readback이 실패하면 기존처럼
  activation을 거부한다. Readback과 cleanup이 함께 실패하면 원래 readback cause가
  primary이고 cleanup 실패는 safe secondary data로만 보존한다.

  **Exact durable lifecycle.** Probe lifecycle은 process memory, Markdown, 기존
  Outbox/ingress/operator-hold row가 아니라 `GovernanceStore`의 전용 operational state로
  추적한다. 허용 transition은 아래뿐이다.

  ```text
  none
    -> POST_INTENT_RECORDED
        -> POST_CONFIRMED
            -> RESOLVED
            -> CLEANUP_PENDING
        -> AMBIGUOUS_POST
  CLEANUP_PENDING -> RESOLVED
  POST_INTENT_RECORDED | AMBIGUOUS_POST -> HARD_BLOCKED_NO_POST
  ```

  Network post 전 `POST_INTENT_RECORDED` commit이 필수다. Channel/app별 non-resolved row는
  최대 하나이고 atomic claim 승자 하나만 post한다. `message_ts`는 Slack 성공 응답 뒤에만
  기록하며 cleanup은 stored exact `channel + ts`로만 한다. `RESOLVED`는 confirmed cleanup과
  durable commit이 모두 성공한 뒤에만 된다. Slack delete 성공 뒤 `RESOLVED` commit이
  실패하면 마지막 committed state를 유지하고 current startup을 `HARD_BLOCKED_NO_POST`로
  판정해 worker activation을 거부한다.

  **Hard-block conditions.** 이전 unresolved lifecycle, `POST_INTENT_RECORDED` 뒤 응답 유실로
  provider identity가 없는 `AMBIGUOUS_POST`, 잔여 여부 불확정, state read/write 실패,
  atomic claim 실패 또는 commit ambiguity가 있으면 새 post는 0회이고
  `HARD_BLOCKED_NO_POST`다. 같은 channel/app의 concurrent startup에서 claim 패자도 같다.
  Unresolved state가 해소될 때까지 몇 번 재시작해도 추가 probe는 0개다.

  **Heuristic recovery 금지.** Stored exact `channel + ts`가 없으면 automatic lookup이나
  delete를 하지 않는다. Message text·prefix·metadata sentinel·app ID 조합·위치·최근 N건·
  시간 window·undocumented `client_msg_id`로 이전 probe를 추정하지 않는다. Operator 입력으로
  unresolved lifecycle을 강제 해제하지 않는다.

  **남은 approval blocker.** 이 결정 기록은 `MGC-012-T008`의 ledger dependency만 닫는다.
  `MGC-012-T013`, FR-018 production completion, `readback-selfcheck-wiring`과 Package 4 gate는
  production Slack worker entrypoint와 owner, provider-supported ambiguous-post recovery
  계약, 별도 governed probe recovery work item, narrow additive lifecycle schema와 repository
  scope가 각각 승인·확정될 때까지 BLOCKED다. 특히 schema/API·production activation은 이
  결정으로 구현 또는 배포 승인된 것이 아니다.

- Reason: Cleanup-only 실패를 startup 거부로 만들면 supervisor restart마다 probe가 늘어
  D-030이 막으려던 history page-cap 위험을 키운다. Readback 성공을 activation 기준으로
  유지하고 durable exact lifecycle로 추가 post를 막아 가용성과 누적 상한을 함께 지킨다.
- Evidence: 2026-08-10~11 사용자 `$grilling` 승인과 2026-08-11 clarification,
  `specs/002-mgc-012-package-4-slack-reference-e2e/spec.md` FR-023~FR-032,
  `data-model.md` `ProbeCleanupLifecycle`, `research.md` R-017~R-020.
- Risks: Durable lifecycle과 production composition root가 구현·검증되기 전에는 degraded
  activation의 restart safety를 production에서 주장할 수 없다. Ambiguous post를 heuristic으로
  회수하면 다른 message 삭제 또는 중복 probe 위험이 생긴다.
- Owner: 사용자 승인 경계는 workstream governor. T008은 MGC-012 Package 4,
  T013 production entrypoint와 governed recovery owner는 미정.
- Date: 2026-08-11
- Affected item: `MGC-012-T008`, `MGC-012-T013`, H-3.2, FR-023~FR-032.
- Reversal condition: 새 사용자 승인 Decision이 readback 가용성, exact provider identity,
  durable restart safety와 operator recovery evidence를 함께 제시하고 D-031을 supersede할 때만.
- Source: **사용자 결정.** 2026-08-10~11 `$grilling`에서 cleanup-only degraded activation과
  durable exact lifecycle을 승인했고, 2026-08-11 `/speckit-clarify`에서 concurrent claim,
  `RESOLVED` commit 실패, governed recovery 분리와 diagnostic 경계를 확정했다.

## D-032 — Package 4 Wave 6R Passes Without Closing Package 4

- Status: APPROVED
- Decision: MGC-012 Package 4 Wave 6R의 `MGC-012-T008`, `MGC-012-T009`,
  `MGC-012-T011`을 **PASS**한다. 이 판정은 typed startup readback outcome,
  `RESPONSE_CHANNEL_MISMATCH`, complete local marker validation, heuristic recovery 제거,
  network-free E2E harness와 provider isolation에만 적용한다.

  사용자 승인 뒤 speckit chain으로 확정한 `RESPONSE_CHANNEL_MISMATCH`는 configured channel의
  history를 거짓으로 조회하지 않고 provider가 확인한 exact `channel + ts`만 cleanup한다.
  이것은 승인 없는 backend behavior 변경이 아니므로 automatic block 조건에 해당하지 않는다.

  이 PASS는 **Package 4 또는 MGC-012 완료가 아니다.** 실제 Slack workspace E2E
  (`MGC-012-T010`), Python 3.12 clean-clone closure (`MGC-012-T012`), production lifecycle와
  startup wiring (`MGC-012-T013`)은 각각의 외부 dependency와 사용자 승인 blocker가 남아
  `blocked`를 유지한다. Schema, production entrypoint, release/deployment는 이번 wave에서
  승인하거나 구현한 범위가 아니다.
- Reason: T008/T009/T011의 acceptance와 evidence가 모두 완료됐고 repository verification 및
  독립 세 관점 review가 blocker 없이 닫혔다. 이번 wave는 network-free 범위라 live Slack
  manual check 부재가 승인 범위의 결손은 아니다.
- Evidence: Targeted pytest 189 passed/1 deselected; full pytest 1087 passed/1 deselected;
  Ruff check/format, mypy, Vault knowledge lint exit 0; `amplai-foundry verify` 7/7 PASS;
  task manifest validator 8/8 PASS; `git diff --check` PASS. Final contract와
  failure/recovery review는 P0/P1/Blocking-P2/Advisory 0이고, isolated regression review는
  mutation 10종 killed/0 survived이며 review 전후 combined source/test SHA-256은
  `0009be7bc702705f776ee057bb717d607db0168e11c8d86feb3623e428c1f20f`로 같다.
- Scope audit: Application 변경은 T008/T009/T011의 허용 경로인
  `src/amplai_foundry/governance/slack_http.py`, `tests/test_slack_http.py`에 한정된다.
  spec·plan·contract·manifest 변경은 사용자 승인 계약을 speckit chain으로 반영한 것이다.
  T008/T009/T011의 forbidden application paths는 건드리지 않았다.
- Remaining risks: Slack 실물 왕복과 실제 channel/app identity는 T010 전까지 검증되지 않는다.
  Python 3.12 clean clone은 T012 전까지 주장할 수 없다. Durable probe lifecycle persistence,
  ambiguous-post recovery와 production activation safety는 T013 전까지 production claim으로
  올릴 수 없다.
- Owner: Workstream governor. 잔여 blocker 해소는 각 T010/T012/T013 external dependency owner.
- Date: 2026-08-12
- Affected item: `MGC-012-T008`, `MGC-012-T009`, `MGC-012-T011` only.
- Source: `CHECKPOINTS/MGC-012-package-4-wave-6r-gate-2026-08-12.md`, `APR-017`.

## D-033 — Remove The Human Approval Before `/speckit-implement`

- Status: APPROVED
- Superseded (부분): Pre-Implement Procedure 절 은 `D-046`(2026-08-19)이 대체한다. 실질(그 단계를 거친다)은 유지되고 **호출 주체가 `/work` controller 로 바뀐다.**
- Decision: `/speckit-implement` 앞의 사람 승인을 없앤다. AI가 승인 없이 스스로 호출해도 된다.
  세 곳을 고쳤다.

  1. `AGENTS.md` Scope 절의 "사용자가 직접 호출할 때만 실행한다"를 지웠다
     (`CLAUDE.md`는 `AGENTS.md`로의 symlink라 함께 반영된다)
  2. `.specify/workflows/speckit/workflow.yml`의 `tasks` gate step을 삭제했다
  3. 그 gate가 담고 있던 필수 3단계를 `AGENTS.md`의 새 **Pre-Implement Procedure** 절로
     옮겼다. `/taskify` → `validate_task_manifest.py` → `taskify_to_tasks_md.py --out`

  **구현 뒤 three-lens subagent review gate(`review-implementation`)는 유지한다.** 이것은
  승인 절차가 아니라 검증이다. P0/P1/Blocking-P2가 하나라도 있으면 gate를 열지 않는다는
  Review Before Gate 규칙도 그대로다.
- Reason: 사용자가 자리를 비운 상태에서 매 구현 전환마다 승인을 기다리면 진행이 멈춘다.
  승인은 통제 수단 중 약한 쪽이다. 실효 통제는 구현 전 manifest 계약(acceptance,
  invariants, forbidden_paths)과 구현 후 독립 review다. 둘 다 유지했으므로 승인 제거가
  통제 총량을 줄이지 않는다.
- Evidence: 변경 후 `python3 -c "yaml.safe_load(...)"`로 workflow.yml parse 확인.
  step 목록은 `specify → review-spec → plan → review-plan → implement →
  review-implementation`이다. `amplai-foundry verify --root .` 7/7 PASS.
- Scope audit: source code와 test는 건드리지 않았다. Package 5 round 10 review target
  aggregate `89c44faaf9c637227d834c6d09a39389fb5dbb76205bfdf338655416cf9994e0`는 불변이다.
  `AGENTS.md`와 `workflow.yml`은 그 target 파일 목록 밖이다.
- Remaining risks: **workflow engine이 shell을 돌리는 step type을 지원하는지 확인하지
  못했다.** 확인 안 된 type을 지어내는 대신 gate를 지우고 절차를 규칙으로 옮겼다. 결과로
  Pre-Implement Procedure 3단계를 engine이 더는 강제하지 않는다. `AGENTS.md` 규칙과,
  `tasks.md` 부재 시 `check-prerequisites.sh --require-tasks`가 멈추는 것
  (`.claude/skills/speckit-implement/SKILL.md:60`)이 남은 안전망이다. 2026-08-03에 이
  단계가 끊겨 `tasks.md`가 영영 안 생긴 사고가 있었으므로 회귀 여부를 관찰한다.
- Owner: Workstream governor.
- Date: 2026-08-13
- Affected item: repository-wide spec-kit 절차. 특정 MGC item에 한정되지 않는다.
- Source: 사용자 요청과 선택. `AGENTS.md` Scope / Pre-Implement Procedure,
  `.specify/workflows/speckit/workflow.yml`.

## D-034 — Align The Safe Outcome Contract To The Implemented Behavior

- Status: APPROVED
- Decision: `contracts/interaction-feedback.md`의 safe outcome mapping table과 `spec.md`
  FR-024를 **구현 쪽에 맞춘다.** 코드 동작은 바꾸지 않는다.

  1. mapping table에서 `completed ← first successful decision` 행을 뺀다
  2. mapping table에서 `unavailable ← retry/hold without a more specific public result`의
     retry 부분을 뺀다. 코드는 `RETRY`에 `None`을 돌려준다
  3. `spec.md` FR-024의 대상 목록에서 "accepted"를 뺀다
  4. `SafeInteractionOutcome.COMPLETED` enum 값과 `_SAFE_INTERACTION_MESSAGES`의 도달 불가
     message를 지운다
  5. 성공 통지는 Result Card가 담당한다는 것을 계약 본문에 남긴다. 이미
     `interaction-feedback.md`의 `Delivery` 절이 그렇게 적고 있으므로 그 문장을 mapping
     table의 근거로 승격한다

- Reason: 세 artifact가 어긋나 있었다. 계약과 spec은 `completed`를 worker의 출력으로
  선언하는데 `ingress_worker.py:373-374`는 그 경우 `None`을 돌려주고, repo 전체에
  `SafeInteractionOutcome.COMPLETED`를 만드는 곳이 없다. 도달 불가 문자열이 API에 남아 있다.

  코드에 producer를 붙이는 반대 방향은 택하지 않았다. 같은 계약의 `Delivery` 절이 성공
  경로를 Result Card로 재배치했고 그것이 실제로 동작한다
  (`tests/test_review_cards.py:1351-1424`가 decision당 result outbox event 정확히 1건을
  확인한다). producer를 붙이면 같은 사건을 두 번 알리는 이중 통지가 된다.

  `unavailable`의 retry 조항을 같이 닫는 이유는 같은 table의 같은 성격 불일치이기
  때문이다. 하나만 고치면 table이 여전히 코드와 어긋난다.

- Evidence: 3lens-review round 10의 `C-7` (contract lens Blocking-P2)과 `A-8`
  (failure/recovery Advisory). `specs/003-slack-proposal-card/evidence/3lens-review.md`.
- Scope audit: 이 Decision은 판단만 기록한다. 문서 변경 자체는 `/speckit-converge` →
  `/taskify` → `/speckit-implement` 경로로 만든다. spec·plan 성격 문서를 손으로 쓰지
  않는다는 AGENTS.md 규칙을 지킨다.
- Remaining risks: `contracts/interaction-feedback.md`와 `spec.md`는 round 10 review
  target aggregate `89c44faaf9c637227d834c6d09a39389fb5dbb76205bfdf338655416cf9994e0`
  안에 있다. 이 변경은 그 freeze를 깬다. round 10은 이미 FAIL이므로 실질 장애는 아니지만,
  **수정 → 재freeze → 재review 순서를 지킨다.** round 9가 무효가 된 원인이 바로 freeze 뒤
  변경이었다.
- Owner: Workstream governor.
- Date: 2026-08-13
- Affected item: `MGC-012-P5`. 후속 task는 converge 산출물에서 확정한다.
- Source: 사용자 선택. 3lens-review round 10 `C-7`, `A-8`.

## D-035 — Define The Reviewed Set So Bookkeeping Cannot Drift It

- Status: APPROVED
- Decision: three-lens review 의 frozen target 구성을 다시 정한다. round 12부터 적용한다.

  **포함한다.**

  - `spec.md`, `plan.md`, `research.md`, `data-model.md`, `quickstart.md`
  - `contracts/*.md`
  - `checklists/*.md`
  - `task-manifests/<FEATURE>-T*.yaml` — **모든** per-task manifest. 계약(acceptance,
    invariants, forbidden_paths)이 여기 있다
  - review 대상 `src/` 와 `tests/` 파일
  - `.specify/feature.json`

  **제외한다.**

  - `task-manifests/index.yaml` — 작업 상태·순서·coverage 만 담는다. 계약 내용은 0건이다
  - `tasks.md` — `index.yaml`에서 자동 생성되는 목차다
  - `evidence/**` 전부 — per-task evidence, `3lens-review*.md`, `mutation-*.md`,
    `review-target.txt` 자신

  manifest header의 제외 문구와 실제 line 목록이 반드시 일치해야 한다. 얼리기 전에 둘을
  대조한다.

- Reason: 세 가지 결함이 같은 뿌리에서 나왔다.

  1. **bookkeeping이 frozen set 안에 쓴다.** review가 끝난 뒤 task status를 `done`으로 바꾸면
     `index.yaml`과 `tasks.md`가 재생성되어 aggregate가 어긋난다. round 10과 round 11이 같은
     방식으로 drift했다. 둘 다 무해했고 둘 다 기록했지만, 같은 실수가 두 번 났으면 절차가
     잘못된 것이다.
  2. **정작 계약서가 안 얼려 있었다.** `MGC-012-P5-T004`~`T008.yaml`은 wave 4가 한 일의 계약
     전부인데 target 목록에 없다. contract lens가 `scope.allowed_paths` /
     `forbidden_paths` 판정에 쓰는 근거가 얼린 범위 밖이었다. round 11 Advisory `A-6`이 이를
     지적했다.
  3. **header가 내용과 다르다.** round 11 header는 per-task `MGC-012-P5-T00*.md` 증거를
     제외한다고 적었으나 목록에는 T001~T003 증거가 들어 있다.

  즉 자주 바뀌는 사무 기록은 얼려두고 검토 기준인 계약서는 안 얼린, 정확히 거꾸로 된 구성이다.
  잃는 것은 없다. 제외하는 둘은 계약 내용이 0건이고 포함하는 manifest에서 파생된다. 원본을
  얼리면 파생물은 따라온다.

- Evidence: `grep -c "acceptance\|forbidden_paths\|invariants"` — `index.yaml` 0건,
  `MGC-012-P5-T004.yaml` 5건. target 목록에 `T004`~`T008.yaml` 부재 확인. round 10과 round 11의
  drift는 각각 `review-target.txt`의 Round 10 / Round 11 closure 절에 기록돼 있고 두 경우 모두
  `index.yaml`과 `tasks.md`만 바뀌었다.
- Scope audit: 이 Decision은 review 절차만 바꾼다. source, test, spec, contract, data-model을
  수정하지 않는다. round 11 판정(FAIL)은 그대로다 — 세 lens 모두 freeze 시점 aggregate
  `36e3923f…`를 확인하고 시작했으므로 그 review는 유효하다.
- Remaining risks: 제외한 `index.yaml`이 task 의존·순서·coverage를 담는다. 그것이 잘못돼도
  review가 못 잡는다. per-task manifest의 `depends_on`이 같은 정보를 중복으로 갖고 있으므로
  manifest validator가 대신 잡는다 — 다만 validator는 lens가 아니다. round 12 이후 이 공백이
  실제로 문제를 만드는지 관찰한다.
- Owner: Workstream governor.
- Date: 2026-08-13
- Affected item: `MGC-012-P5` round 12 이후의 모든 three-lens review.
- Source: 사용자 승인. round 11 Advisory `A-6`,
  `evidence/review-target.txt`의 Round 10 / Round 11 closure.

## D-036 — Make `/speckit-analyze` A Required Pre-Implement Step

- Status: APPROVED
- Superseded (부분): analyze 를 고정 순서에 못박은 부분 은 `D-046`(2026-08-19)이 대체한다. 실질(그 단계를 거친다)은 유지되고 **호출 주체가 `/work` controller 로 바뀐다.**
- Decision: `/speckit-analyze`를 선택에서 **필수**로 올린다. `/taskify` 뒤,
  `/speckit-implement` 앞이다. **CRITICAL 또는 HIGH가 하나라도 있으면 구현을 시작하지
  않는다.** MEDIUM과 LOW는 기록하고 item별로 판단한다.

  네 곳을 고쳤다.

  1. `AGENTS.md` Pipeline 표기에 `/speckit-analyze` 줄 추가
  2. `AGENTS.md`의 "선택적으로 쓴다" 문장 분리 — clarify는 선택 유지, analyze는 필수
  3. `AGENTS.md` **Pre-Implement Procedure**를 3단계에서 4단계로. analyze가 4단계다
  4. `.specify/workflows/speckit/workflow.yml`에 `analyze` step 추가,
     `.specify/memory/constitution.md`의 Spec-Driven Workflow 표와 규칙에 반영

- Reason: wave 4에서 `MGC-012-P5-T006`의 AC-06·AC-07이 일부 미충족인 채 구현이 끝났고,
  그것을 구현 뒤에야 손으로 발견해 기록했다. task와 요구사항의 커버리지 구멍은 구현 전에
  잡는 것이 맞고 analyze가 그 도구다.

  범위를 정확히 적는다. analyze는 `spec.md`·`plan.md`·`tasks.md` **세 artifact를 서로**
  대조하고 **소스 코드는 읽지 않는다** (`.claude/skills/speckit-analyze/SKILL.md:60`).
  잡는 것은 task 없는 요구사항, 요구사항 없는 task, 중복·모호한 요구사항, 미명세 항목이다.
  round 11의 `R-5`(FR-024가 코드보다 하나 적게 셈) 같은 **spec 대 코드** 불일치는 못 잡는다.
  그것은 contract reviewer가 잡았다. **analyze는 subagent review를 대체하지 않는다.**
  앞단에서 다른 종류의 결함을 거를 뿐이다.

- Evidence: `SKILL.md:60`이 대상 artifact 셋을 명시한다. Severity 기준은 같은 파일
  Severity Assignment 절이다. workflow.yml은 변경 후 `yaml.safe_load`로 parse 확인했고
  step 순서는 `specify → review-spec → plan → review-plan → analyze → implement →
  review-implementation`이다.
- Scope audit: 절차 문서와 workflow 정의만 바꿨다. source, test, spec, contract는 수정하지
  않았다. `CLAUDE.md`는 `AGENTS.md`로의 symlink라 함께 반영된다.
- Remaining risks: **이 workflow engine이 `speckit.analyze`를 실제로 dispatch하는지 확인하지
  못했다.** 위 세 command step과 같은 이름 규칙을 따르고 `.claude/skills/speckit-analyze/`가
  존재하지만 command registry를 직접 읽지는 않았다. dispatch가 실패하면 step을 지우지 말고
  `AGENTS.md`의 Pre-Implement Procedure 4단계를 손으로 돌린다. 그 절이 유일한 출처다.
  D-033에서 shell step type을 확인하지 못해 gate를 규칙으로 옮긴 것과 같은 처리다.
- Owner: Workstream governor.
- Date: 2026-08-13
- Affected item: repository-wide spec-kit 절차. 특정 MGC item에 한정되지 않는다.
- Source: 사용자 요청. round 11 wave 4 회고.

## D-037 — Make `/speckit-clarify` A Required Step Before `/speckit-plan`

- Status: APPROVED
- Superseded (부분): clarify 를 고정 순서에 못박은 부분 은 `D-046`(2026-08-19)이 대체한다. 실질(그 단계를 거친다)은 유지되고 **호출 주체가 `/work` controller 로 바뀐다.**
- Decision: `/speckit-clarify`를 선택에서 **필수**로 올린다. `/speckit-specify` 뒤,
  `/speckit-plan` 앞이다.

  네 곳을 고쳤다.

  1. `AGENTS.md` Pipeline 표기에서 괄호(선택) 제거, `/speckit-specify` 뒤로 이동
  2. `AGENTS.md` 규칙을 선택에서 필수로. 성격과 예외를 함께 기록
  3. `.specify/workflows/speckit/workflow.yml`에 `clarify` step 추가. `specify` 바로 뒤,
     `review-spec` gate 앞이다 — gate가 **정리된** spec을 보게 한다. gate message도
     "Review the clarified spec" 으로 고쳤다
  4. `.specify/memory/constitution.md`의 Spec-Driven Workflow 표와 규칙에 반영

- Reason: skill 자신이 그 순서를 요구한다. `SKILL.md:62` — "expected to run (and be
  completed) BEFORE invoking `/speckit-plan`", 건너뛰면 downstream rework 위험을 경고하라고
  적혀 있다. 선택으로 두면 그 경고가 발동할 자리가 없다.

  필수로 올려도 억지 질문이 생기지 않는다. `SKILL.md:75`와 `:127`이 항목별로
  Clear/Partial/Missing을 매기고 Partial·Missing인 것만 질문 후보로 올린다. 다 Clear면
  질문 없이 coverage map만 보고한다.

- 성격: **clarify는 사람에게 묻고 멈춘다.** 이것은 D-033이 없앤 승인 관문과 **다르다.**
  승인이 아니라 모델이 갖고 있지 않은 사실을 받는 단계이고, 답 없이 추정으로 채우지 않는다는
  No Speculation 원칙과 같은 방향이다. D-033은 "이미 정해진 일을 시작해도 되는지 묻는 것"을
  없앴지 "모르는 것을 묻는 것"을 없앤 것이 아니다.

  예외는 하나다. 사용자가 명시적으로 건너뛰라고 하면 진행하되 downstream rework 위험을
  경고한다 (`SKILL.md:62`).

- Evidence: 변경 후 `yaml.safe_load`로 workflow.yml parse 확인. step 순서는
  `specify → clarify → review-spec → plan → review-plan → analyze → implement →
  review-implementation`이다.
- Scope audit: 절차 문서와 workflow 정의만 바꿨다. source, test, spec, contract는 수정하지
  않았다. `CLAUDE.md`는 `AGENTS.md`로의 symlink라 함께 반영된다.
- Remaining risks: D-036과 같다. **engine이 `speckit.clarify`를 실제로 dispatch하는지 확인하지
  못했다.** 실패하면 step을 지우지 말고 손으로 돌린다. `AGENTS.md`가 유일한 출처다.
- Owner: Workstream governor.
- Date: 2026-08-13
- Affected item: repository-wide spec-kit 절차.
- Source: 사용자 요청. D-036과 짝을 이룬다.

## D-038 — Classify Transient Store Failure As Retryable And Give Exhausted Ingress A Terminal Outcome

- Status: APPROVED
- Decision: round 11 의 `R-1`, `R-2`, `R-3` 을 아래 여섯 판단으로 닫는다.

  1. **`R-2` 는 축 1 만 닫는다.** transient store 실패를 재시도 예산 소비 경로로
     재분류한다. operator hold 를 푸는 governed 경로는 만들지 않는다.
  2. **transient 경계는 `ingress_worker` 정책을 복제한다.** `_is_corruption` 을 공유
     helper 로 올리고 같은 순서를 쓴다 — `sqlite3.DatabaseError` 이고 손상이면 terminal,
     그 외 `(GovernanceStoreError, sqlite3.Error)` 는 재시도.
  3. **`OutboxRetryableError` 를 신설한다.** `GovernanceEventError` 형제로 두고
     `deliver_next` 에 분기를 더해 `unreconcilable=False` 로 `fail()` 을 부르되
     `error_code=error.code` 를 넘긴다. 재시도 중간과 소진 후 DLQ 가 진짜 원인을 가진다.
  4. **`R-1` 은 `interruption` 쌍만 합친다.** `_interruption_kind` 와
     `_raise_sanitized_interruption` 을 공유 helper 하나로 만들고 두 파일이 쓴다.
     `_clear_exception_frames` 4사본 통합은 backlog 로 넘긴다.
  5. **`R-3` 은 `_transition` 에서 마지막 attempt 를 승격한다.** `RETRY` 이고
     `claim.attempts >= max_attempts` 면 `RECOVERY_HOLD` 로 바꾼다. 기존
     `_with_feedback` 이 돌아 `unavailable` 이 나간다. migration 은 없다.
  6. **lease 만료 소진에는 자동 통지를 넣지 않는다.** 계약에 그 이유와
     `ingress.stranded()` 가 operator 입구라는 것을 쓴다.

- Reason: 판단마다 근거가 다르다.

  **(1)** 재시도 예산을 쓰게 해도 되돌릴 수 없는 hold 는 사라지지 않는다.
  `events.py:3148-3151` 의 `exhausted or unreconcilable` 이 같은 `_dead_letter` 로 가고
  `_dead_letter`(`:3313-3332`)는 분기 없이 hold 를 insert 한다. 예산은 기본 설정에서
  5+10+20+40 = 75초를 벌 뿐이다 (`OutboxConfig` `events.py:2922-2925`, backoff `:3355-3361`).
  hold 해제는 API 부재가 아니라 schema 금지다 — `migrations.py:631` 의
  `CHECK (resolved_at IS NULL)` 과 `:701-711` 의 append-only 트리거. 여는 것은 migration 을
  포함한 새 기능이고 wave 5 의 성격(기존 spec 안의 결함 수정)을 벗어난다.

  **(2)** review 의 지적이 정확히 "한 저장소에서 같은 예외가 두 정책을 받는다" 였다.
  `ingress_worker.py:228-259` 가 이미 정책을 갖고 있으므로 새로 만들지 않고 그것을 쓴다.
  helper 를 공유하면 앞으로 갈라질 수 없다.

  **(3)** `deliver_next` 의 `except Exception`(`events.py:3238-3245`)은 예외를 보지 않고
  `OUTBOX_DELIVERY_FAILED` 를 박는다. round 11 `R-1` 이 바로 이 형태를 결함으로 셌다 —
  "수렴하기는 한다 (…) 그 값은 원인이 아니고". 같은 항의를 다시 받을 코드를 쓰지 않는다.

  **(4)** `R-1` 의 원인은 복제된 함수 쌍이 한쪽만 고쳐진 것이다. `slack_projection.py:1003`
  은 `"exception"` 을 돌려주고 `:1018` 이 catchable 한 `RuntimeError` 를 올리는데,
  `slack_http.py:593` 은 `"base_exception"` 을, `:603` 은 `BaseException` 을 올려
  `deliver_next` 의 두 handler 를 모두 통과한다. `_clear_exception_frames` 는 4사본
  (`decisions.py:775`, `slack_projection.py:1085`, `slack_http.py:572`,
  `slack_cards.py:327`)이지만 아직 갈라지지 않았고 결함을 만든 적이 없다. 결함 없는 중복까지
  건드리면 round 12 frozen target 이 review 범위 밖 파일 둘로 넓어진다.

  **(5)** `claim_next` 가 claim 시 `attempts = attempts + 1` 하고 row 를 다시 읽으므로
  (`ingress.py:312`, `:331-334`) worker 가 든 `claim.attempts` 는 이번 시도를 포함한다.
  마지막 시도가 정확히 판별된다. `_transition`(`ingress_worker.py:391-417`)은 RETRY 6곳이
  모이는 단일 통로다. 승격하면 `_safe_outcome`(`:377-389`)이 `RECOVERY_HOLD` +
  비특정 code 를 `UNAVAILABLE` 로 옮긴다. **D-034 를 되돌리지 않는다** — 소진된 command 는
  진짜 recovery hold 가 되므로 "recovery hold without a more specific public result" 정의에
  그대로 맞는다. `stranded()` 는 `recovery_hold` 도 포함하므로(`ingress.py:397`) operator
  가시성이 유지된다.

  **(6)** lease 만료 소진은 결과를 모르는 종점이다. worker 가 decision commit 뒤
  `complete` 전에 죽었을 수 있고 그러면 Result Card 는 이미 나갔다. 평소에는 재시도가
  자가 치유한다 — replay 가 `already_completed` 를 만든다(`ingress_worker.py:371-375`).
  소진되면 그 치유가 끊긴다. 거기에 `unavailable` 을 자동 통지하면 배달된 Result Card 와
  모순되는 메시지를 보낼 수 있다. 틀린 통지보다 침묵이 낫고, 그 이유를 계약에 적는다.

- Evidence: `specs/003-slack-proposal-card/evidence/3lens-review-round-11.md` 의 `R-1`,
  `R-2`, `R-3`. 위 인용 line 은 모두 이번 세션에서 원본을 열어 확인했다.
- Scope audit: 이 Decision 은 판단만 기록한다. 코드·계약·spec 변경은 `/taskify` →
  `/speckit-analyze` → `/speckit-implement` 경로로 만든다.
- Remaining risks: 세 가지다.

  1. **되돌릴 수 없는 operator hold 는 남는다.** (1)이 그것을 닫지 않는다고 명시했다.
     transient 가 75초 안에 안 풀리면 destination 은 여전히 영구 정지한다. 별도 item 이다.
  2. **lease 만료 소진의 침묵도 남는다.** (6)이 의도한 선택이지만 결함이 아니게 되려면
     계약이 그것을 사실대로 적어야 한다. 적지 않으면 다음 round 가 다시 P1 로 센다.
  3. `_clear_exception_frames` 4사본은 그대로다. 갈라지면 `R-1` 과 같은 결함이 난다.

- Owner: Workstream governor.
- Date: 2026-08-14
- Affected item: `MGC-012-P5` wave 5. 후속 task 는 `/taskify` 산출물에서 확정한다.
- Source: 사용자 선택. `/grill-me` 세션 6문 6답. round 11 `R-1`, `R-2`, `R-3`.

## D-039 — Do Not Announce Failure For A Click Whose Decision Already Landed

- Status: APPROVED
- Decision: round 12 `F-1` 을 아래로 닫는다.

  소진된 ingress command 를 recovery hold 로 승격하기 **전에** 그 명령에 대한 결정이 이미
  기록됐는지 본다 (`committed_decision()`). 기록돼 있으면

  1. **승격은 그대로 한다.** 명령이 recovery hold 로 남아야 `stranded()` 에 보이고, 결정은
     됐는데 완료 도장이 없는 상태를 사람이 확인할 수 있다
  2. **error code 를 `INGRESS_DECISION_COMMITTED_UNRECONCILED` 로 바꾼다.** 그 code 가
     `_safe_outcome` 에서 침묵 집합에 들어가 사용자 통지를 만들지 않는다

  결정이 기록돼 있지 않은 소진은 그대로 `unavailable` 을 알린다. 두 종점을 code 로 가른다.

- Reason: `D-038` 항목 5 의 승격이 `D-038` 항목 6 이 거부한 모순을 한 분기 옆에서 만들었다.

  reviewer 가 승인을 누르고, 1차 시도가 결정을 **기록하는 데 성공**하지만 완료 도장을 찍는
  마지막 쓰기가 실패하고, lease 만료 뒤 재claim 된 마지막 시도가 실패하는 경우다. 결정은 살아
  있고 "승인됨" Result Card 는 이미 대기열에 있는데, 승격이 `unavailable` 을 보내 **서로
  모순되는 두 메시지**가 간다. round 12 failure lens 가 실측했다.

  항목 6 에서 lease 만료 종점에 통지를 넣지 않은 이유가 정확히 이것이었다 — 배달된 Card 와
  모순되는 통지를 보내지 않는다. 승격 분기가 그 원칙을 보지 않은 것이 결함이다.

  침묵을 고른 것은 취향이 아니라 `D-034` 가 정한 것이다. "첫 성공 결정은 safe outcome 을
  만들지 않는다. 그 통지는 Result Card 가 한다." `already_completed` 를 보내는 선택지는
  `D-034` 위반이라 없다.

  명령을 `completed` 로 끝내는 선택지도 있었으나 택하지 않았다. 상태는 깔끔해지지만 완료
  도장이 왜 실패했는지 아무도 모르게 되고 operator 목록에서도 사라진다. 결정은 됐는데 장부가
  안 맞는 상태는 사람이 한 번 봐야 한다.

- Evidence: `specs/003-slack-proposal-card/evidence/3lens-review-round-12.md` `F-1`.
  negative verification 둘 다 killed — 승격 전 `committed_decision()` 확인을 지우면
  `test_an_exhausted_command_whose_decision_committed_announces_nothing` 이, 침묵 분기를
  지우면 그 test 와 `test_the_silent_hold_code_does_not_swallow_other_outcomes` 가 실패한다.
- Scope audit: `ingress_worker.py` 와 `tests/test_slack_ack_boundary.py` 만 바꾼다. schema 는
  바뀌지 않는다.
- Remaining risks: 결정이 기록됐는지 보는 것은 store 읽기 하나를 더 하는 일이다. 그 읽기가
  실패하면 `committed_decision` 이 예외를 낼 수 있고 승격 자체가 실패한다. 현재는 그 경로가
  `_finalize` 의 기존 except 로 흘러 `FINALIZE_FAILED` 가 된다 — 통지 없이 lease 만료로
  수렴한다. 나쁘지 않지만 측정하지 않았다.
- Owner: Workstream governor.
- Date: 2026-08-16
- Affected item: `MGC-012-P5` wave 6.
- Source: 사용자 선택. round 12 `F-1`.

## D-040 — Let FR-027 Govern The Retry Classification Without Weakening FR-021

- Status: APPROVED (**안 B**)
- Decision: **FR-021 은 원문 그대로 둔다.** 대신 FR-027 에 우선 관계를 명시한다 — 어떤 실패를
  재시도로 분류하는지는 FR-027 이 정하고, 그 범위에서 FR-021 보다 우선한다. 메커니즘 자체
  (ordering, reconciliation, 재시도 예산, hold, provider isolation)를 우회하지 않는 것은
  FR-021 이 계속 요구한다.
- Reason: 이번 사고의 교훈이 "변경하는 쪽이 변경 대상 요구사항을 느슨하게 만들지 않는다" 이므로,
  기존 조항을 건드리지 않고 새 조항에 예외를 다는 쪽이 같은 실수를 반복하지 않는다. 두 조항을
  같이 읽어야 뜻이 통하는 비용은 받아들인다.
- Owner: Workstream governor.
- Date: 2026-08-16
- Affected item: `MGC-012-P5`. `spec.md` FR-027 에 반영했다. FR-021 은 원문이다.
- Source: 사용자 선택. round 12 `C-2`.

### 승인 전 기록 (참고)

- Status: **PROPOSED — 승인 전이다. 적용하지 않았다.**
- Context: 이 Decision 은 **이미 저지른 계약 변경을 되돌린 뒤** 정식으로 다시 묻는 것이다.

  wave 5 에서 `/speckit-analyze` 가 FR-021 과 FR-027 의 긴장을 MEDIUM 으로 잡았을 때, 구현하는
  쪽이 FR-021 문구를 조용히 완화해서 해소했다. Decision 기록이 없었다. round 12 `C-2` 가 그것을
  Blocking-P2 로 잡았고, 사용자 지시에 따라 **FR-021 을 원문으로 되돌렸다.**

  지금 저장소 상태는 원문이다. 그래서 아래 긴장이 **열린 채로 남아 있다.**

- Problem: FR-021 원문은 이렇다.

  > Result and review delivery MUST **preserve existing** destination ordering, reconciliation,
  > **retry, hold**, and provider-isolation **behavior**.

  `T010`(`D-038` 항목 1~3)이 한 일은 어떤 실패를 재시도로 볼 것인가를 **바꾼** 것이다. 즉 retry
  동작을 바꿨다. 원문 그대로 읽으면 위반이다.

- Options:

  **안 A — FR-021 문구를 좁힌다.** "메커니즘을 우회하지 않는다" 는 뜻으로 다시 쓰고, 어떤
  실패가 재시도인지는 FR-027 이 정한다고 명시한다. 요구사항 하나만 읽어도 뜻이 통한다. 다만
  기존 요구사항을 느슨하게 만드는 방향이다.

  **안 B — FR-021 은 그대로 두고 FR-027 에 예외를 적는다.** "재시도 분류에 대해서는 이 조항이
  FR-021 에 우선한다" 를 FR-027 에 넣는다. 기존 요구사항을 건드리지 않는 보수적인 방향이다.
  대신 두 조항을 같이 읽어야 뜻이 통한다.

  **안 C — 아무것도 안 바꾸고 긴장을 기록만 한다.** 다음 round 가 같은 것을 다시 잡는다.

- Recommendation: **안 B.** 이번 사고의 교훈이 "변경 대상이 되는 요구사항을 변경하는 쪽이
  느슨하게 만들지 않는다" 이므로, 기존 조항을 건드리지 않고 새 조항에 예외를 다는 쪽이 같은
  실수를 반복하지 않는다.
- Owner: Workstream governor.
- Date: 2026-08-16
- Affected item: `MGC-012-P5`. **승인 전까지 `spec.md` 는 원문을 유지한다.**
- Source: round 12 `C-2`.

## D-041 — Write The Third Ending Into The Contract Instead Of Deleting It From The Code

- Status: APPROVED (**안 A**)
- Decision: **spec 과 계약을 종점 셋으로 고친다.** `D-039` 의 코드는 그대로 둔다.
  - `spec.md` FR-026 에 세 번째 종점을 넣는다 — 마지막 attempt 가 관측됐고 실패했으나 **앞선
    attempt 가 결정을 이미 commit 한** 경우, 시스템은 아무 outcome 도 알리지 않고 command 를
    recovery hold 로 남겨 operator recovery 대상으로 유지한다.
  - `contracts/interaction-feedback.md` 의 `Two endings are possible` 을 종점 셋으로 고치고,
    `INGRESS_DECISION_COMMITTED_UNRECONCILED` 가 safe outcome 이 아니라 **error code** 임을
    명시한다. 그 code 는 `_safe_outcome` 에서 침묵으로 mapping 된다.
- Reason: 어느 쪽이 진실인지는 실측이 정했다. round 12 `F-1` 이 **배달된 Result Card 와
  `unavailable` 통지가 모순되는 것**을 실측했다. 코드를 FR-026 에 맞추면 그 모순이 되살아나고,
  모순된 통지는 되돌릴 수 없다. 그러므로 틀린 것은 코드가 아니라 계약 문서다.
- Consequence: `CT-1` 과 `CT-2` 가 한 Decision 으로 닫힌다. 코드 변경은 없다.
- Owner: Workstream governor.
- Date: 2026-08-17
- Affected item: `MGC-012-P5` wave 7. `spec.md` FR-026 과
  `contracts/interaction-feedback.md` 를 고친다. `ingress_worker.py` 는 안 고친다.
- Source: 사용자 선택. round 13 `CT-1`, `CT-2`. `D-039` 를 보완하며 supersede 하지 않는다.

## D-042 — Treat An Unreadable Decision Ledger As A Reason To Stay Silent

- Status: APPROVED (**안 A**)
- Decision: `_settle_exhausted_retry` 의 `committed_decision()` 호출을 좁은 `try/except
  (GovernanceStoreError, IngressError, sqlite3.Error)` 로 감싼다. 읽기가 실패하면 **결정이
  기록됐을 수 있다는 보수적 가정**으로 `RECOVERY_HOLD` + `INGRESS_DECISION_COMMITTED_UNRECONCILED`
  를 낸다. 즉 침묵한다.
- Reason: 이 실패는 **상관된 실패**다. 재시도를 소진시킨 그 조건(store busy)이 이 읽기도
  실패시킨다. 그래서 "드물다" 로 넘길 수 없다. 모르는 상태에서 `unavailable` 을 보내면 이미
  배달된 Result Card 와 모순될 수 있고 그 통지는 되돌릴 수 없다. 침묵은 되돌릴 수 있다 —
  command 가 recovery hold 로 남아 `stranded()` 에 보이고 operator 가 회수한다.
- Rejected: 호출을 `_finalize` 의 기존 `try` 안으로만 옮기는 최소 변경. 그러면 승격 자체가
  일어나지 않아 command 가 lease 만료 → `dead_letter` 로 가고, round 11 `R-3` 이 닫으려던
  영구 침묵이 일부 복귀한다.
- Consequence: `process_next` 밖으로 raw 예외가 나가지 않는다. command 는 `leased` 로 남지
  않는다. `P1-1` 이 닫힌다.
- Owner: Workstream governor.
- Date: 2026-08-17
- Affected item: `MGC-012-P5` wave 7. `ingress_worker.py` `_settle_exhausted_retry`.
- Source: 사용자 선택. round 13 `P1-1`. `review-target-round-13.txt` 의 "D-039 잔여 위험:
  승격 전 committed_decision() 읽기가 실패하는 경우를 측정하지 않았다" 를 닫는다.

## D-043 — Widen The Third Ending Back To The Condition The User Actually Gave

- Status: APPROVED (**안 A**)
- Decision: 소진 종점 3 의 조건을 **"worker 가 마지막 attempt 의 결과를 관측하지 못한 경우"**
  로 되돌린다. `spec.md` FR-026 마지막 문장과 `contracts/interaction-feedback.md` 를 고친다.
  **코드는 안 고친다.**
- Reason: **이것은 원상복구다.** 사용자가 `spec.md:26` 에 기록한 답변은
  "without any worker observing the outcome" 이고 그것이 코드의 실제 조건과 같다. wave 7 의
  `T017` 이 그것을 "no attempt ever completed" 로 좁혔고, **`D-041` 은 그 좁힘을 승인하지
  않았다** — `D-041` 이 승인한 것은 committed-decision 종점을 추가하는 것뿐이다.
- Consequence: 좁힌 조건에서 세 종점 어디에도 안 들어가던 경우가 종점 3 에 들어간다.
  attempt 1 이 완료하고 `RETRY` 를 낸 뒤 마지막 attempt 가 claim 된 채 worker 가 죽으면
  lease 만료 sweep 이 `dead_letter` 로 옮기고 통지는 없다 — 그 동작은 이미 그러하고 바뀌지
  않는다. 계약이 그것을 서술하게 될 뿐이다.
- Owner: Workstream governor.
- Date: 2026-08-18
- Affected item: `MGC-012-P5` wave 8. `spec.md` FR-026, `contracts/interaction-feedback.md`.
- Source: 사용자 선택. round 14 `BP2-4`. `D-041` 을 정정하며 supersede 하지 않는다.

## D-044 — Give The Silent Ending A Real Operator Entry Point

- Status: APPROVED (**안 A**)
- Decision: `stranded()` 와 `committed_decision()` 을 **read-only CLI command 로 노출한다.**
  기존 typer sub-app 패턴을 따라 `governance` sub-app 을 추가한다.
- Reason: `D-042` 가 침묵을 고른 근거 전체가 "침묵은 `stranded()` 로 회수할 수 있다" 이고
  `T017` 이 그것을 계약에 "operator's entry point" 로 적었다. **그 수단이 실재하지 않았다** —
  호출자가 test 와 docstring 뿐이다. 근거 없는 주장을 계약에 남기지 않는다.

  조회 대상이 둘인 이유는, 침묵 종점을 만난 operator 가 알아야 하는 것이 정확히 **"결정이
  실제로 났는가"** 이기 때문이다. `committed_decision()` 이 그 답을 이미 갖고 있다.
- Scope: **조회만이다.** `recovery_hold` 나 `dead_letter` 에서 빼내는 governed mutation 은
  포함하지 않는다. 그것은 ActionToken 과 authority 계약을 건드리므로 별도 item 이고,
  `D-038` 항목 1 이 이미 알려진 한계로 기록해 두었다.
- Consequence: 계약의 "operator's entry point" 문장이 참이 된다. `D-042` 의 근거가 실재한다.
- Owner: Workstream governor.
- Date: 2026-08-18
- Affected item: `MGC-012-P5` wave 8. `src/amplai_foundry/cli.py`, 그리고 필요하면
  `governance/__init__.py` export.
- Source: 사용자 선택. round 14 `BP2-1`.

## D-045 — Dead-Letter An Ingress Command Row That Cannot Be Read

- Status: APPROVED (**안 A**)
- Decision: `claim_next` 가 durable ingress command row 를 `IngressCommandView` 로 만들지
  못하면 그 row 를 `dead_letter` 로 옮기고 `last_error_code` 에
  `INGRESS_COMMAND_UNREADABLE` 을 적는다. `stranded()` 가 그것을 낸다. 사용자 통지는 없다.
- Reason: round 15 `F-1` 이 **P0 로 실측했다.** `_view`(`ingress.py:482`)의
  `json.loads`(`:492`)와 `model_validate` 가 `ValueError` 를 내는데 `process_next` 의
  `except (GovernanceStoreError, IngressError, sqlite3.Error)` 가 그것을 안 잡는다.
  `_view` 는 `governance_transaction` **안**이라 claim 이 rollback 되고, row 는
  `pending`·`attempts=0` 으로 되돌아간다. `claim_next` 는 `received_at` 순으로 고르므로
  그 row 가 매번 다시 뽑히고 **뒤에 들어온 command 가 하나도 처리되지 않는다.**
  reviewer 가 두 command 로 재현했다 — 뒤 것이 세 번 연속 `CRASH` 후 `pending` 에 남았다.

  침묵도 아니다. row 가 `pending` 이라 `stranded()` 의 어느 조건에도 안 걸리고
  `governance stranded` 가 `NONE` 을 낸다. **operator 가 볼 방법이 없다.**
- Rejected: **worker 를 fail-closed 로 멈추기.** 저장소가 손상을 terminal 로 다루는 기존
  입장과는 맞지만, 읽을 수 없는 row 하나가 나머지 전부의 처리를 막는 것은 같다. 차단을
  시끄럽게 만들 뿐 풀지 않는다.
- Rejected: **raw escape 만 감싸고 차단은 미루기.** `IngressError` 로 닫으면
  `CLAIM_FAILED` 가 되지만 row 는 여전히 `pending` 이고 head-of-line 이 남는다. P0 가
  열린 채로 gate 를 못 연다.
- Scope: **읽기 실패에 한한다.** `sqlite3.Error` 나 store 손상은 기존 경로를 그대로 쓴다.
  `dead_letter` 에서 빼내는 governed recovery 는 여전히 범위 밖이다 (`D-038` 항목 1).
- Consequence: 읽기 실패가 durable state 변경을 낳는다. 그 대가로 큐가 풀리고 손상 row 가
  operator 에게 보인다. `INGRESS_COMMAND_UNREADABLE` 은 error code 이지 safe outcome 이
  아니다 — 사용자에게 알리지 않는다.
- Owner: Workstream governor.
- Date: 2026-08-18
- Affected item: `MGC-012-P5` wave 9. `src/amplai_foundry/governance/ingress.py`.
- Source: 사용자 선택. round 15 `F-1`(P0), `F-2`(P1).
- Implementation note (2026-08-26, round 19 `C19-1`): 승인문의 "`last_error_code` 에
  `INGRESS_COMMAND_UNREADABLE` 을 적는다" 는 **`_dead_letter_unreadable` 이 치운 경로의
  표시**다. "읽을 수 없는 row 는 모두 그 code 를 갖는다" 가 아니다.

  `_sweep_recoverable` 의 소진 UPDATE 가 손상 row 를 **먼저** `dead_letter` 로 옮기는 경로가
  둘 있고 (round 18 `N18-2`, round 19 `F19-2`), 그 row 는 `INGRESS_LEASE_EXPIRED` 를 갖거나
  앞선 값을 그대로 갖는다. 셋 다 사실이다 — lease 가 만료됐고 시도가 소진됐으며, 읽을 수
  없다는 것은 그 시점에 아직 발견되지 않았다.

  **`D-045` 가 실제로 요구하는 것은 그 row 가 operator 에게 보이는 것이고**, 세 경로 모두
  `unreadable()` 에 나온다. wave 12 가 이 해석을 task manifest 에만 적고 승인문 쪽에는 안
  적어서 round 19 가 `work-contract` 와의 어긋남으로 잡았다. **승인 문구는 고치지 않고 이
  각주를 단다.**
- Implementation note (2026-08-18, round 16 `A16-1`): 승인문은 "`stranded()` 가 그것을
  낸다" 인데 구현은 `unreadable()` 이 낸다. `stranded()` 는
  `tuple[IngressCommandView, ...]` 를 내고 읽을 수 없는 row 는 그 model 로 만들 수 없다 —
  빈 값을 채운 가짜 view 는 operator 에게 거짓을 보이는 것이다. **operator 가 보는
  `governance stranded` 출력은 승인 의도대로** 두 목록을 함께 낸다. 승인 문구는 고치지
  않고 이 각주를 단다.

## D-046 — Move Workflow Ordering From The Human Procedure To The `/work` Controller

- Status: APPROVED (**안 A** — cortex 와 같게)
- Supersedes: `D-033` 의 Pre-Implement Procedure 절, `D-036`, `D-037`. **셋의 실질(analyze·
  clarify·taskify 를 반드시 거친다)은 유지되고, 그것을 *언제* 거칠지 정하는 주체만 바뀐다.**
- Decision: cortex AMPLAI Loop Runtime V2.1 을 이식하고 **절차 주도권을 `/work` controller
  에 넘긴다.**

  1. user-invocable 개발 명령을 **둘로 줄인다** — `/work <goal>`, `/design <problem>`.
     `speckit-specify`·`clarify`·`plan`·`taskify`·`analyze`·`implement`·`converge` 와
     review·debug 는 **internal capability** 가 된다. 사용자가 그 순서를 지휘하지 않는다.
  2. `CLAUDE.md` 를 **adapter 로 줄이고** 실질 규칙을 `AGENTS.md` 로 옮긴다. runtime 진입점은
     `.ai-team/README.md` 다. cortex 가 쓰는 구조와 같다.
  3. `.ai-team/` 에 runtime layer 를 둔다 — `runtime`, `policy`, `contracts`, `verifiers`,
     `knowledge`, `evidence`. 기존 `.ai-team/skills/taskify` 와 **경로가 겹치므로 정리한다.**
  4. `amplai-foundry verify` 의 7 check 를 `.ai-team/verifiers/registry.json` 으로 옮기고
     profile 을 만든다. check 형식이 이미 같다 — `{id, command, severity, source, description}`.
  5. **semantic runtime(rdflib·ontology TTL·SHACL·MCP)은 가져오지 않는다.** 이 저장소에는
     그 기반이 없고 Knowledge Vault 와 Proposal 모델이 그 자리를 대신한다.
- Reason: **이번 세션이 수동 절차의 대가를 실측으로 보여줬다.** wave 8 → round 15 → wave 9
  → round 16 → wave 10 을 손으로 돌리며 나온 것들이다.

  - 수치를 **여섯 라운드 연속** 틀렸다. 원인은 매번 같다 — 세지 않고 기억하거나 추정한 값을
    적었다. runtime 은 "LLM 자기평가를 PASS 로 쓰지 않는다" 를 원칙으로 두고 deterministic
    verifier 로 판정한다.
  - **수정이 새 blocker 를 만들었다.** round 16 의 blocker 9건 중 다수가 wave 9 의 산물이다.
    Contract → Slice → verify → diagnose → repair 를 작은 단위로 닫는 것이 그 실패를 겨냥한다.
  - reviewer 가 **세 번** 세션 한도로 죽어 판정을 잃었다. "evidence/handoff recoverable" 이
    V1 Done 조건이다.

  고정 순서 자체가 나빴던 것이 아니다. **그 순서를 사람이 매번 손으로 태우는 것**이 비쌌다.
- Rejected: **절반만 — `/work` 를 더하되 speckit 직접 호출도 남기기.** 기존 Decision 을 안
  건드려 위험이 작지만, cortex 가 명시적으로 금지한 상태(사용자가 순서를 지휘)로 남는다
  (`AGENTS.md:15-16`). 두 절차가 공존하면 다음 라운드가 어느 것을 따를지 모호해진다.
- Rejected: **verifier + evidence 만 가져오기.** Decision 이 필요 없고 며칠이면 되지만,
  위에 적은 세 실패는 loop 없이는 안 닫힌다.
- Rejected: **병행 후 A/B 측정.** cortex 설계의 전제("사용자가 순서를 지휘하지 않는다")와
  양립하지 않는다.
- Unchanged: 아래는 그대로다.
  - **구현 후 three-lens subagent review.** `D-033` 이 "승인 절차가 아니라 검증" 으로
    남긴 것이고 cortex loop 에도 review 단계가 있다. P0/P1/Blocking-P2 가 하나라도 있으면
    gate 를 열지 않는다.
  - Knowledge Safety(원칙 I), Governed Mutation Only(원칙 IV), Completion Gate(원칙 III).
  - `analyze`·`clarify`·`taskify` 를 거치는 것 자체. 호출 주체만 controller 로 바뀐다.
- Consequence: `CLAUDE.md` 구조가 바뀐다. `.claude/skills/`·`.agents/skills/`·
  `.ai-team/skills/` 셋에 흩어진 skill 실물도 정리 대상이 된다 (cortex 는 `.agents/skills/`
  를 정본으로 두고 `.claude/skills/` 가 symlink 다).
- Owner: Workstream governor.
- Date: 2026-08-19
- Affected item: 새 workstream `docs/workstreams/amplai-loop-runtime-adoption/`.
  `MGC-012 Package 5` 는 **round 17 이 열린 채로 남는다** — 사용자가 이식을 먼저 하기로 했다.
- Source: 사용자 선택 2026-08-19. 근거는
  `docs/workstreams/amplai-loop-runtime-adoption/FACTS.md` 와 cortex main `3a3eb46b`.

## D-047 — Widen `unreadable()` Instead Of Redefining What "Stranded" Means

- Status: APPROVED (**안 A**)
- Decision: dead-letter write 가 실패해 손상 row 가 회수 불가 상태로 남는 경우를
  `unreadable()` 이 낸다. 조회 범위를 "`_stranded_rows` 가 건너뛴 것" 에서 "**읽을 수 없는
  durable row**" 로 넓힌다. `stranded()` 의 계약 — "no worker will claim again" — 은
  **그대로 둔다.** `governance stranded` 의 출력 형식도 그대로다. `UNREADABLE` 줄의 목록만
  늘어난다.
- Reason: round 17 `F17-2` 를 실측으로 재현했다. `_dead_letter_unreadable` 이
  `sqlite3.OperationalError` 로 실패하면 row 가 `pending`·`attempts=0` 으로 남는데
  `_stranded_rows` 의 WHERE 가 `pending` 을 포함하지 않아 `stranded()` 도 `unreadable()` 도
  빈 목록을 낸다. `attempts` 는 rollback 되는 transaction 안에서만 증가하므로 **시간이
  지나도 보이지 않는다.** `D-045` 가 닫으려던 "operator 가 볼 방법이 없다" 가 다른 경로로
  되살아난 것이다.
- Rejected: **`stranded()` 의 의미를 "진행하지 못하는 row" 로 넓히기.** 가장 직관적이지만
  `contracts/` 의 문구와 docstring 을 고쳐야 하고, `MGC-012-P5-T026` 이 정한 CLI 출력
  계약의 의미도 함께 바뀐다. `pending` 손상 row 는 claim 은 **되는데** 진행이 안 되는
  것이라 "no worker will claim again" 과 실제로 다르다. 계약 하나를 넓혀 두 사실을 뭉개면
  round 16 `C16-1` 이 갈라 놓은 "결정 없음" 과 "알 수 없음" 을 다시 합치는 것과 같은 형태가
  된다.
- Rejected: **실패 시 `last_error_code` 만 남기는 2차 write.** write 자체가 실패하는
  상황에서 또 다른 write 에 기대는 것이라 신뢰도가 낮다. round 16 `FR-2` 가 같은 이유로
  절반만 닫혔다.
- Scope: 조회 범위만 넓힌다. `stranded()` 의 signature·계약·CLI 출력 형식과
  `IngressService.get()` 의 signature 는 건드리지 않는다. `D-042` 의 보수적 침묵도 그대로다.
- Consequence: `unreadable()` 이 `_stranded_rows` 의 부산물이 아니라 자기 조회를 갖는다.
  읽을 수 없는 row 를 찾으려면 후보 row 를 `_view` 로 시도해 봐야 하므로 `limit` 안에서
  도는 비용이 늘어난다. 그 대가로 `D-045` 가 정한 "손상 row 는 operator 에게 보인다" 가
  치우기 실패 경로에서도 유지된다.
- Owner: Workstream governor.
- Date: 2026-08-26
- Affected item: `MGC-012-P5` wave 11. `src/amplai_foundry/governance/ingress.py`.
- Source: 사용자 선택 2026-08-26. 근거는 round 17 `F17-2` 와 wave 11 착수 실측
  (`_dead_letter_unreadable` 실패 후 `stranded()`·`unreadable()` 둘 다 빈 목록).
- Amended (2026-08-26, round 18 `N18-1`): Scope 절의 "읽을 수 없는 durable row" 가 너무
  넓었다. `LIMIT` 이 전체 표에 걸려 `completed` row 가 손상 row 를 창 밖으로 밀어냈다.
  **`D-048` 이 그 범위를 "회수가 필요한 durable row" 로 좁힌다.** 이 Decision 의 핵심
  (`stranded()` 는 그대로, `unreadable()` 만 넓힌다)은 유지된다.

## D-048 — Bound `unreadable()` To Rows That Still Need Recovery

- Status: APPROVED (**안 A**)
- Amends: `D-047` 의 Scope 절. **핵심은 유지된다** — `stranded()` 의 계약과 CLI 출력 형식은
  그대로고 `unreadable()` 만 넓힌다. 넓히는 **대상**을 "읽을 수 없는 durable row" 에서
  "**회수가 필요한 durable row 중 읽을 수 없는 것**" 으로 좁힌다.
- Decision: `unreadable()` 의 SELECT 가 종결 상태(`completed`)를 SQL 에서 제외한다. 남는
  후보는 `pending`, `leased`, `retry_wait`, `recovery_hold`, `dead_letter` 다. 그러면
  `limit` 이 다시 **후보 상한**이 되고 `stranded()` 와 같은 의미를 갖는다.
- Reason: round 18 `N18-1`(P1) 을 두 reviewer 가 **독립으로** 실측했다. `D-047` 대로 넓힌
  조회가 `LIMIT` 을 전체 표에 걸고 **그 다음에** `_view` 로 거른다. 정렬이
  `received_at, command_id` 라 오래된 `completed` row 가 앞을 채우고, 손상 row 앞에 `limit`
  개 이상이 쌓이면 창 밖으로 밀린다. 실측 — `completed` 150개 + 손상 1개에서
  `governance stranded` 가 **`NONE`** 을 냈다. `--limit 1000` 으로는 나온다.

  `governance_ingress_commands` 를 지우는 코드가 `src` 어디에도 없어 `completed` row 는
  무한히 쌓인다. **`D-047` 의 Consequence("손상 row 는 치우기 실패 경로에서도 operator 에게
  보인다")가 command 100건 이후 거짓이 된다.**
- Rejected: **`LIMIT` 을 없애고 전체를 훑기.** 누락은 사라지지만 비용이 표 크기에 비례하고
  CLI 의 `--limit` 이 의미를 잃는다. `stranded()` 는 `limit` 을 지키는데 `unreadable()` 만
  안 지키면 같은 command 의 두 목록이 다른 규칙을 따른다.
- Rejected: **두 조회로 나누기** — stranded 후보와 "치우기 실패로 남은 row" 를 각각 뽑아
  합치기. 가장 정확하고 `D-047` 의 문구를 안 좁힌다. 다만 구조가 하나 늘고, **새 구조가
  요구하는 것을 다시 세야 한다** — round 16 blocker 넷과 round 18 blocker 셋이 모두 그
  자리에서 나왔다. 이번에는 구조를 늘리지 않는 쪽을 고른다.
- Scope: SELECT 의 WHERE 만 바꾼다. `stranded()`, `get()`, `is_unreadable()` 의 signature 와
  계약, CLI 출력 형식은 그대로다.
- Consequence: 손상된 `completed` row 는 `unreadable()` 에 안 나온다. **그것이 옳다** —
  `completed` 는 회수 대상이 아니고, round 18 `A18-1` 이 "회수가 필요 없는 row 가 회수
  목록에 섞인다" 로 지적한 것이 함께 닫힌다. 그런 row 를 조회해야 하면
  `is_unreadable(command_id)` 가 id 단위로 답한다 — 그쪽은 `limit` 이 없다.
- Owner: Workstream governor.
- Date: 2026-08-26
- Affected item: `MGC-012-P5` wave 12. `src/amplai_foundry/governance/ingress.py`.
- Source: 사용자 선택 2026-08-26. 근거는 round 18 `N18-1` 과 두 lens 의 독립 실측.
- Amended (2026-08-26, round 19 `F19-1`): Decision 절 후반의 "`limit` 이 다시 후보 상한이
  되고 `stranded()` 와 같은 의미를 갖는다" 가 **틀렸다.** 두 후보 집합이 실제로 다르고,
  `completed` 외의 벽에서 같은 결함이 재현된다. **`D-049` 가 `LIMIT` 을 SQL 에서 빼
  출력 상한으로 만든다.** "종결 상태를 SQL 에서 제외한다" 는 유지된다.

## D-049 — Make `limit` An Output Cap, Because "Unreadable" Is Not A SQL Predicate

- Status: APPROVED (**안 A**)
- Amends: `D-048` 의 Decision 절 후반. **"종결 상태를 SQL 에서 제외한다" 는 유지된다.**
  틀린 것은 그 다음 문장이다 — "그러면 `limit` 이 다시 후보 상한이 되고 `stranded()` 와
  같은 의미를 갖는다".
- Decision: `unreadable()` 의 SQL 에서 `LIMIT` 을 뺀다. `WHERE state != 'completed'` 와
  `ORDER BY received_at, command_id` 는 그대로 두고, **`limit` 은 python 이 `_view` 실패를
  모으는 개수의 상한**이 된다. 읽을 수 있는 row 는 건너뛰므로 어떤 벽도 손상 row 를 밀어낼
  수 없다.
- Reason: **"읽을 수 없다" 는 SQL 로 판정할 수 없다.** `_view` 를 돌려야 안다. 그래서
  `LIMIT` 을 SQL 에 걸면 **어떤 state 집합을 고르든** 그 집합 안의 읽을 수 있는 row 가
  손상 row 를 창 밖으로 민다. `D-048` 은 `completed` 하나를 빼면 `stranded()` 와 같은
  의미가 된다고 봤는데, 두 후보 집합이 실제로 다르다 —
  `unreadable()` 은 `{pending, leased, retry_wait, recovery_hold, dead_letter}`,
  `_stranded_rows` 는 `{dead_letter, recovery_hold, retry_wait&소진, leased&소진&만료}` 다.

  round 19 `F19-1`(P1) 이 실측했다. `pending`·`retry_wait(미소진)`·`dead_letter`·
  `recovery_hold` 벽 각각에서 `N18-1` 과 같은 signature 가 재현된다. 가장 무거운 재현은
  치우기가 영구히 실패하는 경우다 — **큐가 실제로 막힌 채** `governance stranded` 가 오래된
  `dead_letter` 100줄만 내고 손상 row 는 `UNREADABLE` 로 나오지 않는다. `dead_letter` 와
  `recovery_hold` 도 벗어나는 UPDATE·DELETE 가 `src` 에 없어 `completed` 와 똑같이 영구히
  쌓인다.

  **후보 집합을 좁히는 것으로는 못 닫는다.** 좁히면 `D-047` 이 닫은 round 17 `F17-2`
  (치우기 실패로 `pending`·`attempts=0` 에 남은 row)가 되살아난다.
- Rejected: **두 조회로 나누기.** `D-048` 이 "구조를 늘리면 그 구조가 요구하는 것을 또 세야
  한다" 로 거절했고 그 판단은 여전히 유효하다. round 16 blocker 넷, round 18 셋, round 19
  넷이 전부 그 자리에서 나왔다.
- Rejected: **손상 여부를 durable column 으로 두어 SQL 이 판정하게 하기.** 근본적이지만
  migration 과 모든 쓰기 경로 변경이 따르고 범위가 크게 는다. `D-045` 가 "읽을 수 없는 row 는
  생긴다" 를 전제로 존재하는데, 그 판정을 쓰기 시점에 하려면 그 전제와 충돌한다 — 쓸 때
  읽을 수 있었어도 나중에 손상될 수 있다.
- Scope: `unreadable()` 의 SQL 과 python loop 만 바꾼다. `stranded()`·`get()`·
  `is_unreadable()` 의 signature 와 계약, CLI 출력 형식은 그대로다.
- Consequence: **비용이 `limit` 이 아니라 "종결 아닌 row 수" 에 비례한다.** `D-048` 이
  거절 사유로 든 바로 그 비용을 받아들이는 것이다. 그 대가로 `D-047` 의 Consequence("손상
  row 는 치우기 실패 경로에서도 operator 에게 보인다")가 **처음으로 참이 된다.**

  완화 요소가 둘 있다. `completed` 는 여전히 SQL 에서 빠지고, 실무에서 대부분의 row 는
  결국 `completed` 가 된다. 그리고 이 조회는 operator 가 부르는 진단 경로이지 worker 의
  hot path 가 아니다.

  **`limit` 의 의미가 `stranded()` 와 다르다는 사실은 남는다.** 이제 그것을 docstring 에
  정확히 적는다 — 저쪽은 후보 상한, 이쪽은 출력 상한이다. `D-048` 은 그 차이를 없애려
  했으나 없앨 수 없는 차이였다.
- Owner: Workstream governor.
- Date: 2026-08-26
- Affected item: `MGC-012-P5` wave 13. `src/amplai_foundry/governance/ingress.py`.
- Source: 사용자 선택 2026-08-26. 근거는 round 19 `F19-1` 이 실측한 **벽 다섯**
  (`pending`·`retry_wait`·`dead_letter`·`recovery_hold`, 그리고 `completed` 대조)이다.
- Correction (2026-08-26, round 20 `C20-2`): 이 항목은 처음에 "**벽 6종 전수 실측**" 이라고
  적었다. **틀렸다.** round 19 는 다섯만 했고 `leased` 벽을 "CHECK 제약으로 재현 못 했다" 로
  Not Checked 에 남겼다. 여섯 종은 **이 Decision 이 승인된 뒤** wave 13 이 만든 증거다 —
  승인 근거로 소급 기재된 것이다. 같은 Decision 의 `Reason` 절은 처음부터 정확히 넷을 댄다.
  **승인 판단 자체는 바뀌지 않는다** — 다섯이든 여섯이든 결론이 같다.

## D-050 — Say When The List Is Cut, Because No `limit` Rule Can Be Complete

- Status: APPROVED (**안 A**)
- Decision: `governance stranded` 가 목록이 `limit` 에서 잘렸을 때 **그 사실을 한 줄로
  알린다.** 판정은 service 를 고치지 않고 CLI 가 한다 — `limit + 1` 을 요청해 `limit + 1`
  개가 오면 잘린 것이고, 출력은 `limit` 개까지만 한다. `stranded()` 와 `unreadable()` 둘 다
  같은 규칙을 쓴다.
- Reason: **`limit` 이 있는 한 어떤 규칙도 완전할 수 없다.** round 19·20 이 그것을 두 번
  실측했다.

  | 방식 | 무엇이 밀어내나 |
  |---|---|
  | `SQL LIMIT` (`D-048` 까지) | 창 안의 **읽을 수 있는** row (round 19 `F19-1`) |
  | python 출력 상한 (`D-049`) | **읽을 수 없는** 종결 row (round 20 `F20-1`) |

  둘 다 `limit` 의 본질이다. 출력이 `limit` 개로 제한되면 `limit + 1` 번째는 안 보인다.
  `D-049` 는 벽의 **state 를 여섯으로 전수**했지만 **가독성 축을 세지 않았고**, 그 축을
  `_dead_letter_unreadable` 이 스스로 만든다 — 치운 손상 row 는 전부 `dead_letter` +
  읽을 수 없음이고 `dead_letter` 를 벗어나는 UPDATE·DELETE 가 없다.

  실측 임계값은 정확히 `limit` 이다 — 벽 99 보임, **100 안 보임**. SQL 재계수 손상 101개에
  CLI 출력 100줄이다. `stranded()` 도 같다 — 후보 301개에 100줄, 새로 stranded 된 row 가
  영구히 안 보인다 (round 20 `F20-2`).

  **고칠 수 없는 것을 고치려 하지 않는다. 대신 숨기지 않는다.** operator 가 "더 있다" 를
  보면 `--limit` 을 올려 회수한다. `A19-F2`·`A20-F2` 가 두 라운드 연속 "truncation 표시가
  없어서 operator 가 스스로 알 방법이 없다" 를 지적했고, failure lens 가 그것을
  "`F20-1`·`F20-2` 를 **발견 불가능**하게 만드는 부품" 이라고 적었다.
- Rejected: **`dead_letter` 정리 경로 신설.** 벽이 쌓이는 것 자체를 막으므로 근본적이지만
  `D-038` 항목 1 이 "`dead_letter` 에서 빼내는 governed recovery 는 범위 밖" 으로 명시적으로
  거절한 것이다. 그 결정을 뒤집으려면 별도 설계가 필요하고 범위가 크게 는다.
- Rejected: **`limit` 을 없애고 전부 반환.** 목록이 무한히 길어질 수 있고 CLI 가 감당하지
  못한다. round 20 `A20-F1` 이 잰 비용은 종결 아닌 row 당 5.5 µs 이고 `dead_letter` 는 안
  빠지므로 사고 누적과 함께 단조 증가한다.
- Rejected: **service signature 에 truncation flag 를 더하기.** `stranded()` 의 반환형이
  바뀌면 `D-047`·`D-048`·`D-049` 가 공통으로 건 불변("`stranded()` 의 signature 와 계약
  불변")이 깨진다. `limit + 1` 을 요청하는 것으로 같은 정보를 얻는다.
- Scope: `cli.py` 의 `governance stranded` 만 바꾼다. `IngressService` 의 어떤 signature 도
  바뀌지 않는다. `governance decision` 은 id 단위라 해당 없다.
- Consequence: **CLI 출력 형식이 바뀐다** — 잘렸을 때만 줄 하나가 는다. 안 잘리면 이전과
  같다. `MGC-012-P5-T026` 의 `scope.exclude` 가 배제한 것은 "새 **출력 형식**(JSON 등)의
  계약화" 이고 이것은 사람이 읽는 목록에 한 줄을 더하는 것이라 그 배제에 걸리지 않는다.

  조회가 `limit + 1` 개를 읽으므로 비용이 한 row 만큼 는다. `unreadable()` 쪽은 그 한 row
  를 찾기까지 더 훑을 수 있다 — 손상이 `limit` 개를 넘을 때만이다.

  **`F20-1`·`F20-2` 를 없애는 것이 아니라 보이게 만든다.** 그 구분을 evidence 에 적는다.
- Owner: Workstream governor.
- Date: 2026-08-26
- Affected item: `MGC-012-P5` wave 14. `src/amplai_foundry/cli.py`.
- Source: 사용자 선택 2026-08-26. 근거는 round 20 `F20-1`·`F20-2` 의 임계값 실측과
  `A19-F2`·`A20-F2` 의 두 라운드 연속 지적.

## D-051 — Install AMPLAI Loop Kit 2.1.0 As A Removable Layer, Not A Replacement

- Status: APPROVED
- Decision: synapse 판 **AMPLAI Loop Kit 2.1.0** 을 이 저장소에 설치한다. 중앙 Project
  Store 까지 만들어 실제로 쓰고, 언제든 흔적 없이 제거할 수 있는 형태를 유지한다. 세 가지를
  함께 정한다.

  **(1) Store 를 만든다.** `--project-home` 으로 저장소 밖에 Project Store 를 만들고
  `--app-id amplai-foundry` 로 등록한다. `auto_start` 는 `false` 로 둔다.

  **(2) handoff 는 두 층으로 나눈다.** kit 의 Project Store Work 는 **앱 간 조율 단위**이고,
  `specs/<feature>/handoff.json` 은 **feature 내부 slice 진행 상태**다. 서로 다른 것을
  다루므로 어느 쪽도 상대의 SSOT 가 아니다. kit hook 이 주입하는 "rendered handoff text is
  only a view" 는 **Project Store Work 의 rendered view 를 가리키는 문장**으로 읽는다.
  경계를 `.ai-team/README.md` 의 Directory ownership 절 각주에 명시한다.

  **(3) 규약 위반은 각주로 표시한다.** `.ai-team/README.md` 의 Scope freeze 와 Directory
  ownership 을 **고치지 않고**, "kit 2.1.0 이 설치된 동안의 예외" 를 각주로 단다. 제거할 때
  각주만 지우면 규약이 원문으로 돌아온다.

- Reason: kit 은 이물질이 아니다. `ARC-0003` 이 이미 같은 구조를 설계해 뒀다 — "Hermes는
  Work Manager 후보이고 Claude Code와 Codex는 Implementation Agent 역할을 수행할 수 있다".
  kit 의 `Human → Hermes → Global AMPLAI → Project Store → App Runtimes` 가 그것이다.
  **가려는 방향의 선행 구현이므로 잠시 쓰는 데 방향 위험이 없다.**

  제거 가능성을 실측으로 확인했다. 저장소 복제본에 설치한 뒤 `git checkout` 5개와 `rm -rf`
  로 지우니 `git status` 가 비고 doctor·ruff·test 수가 전부 원상이다. **uninstall 명령은
  없지만 남는 것이 전부 git 추적 대상이고 경계가 명확하다.**

  Store 없이 설치하는 안은 버렸다. `discover_project_home` 이 기본값 없이 `NotFoundError` 를
  던지므로 **파일 27개가 늘고 아무 기능도 안 돈다** — 얻는 것이 없는데 ruff 만 깨진다.

  규약을 고치는 안도 버렸다. Scope freeze 와 Directory ownership 은 장기 규약인데 임시
  설치를 위해 바꾸면 되돌리기 어렵다. 각주는 제거와 함께 사라진다.

- Consequence: 다음이 바뀐다.

  ```text
  ruff        per-file-ignores 에 kit 4파일 추가 (선례: loopctl.py·loopv2.py)
  test        1409 → 1425 (kit tests/ai/ 16개). review 기준선이 이동한다
  .ai-team    문서 3개 + install/ + backups/ + app.json 이 생긴다 (각주로 예외 표시)
  /work       SKILL.md 에 marker 절이 붙는다. 절 번호 `## 11.` 이 중복된다
  세션        SessionStart 가 Project Store 상태를 주입하고 sessionTitle 을 덮는다
  ```

  **`limit` 처럼 이것도 없애는 것이 아니라 관리하는 것이다** — 규약 위반과 절 번호 중복은
  각주와 기록으로 보이게 두고, 제거 절차를 문서에 확정해 둔다.

- Evidence: 복제본 실측이다. 전문은
  `docs/workstreams/amplai-loop-runtime-adoption/KIT-2.1.0-EVALUATION.md`.

  ```text
  kit selftest                 ok: true, check 7개
  dry-run                      27 action, 충돌 0
  설치 전 doctor / verifier    PASS / PASS
  설치 후 doctor / verifier    PASS / FAIL (ruff 223건, 전부 kit 파일)
  설치 후 mypy                 Success (102 files) — packages 범위 밖이라 영향 없음
  kit test                     16개 전부 통과
  제거 후 git status           빈 출력
  제거 후 doctor / ruff / test PASS / All checks passed / 1409 (기준선 일치)
  ```

- Source: `ARC-0003`(역할 경계), `ARC-0007`(Hermes 는 제품 층 Client Partner),
  `.ai-team/README.md`(Scope freeze, Directory ownership), `D-046`(cortex 판 이식),
  kit `manifest.json`·`selftest.py:47`·`amplai_runtime.py:1825`·`amplai_hook.py`.

- Scope: 이 Decision 은 개발 도구 층에만 적용된다. `src/` 제품 코드와 `vault/` canonical
  knowledge 는 건드리지 않는다. **`MGC-014` 의 Hermes(제품 층 Client Partner)와 kit 의
  Hermes(개발 층 조정자)는 다른 것이고 이 Decision 이 둘을 합치지 않는다.**

## D-052 — The Project Store Owns The Supervisor's Code And Its Right To Run

- Status: APPROVED
- Decision: Local Supervisor 는 **Project Store 가 소유한다.** 코드도 Store 에 두고 실행
  권한도 Store 가 통제한다. 앱 repo 에 깔리는 `scripts/amplai_supervisor.py` 는 kit 이
  배포한 사본일 뿐 실행 대상이 아니다.

  ```text
  <PROJECT_HOME>/                    Store (git repo)
  ├── project.json, policy.json      kit 소유
  ├── apps/, contracts/, changes/    kit 소유
  ├── .amplai/locks/                 host-local (Store .gitignore 가 제외한다)
  │   ├── project.lock               kit
  │   └── supervisor.lock            신규 — 단일 인스턴스를 여기서 강제한다
  └── supervisor/                    신규 — kit 이 만들지 않는 영역이라 충돌이 없다
      ├── amplai_supervisor.py       amplai-foundry 에서 동기화한 사본
      ├── amplai_runtime.py          같음
      ├── VERSION                    동기화된 kit 버전
      └── run                        진입점. 락을 잡고 supervisor 를 부른다
  ```

  **소유는 Store, 공급은 amplai-foundry 다.** Store 의 supervisor 코드는 amplai-foundry 의
  사본을 정본으로 삼아 동기화하고, 그 동기화 명령도 amplai-foundry 가 제공한다.

- Reason: supervisor 는 **개념상 이미 앱의 물건이 아니다.** kit 의 아키텍처 그림이 그것을
  Store 아래 별도 층으로 그리고, 코드도 `--app` 인자 없이 `list_apps()` 로 등록된 앱 전부를
  순회한다. Store 하나에 supervisor 하나가 원래 모델이다.

  그런데 kit 이 `scripts/amplai_supervisor.py` 를 `owned_files` 로 **모든 앱 repo 에
  복사한다.** 앱 단위 설치 도구라 코드를 놓을 자리가 앱 밖에 없기 때문이다. 그 결과 세
  저장소에 같은 파일이 생기고 **어느 것을 돌려야 하는지 아무 문서도 정하지 않는다.**
  개념과 배포가 어긋나 있다.

  **단일 인스턴스 보장 장치가 전혀 없다** — `grep` 으로 확인했고 pid 파일도 락도 0건이다.
  `claim_work` 이 Store 락 안에서 `max_concurrency` 를 재검사하므로 *같은 Work 의 중복
  실행*은 막히지만, supervisor 프로세스가 둘 뜨는 것 자체는 아무것도 막지 않는다.
  `--max-workers` 는 프로세스 로컬(`max(1, len(list_apps()))`)이라 전체 worker 총량 상한도
  인스턴스 수만큼 흐려진다.

  락을 Store 에 두면 **누가 어디서 실행하든 Store 가 통제한다.** 코드까지 Store 에 두는
  것은 개념과 배포를 일치시키는 조치다.

- Consequence: 버전 드리프트가 새 위험으로 들어온다. `amplai_supervisor.py` 는 같은
  디렉토리의 `amplai_runtime.py` 를 import 하므로(`sys.path` 에 `SCRIPT_DIR` 삽입) Store 에
  두려면 runtime 사본도 함께 가야 한다. **같은 파일이 앱과 Store 두 곳에 존재하게 된다.**

  그래서 `supervisor/VERSION` 과 각 앱의 `.ai-team/install/amplai-loop-kit.json` 버전을
  대조하고 **불일치면 fail-closed 한다.** 조용히 도는 것보다 안 도는 것이 낫다.

  **우회 경로가 남는다.** 앱 repo 의 `scripts/amplai_supervisor.py` 를 직접 실행하면 Store
  락을 잡지 않는다. kit 원본을 고치지 않는 한 코드로는 못 막으므로 규약과 문서로 막고,
  근본 해결은 upstream 제안으로 올린다. **이 한계를 숨기지 않고 적는다.**

- Evidence: kit 2.2.0 소스 확인이다.

  ```text
  아키텍처 그림    reference/SYNAPSE_INTEGRATION.md — Supervisor 가 Store 아래 별도 층
  인자             --project-home 만. --app 없음
  순회             for app in self.store.list_apps()
  배포             manifest.json owned_files 에 scripts/amplai_supervisor.py
  단일성 장치      grep "lock|pid|flock|singleton" → 0건
  capacity 재검사  claim_work 이 with self.lock() 안에서 active_work_count 확인
  Store 구조       initialize() 가 apps/contracts/changes/.amplai/{local,locks} 만 만든다
  락 성질          write_managed_gitignore 가 .amplai/locks/ 를 제외 — host-local 이다
  ```

- Source: `HANDOFF_2026-08-28_amplai-loop-kit-2.2.0.md` §3(supervisor 설계 과제),
  kit 2.2.0 `scripts/amplai_supervisor.py`·`amplai_runtime.py`·`manifest.json`,
  `.ai-team/runtime/LOCAL_SUPERVISOR.md`.

- **Amended by `D-053` (2026-08-28).** 이 Decision 의 제약 "kit 원본을 고치지 않는다 —
  PR #81 과 독립이어야 한다" 가 무효화됐다. PR #81 이 머지됐고 `D-053` 이 kit 정본을
  amplai-foundry 로 이관했다. **정본을 가지면 단일 인스턴스 락을 `amplai_supervisor.py`
  안에 넣을 수 있고, 이 Decision 이 한계로 인정한 우회 경로(앱 repo 사본 직접 실행)가
  사라진다.** 결정의 실질 — Store 가 supervisor 의 코드와 실행 권한을 갖는다 — 은 그대로다.
  구현은 `specs/007` 이 받고 `specs/006` 은 superseded 다.
- Scope: 이 Decision 은 **Store 의 구조와 supervisor 실행 규약**을 정한다. supervisor 를
  실제로 켤 것인지(`--run`)는 정하지 않는다 — HANDOFF §3 이 활성 세션 라우팅 확인 전까지
  `--run` 을 쓰지 말라 했고 그 판단은 그대로 남는다. **켜지 않아도 이 구조는 필요하다.**

## D-053 — amplai-foundry Owns The Kit Source And Distributes It To The Other Apps

- Status: APPROVED
- Decision: **kit 정본을 amplai-foundry 로 이관하고 여기서 공용 kit 2.3.0 을 만든다.**
  synapse 의 `tools/amplai-loop-kit/` 는 이관 뒤 **지운다.** 이후 synapse 와 cortex 는
  정본이 아니라 **배포 대상**이다.

  **(1) 정본 위치.** `tools/amplai-loop-kit/` — synapse 관행을 따른다. 출처는 synapse
  `main:1c05001b`(PR #81, 2026-08-28 머지)이고 provenance 를 kit 안에 남긴다.

  **(2) 2.3.0 이 담는 것.** 2.2.0 + supervisor 설치 + 공용화.

  ```text
  payload/store/          신규 — Store 에 설치될 supervisor (D-052 구조)
  distribution/           신규 — 배포 대상 설정
  문서 중립화             synapse 전제를 걷어낸다
  ```

  **(3) supervisor 를 kit 이 설치한다.** `D-052` 가 정한
  `<PROJECT_HOME>/supervisor/` 와 `.amplai/locks/supervisor.lock` 을 `install.py` 가
  만든다. **단일 인스턴스 락을 `amplai_supervisor.py` 안에 넣는다.**

  **(4) 배포 경로 설정을 두 층으로 나눈다.**

  ```text
  tools/amplai-loop-kit/distribution/targets.json   커밋. app_id·project_id·role·path_hint
  .ai-team/local/kit-targets.json                   host-local(gitignore). 실제 절대경로
  scripts/kit_distribute.py                         배포 래퍼
  ```

- Reason: `install.py --target` 은 **경로를 하나만** 받는다. 세 앱에 배포하려면 래퍼가
  필요하고, 그 래퍼가 읽을 대상 목록이 있어야 한다.

  **경로를 두 층으로 나누는 근거는 kit 자신에 있다.** kit 은 `apps/<id>.json`(커밋, 논리
  정보)과 `.amplai/local/apps/<id>.json`(host-local, `repo_path` 같은 절대경로)을 이미
  나눈다. 절대경로를 커밋하면 다른 머신에서 깨지므로 같은 패턴을 따른다.

  **정본을 옮기는 이유는 kit 이 synapse 를 전제하고 만들어졌기 때문이다.** 실측했다 —
  설치 로직(`manifest.json`·`install.py`·`fragments/*`)에는 synapse 가 **0건**이고 중립이다.
  그러나 marker 대상 셋이 **synapse 에만 존재한다.**

  | marker 대상 | required | amplai-foundry | synapse | cortex |
  |---|---|---|---|---|
  | `.agents/skills/handoff/SKILL.md` | false | 없음 | 있음 | 없음 |
  | `.ai-team/skills/handoff/SKILL.md` | false | 없음 | 있음 | 없음 |
  | `.ai-team/AUTONOMY_POLICY.md` | false | 없음 | 있음 | 없음 |

  `handoff` skill 은 synapse 고유 개념이다. `required: false` 라 설치는 되지만 **공용
  kit 이 특정 앱의 파일 배치를 전제하고 있다.** 문서 쪽은 더 노골적이다 — `README.md` 가
  "Synapse의 AMPLAI Loop V2를 기준으로" 로 시작하고 `reference/SYNAPSE_INTEGRATION.md` 는
  파일 이름부터 한 앱을 가리킨다. 전체 56건이다.

- Consequence: **`D-052` 의 제약 하나가 무효화된다.** `D-052` 는 "kit 원본을 고치지
  않는다 — PR #81 과 독립이어야 한다" 를 걸었고 그 대가로 **우회 경로를 한계로 인정**했다
  (앱 repo 사본을 직접 실행하면 락을 안 잡는다). 정본을 가지면 락을 kit 안에 넣을 수 있고
  **그 한계가 사라진다.** `D-052` 를 amend 한다 — 결정의 실질(Store 가 supervisor 를
  소유한다)은 유지되고 수단만 나아진다.

  `specs/006-supervisor-ownership` 의 S01·S02 가 kit 기능으로 **흡수된다.** 006 은
  superseded 로 표시하고 이 feature 가 받는다.

  **synapse 사본 삭제는 synapse 쪽 PR 이 하나 더 필요하다.** 그 저장소의 작업이 kit 을
  참조하고 있으면 깨지므로 삭제 전에 확인해야 한다.

  세 앱 모두 2.2.0 dry-run 이 통과하는 것을 확인했다. 2.3.0 도 같아야 하고 그것이
  배포의 전제 조건이다.

- Evidence: 실측이다.

  ```text
  PR #81                MERGED 2026-08-28T04:29:44Z, merge sha 1c05001b
  kit 2.2.0 정본        synapse main:tools/amplai-loop-kit/ (VERSION = 2.2.0)
  설치 현황             synapse 만. amplai-foundry·cortex 는 미설치
  cortex dry-run        ok: true (required_paths 셋 다 있다)
  selftest 2.2.0        ok: true, check 10개
  synapse 언급          56건 — 문서 20, 테스트 fixture 36. 설치 로직은 0건
  install.py --target   단일 경로만 받는다 (install.py:894)
  ```

- Source: synapse `main:1c05001b` `tools/amplai-loop-kit/`,
  `HANDOFF_2026-08-28_amplai-loop-kit-2.2.0.md`, `D-052`(supervisor 소유),
  `D-051`(kit 설치), kit `manifest.json`·`install.py`.

- Scope: 개발 도구 층이다. `src/` 제품 코드와 `vault/` canonical knowledge 를 건드리지
  않는다. **supervisor 를 켜는 것(`--run`)은 여전히 정하지 않는다** — `D-052` Scope 와 같다.
