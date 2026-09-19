# Contract package — normative design artifacts

JSON Schema 2020-12로 작성한 명세다. 서비스 구현·실제 승인·실행 파일이 아니다. `schema-index.json`에 있는 모든 `$id`를 로컬 registry에 등록하고 network `$ref` resolving을 금지한다. `additionalProperties:false`는 오타와 잘못된 state를 줄이지만 semantic validators와 권한 검사를 대신하지 않는다.

`fixtures/valid`는 synthetic structural example이다. fake identifiers/digests/signatures는 실제 운영 객체나 승인으로 사용 불가다. validator 결과는 구조 검증 결과에만 해당한다. cross-object refs·서명·현재 권한·상태·환경·통계는 별도 semantic/service tests가 필요하다.

immutable object의 identity digest는 unsigned content의 JCS bytes hash다. `signature` 전체(포함된 signed_digest 포함)는 hash 전에 제외한다. 그 외 사용자 내용과 scope/revision은 포함한다. serializer convenience metadata를 추가하지 않는다. artifact digest는 원본 bytes hash다. JSON numeric range는 안전한 정수; 비용은 microunit integer, unknown은 null이다. 버전·timestamp·canonicalization golden tests 없이 plain JSON string hash로 대체하지 않는다.

GoalContract → verification/context refs, VerificationPlan/ContextBundle → contract ref가 서로 순환 hash를 만들지 않도록 **초기 plan/context의 contract_ref는 null**이고 contract가 그 digest를 bind한다. 실행 시 compiled ContextBundle은 이미 frozen contract ref를 가질 수 있으나 이는 ExecutionEnvelope에 연결하고 원 Contract의 bound context를 덮어쓰지 않는다. verification 결과는 해당 실제 contract ref를 보유한다.

서명은 unsigned payload hash에 bind한다. 진짜 key trust/issuer/expiry/revocation 검사는 JSON Schema가 할 수 없다. Actor attribution은 server-authenticated ActorContext와 일치해야 한다. ExecutionGrant는 외부 입력 JSON만으로 발급될 수 없다.

정본 파일: schemas/*(구조), invariant-registry.json(불변조건), gate-matrix.json(강제 시점), semantic-validators.json(객체 간 검증), state-machines.json(상태), api-catalog.json(서비스 경로), storage-model.json(저장 키), runtime-defaults.json(기술 기본값). 자연어 문서와 불일치가 발견되면 구현 전에 DESIGN_CONFLICT로 보고하고 임의의 유리한 쪽을 선택하지 않는다.
