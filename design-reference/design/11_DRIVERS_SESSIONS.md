# 11. AgentDriver·Session·Model·Sandbox

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 네 가지를 혼합하지 않는다

`AgentDriver`는 호출/이벤트/세션 transport, `ModelProfile`은 모델 ID와 제공자·추론 설정, `SessionStore`는 재개에 필요한 상태, `SandboxDriver`는 격리된 실행 환경이다. **AstraDriver는 만들지 않는다.** Astra 모델은 Codex/Responses 등 실제 드라이버 위의 ModelProfile로 선택한다. 한 provider에 강한 기능이 있다고 모든 host에서 동작한다고 가정하지 않는다. [R01,R02,R09]

AMPLAI는 model 자체의 compaction/subagent/async tool 기능을 중복 구현하지 않는다. 대신 사용 가능 여부·범위·실패 의미·usage를 관측하고, provider가 보장하지 않는 durable ledger·권한·budget·recoverability를 관리한다.

## 2. Driver port

| method | 입력 | 반환 / 조건 |
|---|---|---|
| `probe` | binary/API version, environment ref | declared + observed capabilities; side effect 없는 probe |
| `prepare` | ExecutionEnvelope, sandbox lease | driver session ref; no work started |
| `start` | prepared ref, immutable prompt/context refs | accepted handle + event cursor; success 의미 아님 |
| `poll/subscribe` | exact run handle, after cursor | normalized event batch; dedupe 가능한 event ID |
| `steer` | SteeringEvent, expected native turn | accepted/unsupported/stale; 적용 여부 별도 |
| `pause/cancel` | run handle, reason | requested/acknowledged; process/tool 종료 확인 별도 |
| `checkpoint` | run handle | resumable opaque ref or unsupported; credentials 미포함 |
| `resume` | exact session + compatible versions | resumed/new_session_required/stale |
| `collect` | stopped handle | artifacts/usage/errors; authority 결정하지 않음 |
| `destroy` | prepared/run handle | idempotent resource teardown; effect rollback 아님 |

driver는 `invoke shell arbitrary bypass` 같은 별도 escape hatch를 갖지 않는다. normalized errors는 auth/unavailable/rate_limit/context_limit/capability/session_stale/cancelled/unknown_effect/provider_error로 분류한다. provider stderr 전문을 사용자에게 내보내기 전 redact한다.

## 3. Qualification matrix

| 드라이버 | V3 포지션 | 필수 자격시험 |
|---|---|---|
| ClaudeCode CLI | 기본 로컬/격리 worker 후보 | explicit session resume, JSON parsing, allowed tools, process-tree stop, usage unknown 처리 |
| Codex CLI | 기본 로컬/격리 worker 후보 | exact run/session correlation, JSONL fragmentation, nonzero/empty output, sandbox policy |
| Codex App Server | **실험적 opt-in** | version-generated schema, authentication/stdio boundary, turn fencing, disconnect recovery |
| OpenCode Server | 선택 profile | password/auth explicitly on, async 204 + event correlation, SSE recovery, permissions |
| OpenAI Responses | 선택 API profile | original call_id pairing, async pending tools, WebSocket reconnect, budget across continuations |
| Managed Agents API | 선택 managed profile | region/data/credential policy, durable handle, authority boundary, SDK change qualification |
| 온프레미스 API | 데이터 제한 profile | protocol subset, usage/provenance, unavailable capabilities fail closed |

2026-09-15에 확인한 Codex app-server 문서는 experimental/production unsupported 경고가 있으므로 이것을 V3 기본 production transport로 고정하지 않는다. OpenCode async endpoint의 204는 접수이며 완료가 아니다. managed API는 최신 공급 옵션일 뿐 Foundry를 대체할 이유가 아니다. [R03,R22,R25]

`declared`(문서/설정), `observed`(현재 endpoint probe), `qualified`(AMPLAI conformance 통과), `granted`(이 Run에 정책 허용)를 따로 저장한다. 드라이버 버전이 바뀌면 이전 qualification을 그대로 재사용하지 않는다. 버전·OS·sandbox·모델 조합의 재검증 범위는 release compatibility matrix가 정한다.

## 4. Async와 native subagent

외부 tool 요청은 `amplai_effect_id ↔ provider_call_id ↔ native_job_id`를 일대일 또는 명시된 parent-child 관계로 저장한다. 결과가 다른 run/session에 섞이면 reject한다. 모델이 tool pending 중 다른 일을 해도 AMPLAI resource/budget reservation은 root goal에 유지한다. tool result 재전송은 provider 문서의 protocol 허용 범위에 맞춰 dedupe한다. [R04]

native subagent는 자체 권한자가 아니라 parent execution capability의 subset을 받는다. parent가 완료됐다고 child process가 자동 정리됐다고 가정하지 않는다. 드라이버가 child usage/취소/범위 제어를 제공하지 못하면 그 risk profile에서는 native delegation을 비활성화하고 AMPLAI가 명시적 Work로 분리한다. 동시성 제한은 AMPLAI+native 합계가 정책 상한을 넘지 않아야 한다.

## 5. Steering 전달

native steer 지원 시 expected turn ID와 event ID를 묶는다. ACK=수신, APPLIED=모델/세션이 적용 확인, EFFECTIVE=새 revision으로 뒤의 effect admission이 적용된 상태를 분리한다. provider queue disconnect 손실 가능성을 고려해 AMPLAI ledger에 먼저 commit하고 미확인 event를 reconcile한다. duplicate steering이 contract를 두 번 수정하지 않도록 idempotency를 둔다. [R05,R22]

native 미지원이면 safe checkpoint에서 pause -> revision 재평가 -> 새 세션/새 prompt로 resume한다. run 중 stdin에 텍스트를 임의 삽입하여 '지원'했다고 하지 않는다. 이미 시작한 tool 취소·rollback은 provider steering과 별도다.

## 6. Session과 모델 교체

resume은 exact opaque session ID와 driver binary/API version, model compatibility, sandbox snapshot digest, context manifest, contract/graph revision을 대조한다. CLI의 '가장 최근 대화 계속' 옵션은 동시 작업 환경에서 사용하지 않는다. raw nested JSON 어디서나 `session_id`를 찾아서 붙이는 heuristic을 제거하고 version-specific typed decoder로 바꾼다. [R23]

모델 교체는 checkpoint에서만 기본 허용한다. 기존 usage·실패·evidence는 유지한다. reasoning cache/세션 format은 provider별이라 portable이라 주장하지 않는다. resume 불가능하면 canonical facts + approved decisions + artifacts + failed attempts 요약으로 새 session을 만든다. 비공개 사고과정의 복제를 요구하지 않는다.

## 7. 지원 제한의 표기

`unsupported`를 false-success로 바꾸지 않는다. README와 UI에 tested matrix만 '지원'으로 표시한다. 실제 설치된 provider release는 구현 시 다시 pin/probe해야 하며 이 설계 ZIP은 어떤 SaaS credential이나 현재 계정의 사용 가능성을 검증한 결과가 아니다.
