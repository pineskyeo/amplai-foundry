# 31. 환경별 실행과 지원 매트릭스

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 기본 profile

| Profile | Control Plane | Worker/model | 권한·데이터 | V3 검증 요구 |
|---|---|---|---|---|
| local-dev | modern Python≥3.11 local disk | isolated CLI/session | 등록된 project scope, budget | baseline driver + state/fault suite |
| onprem | 사내 modern host, local DB/CAS | local API/approved CLI worker | 기본 external egress deny | internal auth/secret/backup/fleet test |
| hybrid | onprem truth/authority | 승인된 cloud 또는 local route | data classification별 허용; 자동 유출 fallback 금지 | both routes conformance+privacy policy |
| offline | pinned local release + cached canonical | qualified local model only | 신규 grant·외부 verification 필요한 write HOLD | cold start/cache staleness/deny tests |
| legacy-app-client | modern CP 별도 | old host의 얇은 client/installer | 기존 앱 build/runtime 독립 유지 | 실제 OS/Python version별 smoke/uninstall |
| managed optional | 동일 Foundry authority | managed API profile | vendor persistence/region/secret policy 별도 검토 | exact provider capability/exit/effect tests |

## 2. CP/Kit 배포 의존

CP는 core source의 Python>=3.11 기준을 따른다. 앱의 RHEL7/Python3.6 경계와 충돌하면 얇은 client를 유지한다. 앱의 production binary가 AMPLAI session/lib를 import하도록 만들지 않는다. CP가 꺼져도 기존 앱 서비스 동작은 유지돼야 한다. 개발 automation은 HOLD할 수 있지만 production business dependency로 새로 삽입하지 않는다.

## 3. 설정을 provider 기능과 혼동하지 않는다

model이 async를 지원해도 선택 host가 expose하지 않으면 native async capability는 false다. driver가 API상 기능을 선언해도 실제 sandbox/권한/versions 조합이 conformance를 통과하지 않으면 qualified가 아니다. profile activation은 required capabilities⊆qualified∩granted 조건이다.

## 4. 운영 원칙

단일 active scheduler를 기본으로 한다. worker pool 증가와 다중 CP writer는 다른 기능이다. V3는 여러 worker를 제어할 수 있으나 shared SQLite 파일을 여러 host가 여는 HA를 지원하지 않는다. uptime 요구가 강해지면 port-compatible DB/scheduler backend를 별도 검증해 도입한다.

비용·canary 비율·retention·key identities는 template value가 아닌 actual deployment config로 채운다. 검증된 combination만 support matrix에 올리고 미검증 항목은 experimental/disabled/not-tested로 표시한다. 이 ZIP의 합성 fixture는 어떤 환경의 credential/승인/모델 선택값으로도 사용하지 않는다.
