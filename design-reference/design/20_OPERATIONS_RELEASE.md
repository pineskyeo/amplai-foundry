# 20. 운영·배포·호환성·장애 대응

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 배치 profile

`local-dev`: 단일 사용자 CP+격리 worker, explicit project policy. `onprem`: 사내 CP+로컬 CAS+인증된 worker, 외부 egress 제한. `hybrid`: 정책 허용된 데이터에만 cloud provider route. `offline`: pinned packages/local model/캐시된 canonical read, 신규 권한·외부 확인 필요한 write HOLD. `legacy-app-client`: RHEL7/Python3.6 등 앱의 얇은 접속·설치 bootstrap; 현대 CP 전체를 old host에 이식하지 않는다.

CP Python >=3.11은 첨부 Platform의 현재 기준과 맞춘다. dependencies는 lockfile+hash+reproducible build로 pin한다. Python3.6용 기존 Kit 경계를 바꾸려면 host inventory와 사용 owner 승인 후 별도 support matrix에 표시한다. 코드가 Python3.11에서 동작했다는 사실로 RHEL7 support를 주장하지 않는다.

## 2. 운영 설정의 확정과 미확정

기술 baseline defaults는 `contracts/runtime-defaults.json`에 둔다. production authority endpoint, signing identity, approved egress, tenant admins, currency budget, retention/legal constraints, provider account availability는 배포자가 설정해야 하는 **activation prerequisites**다. placeholder가 남아도 read-only design/validate는 가능하지만 write worker enable은 fail closed다. 이 설계가 현재 사내 접근권한을 확인했다고 주장하지 않는다.

## 3. 시작 순서

검증된 release→config/schema checksum→authority reachability/revocation sync→store schema/version→recovery scan→driver probes/qualification→pack verification→scheduler enable→worker admission 순서다. authority unavailable 또는 unresolved unknown effects가 있는 project는 read-only status 제공만 허용할 수 있다. webhook/API listen 전에 auth config를 검증한다.

## 4. 상태·알림

health는 liveness/readiness/authority-ready/scheduler-ready/artifact-ready/driver-available로 분리한다. 프로세스가 살아 있다고 write ready가 아니다. 주요 경보는 outbox backlog, heartbeat loss, unresolved effect age, authority sync gap, disk/CAS pressure, budget overrun reservation mismatch, schema drift, invalid signature, cross-scope deny surge다.

log는 structured JSON, bounded size, secret redaction, correlation ID를 사용한다. event store와 log rotate는 다른 보존 정책이다. default debug logging에 raw prompts/DB tokens를 넣지 않는다. incident bundle export는 scoped/sanitized manifest를 생성한다.

## 5. 장애 runbook

| 장애 | 즉시 조치 | 재개 조건 |
|---|---|---|
| CP crash | owner epoch 교체·lease invalidate·outbox/inbox reconcile | pending effects 분류, duplicate-safe dispatch |
| authority down | 신규 write/effect HOLD | authoritative sync와 expiry 재검증 |
| worker lost | lease 만료·resource quarantine | process/external effects 종료 또는 unknown 기록 |
| CAS missing/corrupt | 관련 goal verify 차단 | backup restore와 digest 일치 |
| provider rate limit | bounded jitter·queue backpressure | budget/time window 내 retry |
| leaked credential | revoke/kill switch·audit | 새 scoped token+원인 격리 |
| disk full | stop admission·protect authority/event consistency | space+integrity check 완료 |
| signing key revoke | 신규 installs/promotions 차단 | 신뢰 가능한 새 release chain 검증 |

SLO는 workload baseline을 수집해 정한다. 임의 99.99% 약속을 하지 않는다. 초기 acceptance는 fault 시 권한·증거·재시도 정합성이 깨지지 않는지와 bounded recovery를 시험하는 것이다.

## 6. Supply chain

release package에는 source commit, build tools, dependency lock hashes, SBOM, test/qualification refs, artifact digests, publisher signature/attestation, migration compatibility를 포함한다. SLSA의 provenance 관점을 활용하되 구현하지 않은 level을 달성했다고 표시하지 않는다. [R36]

pack/driver는 latest auto-update가 아니라 staged qualification→release set pin→authorized rollout이다. provider 문서/SDK 변화 감시는 Research/Operations job으로 제안될 수 있으나 승인 없는 시스템 변경을 수행하지 않는다. 도구 접근권한이 없는 미래 자동화를 이 설계만으로 활성화했다고 주장하지 않는다.
