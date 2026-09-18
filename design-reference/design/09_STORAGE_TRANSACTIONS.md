# 09. 저장소·트랜잭션·정합성

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 저장의 책임을 분리한다

**파일이 모두 SSOT인 구조도, SQLite 한 곳에 모든 의미를 넣는 구조도 채택하지 않는다.** 객체별 소유자를 하나로 고정한다. Git은 승인된 지식·정책·정의의 배포 가능한 정본을 관리하고, Authority Store는 현재 유효한 승인·grant·revocation의 권한 원장이며, Runtime Store는 작업 상태·lease·예산·이벤트 원장이다. Git에 복제한 실행 요약은 projection이지 실행을 재개하는 기준이 아니다.

| 객체 | 정본 / 쓰기 주체 | 사본·투영의 역할 |
|---|---|---|
| source/knowledge/decision content | Foundry canonical Git + 기존 governance publish 경로 | ContextBundle이 digest로 고정하여 읽음 |
| 승인·ApplyGrant·revocation | Authority Store / AuthorityService | 감사용 export는 권한 부여 불가 |
| GoalContract·WorkGraph revision | content-addressed immutable blob + Runtime aggregate pointer | Git export는 설계·리뷰용 |
| goal/work/run/lease/reservation | Runtime Store / RuntimeService | status UI, Hermes, SSE는 read projection |
| artifact·verifier result | CAS + ArtifactRegistry | 첨부·HTML은 참조만 사용 |
| 집계 metric | Observatory projection | RunRecord와 원시 event에서 재생성 가능 |
| model/pack/harness definition | 서명된 ReleaseSet이 고정한 정의 | 설치된 host surface는 생성물 |

한 DB에 구현할 수 있더라도 논리 소유권은 유지한다. 기존 Authority DB와 신규 Runtime DB의 **서로 다른 트랜잭션을 원자적이라고 부르지 않는다**. [R30]

## 2. V3 기본 배치

단일 활성 control plane, 단일 로컬 Runtime SQLite, 로컬 CAS, 로컬 authority 연결로 시작한다. WAL/foreign_keys/busy_timeout을 명시하고 트랜잭션은 짧게 유지한다. **NAS/NFS/SMB 파일을 SQLite live DB로 사용하지 않는다.** 원격 worker는 HTTPS/API로 접속한다. 다중 writer control plane 및 PostgreSQL backend는 포트 수준의 확장점이며 별도 conformance 없이는 지원으로 표시하지 않는다. CP 프로세스 이중 기동은 exclusive owner lock + DB owner epoch로 차단한다. 다중 호스트 자동 leader election은 초기 필수 범위가 아니다. [R30]

DB 작업 중 LLM, 외부 HTTP, Git push, 파일 렌더링을 실행하지 않는다. 그것들은 Effect/Job으로 분리한다. SQLite schema migration은 transactional 가능한 단계와 CAS/file 단계가 분리된다. 저장소를 여는 서비스는 schema major 불일치를 read-write로 열지 않는다.

## 3. 논리 테이블과 키

전체 정의는 `contracts/storage-model.json`을 정본으로 사용한다. 모든 project 데이터 PK/FK에 `tenant_id, project_id`를 포함한다. ID가 우연히 전역 고유해도 scope 검증을 생략하지 않는다.

| 그룹 | 테이블 | 필수 제약·인덱스 |
|---|---|---|
| intake | intents, resolutions, questions | actor binding; `(scope, external_message_id)` dedupe |
| 정의 | goal_revisions, graph_revisions, artifact_refs | revision immutable; digest unique; active pointer CAS |
| 실행 | goals, works, runs, leases | row_version CAS; goal+node unique; active lease partial unique |
| 자원 | reservations, resource_claims | root budget sum; write resource unique; release idempotent |
| 변경 | steering_events, replan_requests | client event id unique; accepted/applied revision 별도 |
| 메시지 | runtime_events, inbox, outbox | aggregate seq unique; global cursor monotonic; delivery dedupe |
| 결과 | effects, evidence, verdicts | effect idempotency unique; artifact/verifier/subject digest 고정 |
| 평가 | eval_experiments, eval_runs, promotions | frozen manifests; protected holdout refs; promotion CAS |
| 보안 | grant_refs, revocation_watermarks | 참조만 저장; 최종 권한은 AuthorityService가 결정 |

