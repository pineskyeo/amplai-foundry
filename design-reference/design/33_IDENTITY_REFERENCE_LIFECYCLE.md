# 33. 객체 identity·해시·참조 수명주기

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 세 가지 식별자를 구분

business ID는 같은 Goal/Work를 계속 가리키는 이름, revision은 immutable definition의 순번, digest는 정확한 내용 bytes를 가리킨다. row_version은 mutable aggregate의 CAS 버전이다. 이 네 개를 서로 대체하지 않는다. scope는 tenant/project로 항상 함께 대조한다. `latest`는 UI projection에서만 해석할 수 있으며 executing contract에는 고정 ref가 필요하다.

## 2. hash 범위

JSON object digest는 `signature` container와 transport-only envelope를 제외한 unsigned content의 JCS bytes다. metadata가 hash 대상인지 schema마다 임의로 달리하지 않는다. top-level definitions의 created_at/id/revision은 내용에 포함되므로 freeze 시 이미 정해져 있어야 한다. artifact digest는 raw bytes. digest 문자열을 object 자기 필드로 넣어 hash하지 않는다.

## 3. Compiler 결정성

ID/created_at/revision은 compile **전에** intake/allocator가 durable CompileRequest에 한 번 부여한다. compiler는 frozen metadata+contract+plan+registry snapshot을 순수 입력으로 받고 실행 중 now/random UUID를 호출하지 않는다. 동일 frozen request에 대한 compile은 같은 output/digest다. 새로운 request에 다른 created_at/ID가 부여되면 digest가 다를 수 있으며 이는 비결정적 compiler 결함이 아니다.

## 4. 참조 순환 금지

immutable identity graph는 backward-reference DAG여야 한다. 특히 다음을 지킨다.

- 초기 Resolution의 candidate_contract_ref는 null. Contract가 그 Resolution을 참조한다. 나중의 Resolution projection/new revision은 Contract를 가리킬 수 있지만 기존 Contract의 resolution_ref를 덮어쓰지 않는다.
- 초기 VerificationPlan/ContextBundle의 contract_ref는 null. Contract가 이 초기 plan/context를 bind한다. Run용 context는 frozen Contract를 참조하는 새 artifact이고 ExecutionEnvelope가 이를 참조한다. 기존 Contract는 새 run context를 backfill하지 않는다.
- HarnessChangeProposal의 experiment_plan_ref는 후보/검증 조건만 가진 **pre-experiment definition**이다. 이후 Frozen EvalExperiment는 proposal_ref를 가질 수 있다. proposal을 수정해 그 EvalExperiment를 다시 hash dependency로 넣지 않는다.
- HarnessComposition.qualification_ref는 이미 존재하는 driver/model/pack 자격 근거 집합이다. 완성 composition에 대한 후속 평가 보고서는 외부 attestation/reverse registry relation으로 연결한다.
- ReleaseSet의 qualification_matrix_ref는 component compatibility 근거다. 최종 release conformance/promotion receipt는 release ref를 향하는 별도 append-only object이며 release 내용을 backfill하지 않는다.

이 규칙은 객체 관계를 graph DB로 옮기면 해결되는 문제가 아니다. hash 정의와 lifecycle 설계의 문제다. `SEM-24`와 identity property tests가 cycle을 잡아야 한다.

## 5. Artifact binding

WorkGraph.produces의 output name은 node 안에서 유일하다. consumes는 local input name, from_node, **output_name**, external_ref, media_type를 가지며 from_node+output_name 또는 external_ref 중 정확히 하나의 source를 선택한다. producer output이 실제 WorkOutput registry에서 current graph/run/verdict와 bind되기 전에는 consumer가 준비되지 않는다.

worker artifact.produced event의 output_name은 declared produces에 있어야 한다. 실제 digest/media를 ArtifactService가 확인한 뒤 trusted verifier가 결과를 판정한다. worker가 임의의 artifact를 consumer input으로 직접 주입하지 못한다. 이름만 같고 hash가 다른 output은 서로 다른 revision의 결과다.

## 6. 시간과 승인

timestamp 형식 검사는 실제 clock/expiry 의미 검사를 대신하지 않는다. server-authoritative time으로 not_before/expiry를 확인한다. worker clock은 audit observation이다. 오래된 approval·context가 구조상 valid JSON이어도 active authority라는 뜻이 아니다. fixture의 synthetic times/keys는 실제 실행에 사용할 수 없다.
