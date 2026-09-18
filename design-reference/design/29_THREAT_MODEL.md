# 29. 위협·실패 모델과 통제 매핑

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 보호 자산과 공격 표면

보호 자산은 source code/사내 데이터, canonical knowledge, decisions/grants, release signing keys, credentials, runtime/evidence integrity, budget, production apps다. ingress는 사용자 메시지·repo/doc attachments·tool/MCP responses·driver event stream·pack registry·webhooks·migration archives·eval datasets다.

| 위협 | 경로 | 통제·검증 |
|---|---|---|
| confused deputy | 사용자 project ID/role 위조, 다른 app hint | ActorContext + scoped lookup + grant/action binding; T scope group |
| indirect prompt injection | README/검색결과가 policy 변경 요구 | trust-labelled context + non-LLM policy/broker; security group |
| excessive agency | planner가 추가 scope/production action 수행 | capability intersection + protected non-goals + budget; G-03/06/10 |
| data exfiltration | external provider/tool/redirect/secret env | egress allowlist·classification routing·secret broker·redaction |
| supply chain | unsigned pack/model profile/driver schema drift | digest/signature/qualified release sets; G-07/15 |
| evidence fabrication | agent-declared digest/pass/judge score | server-computed artifacts + frozen verifiers + attestation; G-11/12 |
| eval gaming | holdout leakage, test deletion, criterion weakening | protected surfaces·separate approval·independent corpus; G-16/22 |
| stale authority | revoked grant + old worker + changed contract | pre-effect authoritative check·fence·immutable refs; G-09/10 |
| duplicate effects | outbox replay/timeout/callback duplicate | scoped idempotency+inbox+reconcile; G-10/21 |
| resource exhaustion | graph explosion/recursive subagents/infinite repairs | root budget·max nodes·depth/concurrency caps·stop signatures |
| lost state | CP crash/CAS gap/clock skew/disk full | UnitOfWork·CAS staging·server time·owner epoch·fault drills |
| destructive cleanup | legacy name heuristic/user override overwrite | owned digest/live ref/backup/grant gates; G-20 |

## 2. 공격자와 실수의 구분

모든 문제가 악의적 공격일 필요는 없다. 잘못된 model output, 오래된 문서, 정상 네트워크 timeout도 같은 안전 실패를 유발한다. 통제는 악성/실수 양쪽에 동일하게 적용한다. 사용자 자신이 임시로 policy를 낮추려 해도 조직 권한과 보호 invariant에 따라 필요한 승인/격리를 거쳐야 한다.

## 3. 남는 한계

허용된 tool 자체가 잘못 구현됐거나 external system이 idempotency 의미를 지키지 않으면 AMPLAI 내부 transaction만으로 안전을 보장할 수 없다. native subprocess/agent가 sandbox control을 제대로 상속하는지도 qualification이 필요하다. 단일 host compromise는 해당 host 권한의 위험을 수반한다. 별도 VM/호스트/credential 분리와 감사·revoke 절차로 영향을 줄이며 'sandbox이므로 완전 안전'이라 쓰지 않는다.

모델이 UX rubric을 잘못 평가할 수 있고 독립 model judge도 편향될 수 있다. critical/business acceptance에는 deterministic evidence와 human review를 적절히 혼합한다. **보안 gate와 통계 uncertainty를 숨기지 않고 운영 상태에 노출하는 것**이 이 설계의 요구다.