인덱스: ready scheduling `(scope,status,not_before,priority,created_at)`, run recovery `(status,last_heartbeat_at)`, outbox `(delivery_status,next_attempt_at)`, event `(scope,cursor)`, root budget `(scope,goal_id,state)`. 모든 pagination은 snapshot/cursor 기반이며 tenant 경계를 넘는 global cursor는 외부로 반환하지 않는다.

## 4. 명령 처리의 원자 경계

```text
Authenticate -> authorize command -> validate syntactic input
BEGIN IMMEDIATE
  load current aggregate + row_version
  verify immutable refs + state + generation
  reserve budget/resource if needed
  update aggregate using CAS
  append aggregate event(seq = old_seq + 1)
  append outbox item, if external dispatch is required
  persist idempotency response digest
COMMIT
return committed response
```

중복 idempotency key + 같은 payload digest는 원래 결과를 반환한다. 같은 key + 다른 payload는 `IDEMPOTENCY_CONFLICT`. TTL 만료는 기록을 삭제해 위험한 write를 재허용하는 근거가 아니다. 일반 조회성 command dedupe와 irreversible effect receipt의 보존 기간을 분리한다.

외부 consumer는 inbox에 `(producer_id,event_id)`를 먼저 기록한 트랜잭션과 상태 반영을 함께 commit한다. ACK 전 crash하면 다시 전달되어도 중복 적용하지 않는다. delivery 자체는 **at-least-once**이며, exactly-once effect는 외부 시스템의 idempotency/reconciliation 지원이 있는 범위에만 한정된다.

## 5. Authority와 Runtime 사이

실행 admission은 authoritative grant를 조회하고 정확한 scope/action/contract_digest/graph_revision/expiry/policy_version을 대조한다. runtime에 grant reference를 저장해도 권한이 영구 고정되지 않는다. **부작용 직전에 Broker가 다시 조회**한다. 조회 불가·revocation watermark gap은 write HOLD다. read-only는 policy가 허용하는 cached lease 범위만 허용하며 오프라인에서 신규 권한을 생성하지 않는다.

grant revoke 직후 이미 전송된 외부 effect를 완전히 되돌릴 수 있다고 약속하지 않는다. `dispatched_before_revocation`, `unknown`, reconciliation 결과를 구분한다. 승인 취소 이후 새 side effect가 발생하지 않도록 중앙 Broker와 epoch fencing을 사용하며, 외부 시스템이 요청을 수신한 뒤의 결과는 Effect protocol로 추적한다.

## 6. CAS와 artifact commit

객체는 임시 파일에 기록 -> hash 계산 -> 허용 크기·형식·secret scan -> 같은 filesystem atomic rename -> registry commit 순서다. registry보다 파일이 먼저 생기면 orphan은 안전하게 quarantine 가능하다. 파일보다 registry가 먼저 보이는 상태를 만들지 않는다. upload는 content-type/extension을 신뢰하지 않고 media validator로 확인한다. 경로가 아니라 ArtifactRef를 교환한다. 경로 탈출·symlink·archive bomb 차단은 ingestion boundary 책임이다.

digest는 `sha256:<64 lowercase hex>`; JSON hash는 RFC 8785 JCS canonical bytes를 사용한다. `sort_keys=True`만으로 JCS라고 하지 않는다. 정수 범위는 I-JSON 안전 범위, NaN/Infinity 금지, timestamp는 UTC `Z` 정규형. digest field 자기 자신과 transport metadata는 hash 대상에서 제외하며 각 schema의 `digest_scope`에 고정한다. [R31]

## 7. 백업·복원·삭제

DB consistent backup API + CAS referenced object manifest + release pins + authority checkpoint를 함께 보관한다. live WAL 파일을 무작정 복사하지 않는다. 복원은 새로운 owner epoch로 시작하고 이전 worker lease를 전부 무효화한다. 외부 effect receipts와 grant revocation 상태를 reconcile하기 전 write 실행을 재개하지 않는다.

삭제는 mark -> reference scan -> retention/legal hold check -> approval -> tombstone -> physical purge 순서다. active contract/context/release/experiment가 참조한 digest는 GC 제외다. 최신 N개 보존만으로 evidence를 삭제하지 않는다. 민감정보 삭제 의무가 생기면 원문 삭제·redacted replacement·tombstone·감사 기록을 함께 처리하며 hash 존재만으로 원문 복원이 가능하다고 주장하지 않는다.
