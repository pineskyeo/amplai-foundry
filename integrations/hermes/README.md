# Hermes V3 adapter

`integrations/hermes/` 는 V3 control plane 의 intake/status/approval 어댑터 manifest 다. 구현은
`src/amplai_foundry/control_plane/hermes.py` (`HermesIdentityMap`, `HermesAdapter`) 에 있다.

- **identity**: 외부 사용자 → AMPLAI actor 매핑은 `runtime.admin` 운영자가 `bind()` 로 만든다.
  대화 텍스트의 repo ID / role / approval token 은 데이터일 뿐 권한이 아니다 (design/17 §5).
- **intake**: `hermes:<channel>:<external_message_id>` 를 idempotency key 로 써서 webhook 재전송이
  같은 goal 을 돌려준다. 같은 message id 에 다른 내용은 `IDEMPOTENCY_CONFLICT` 다.
- **status**: design/17 §6 의 사용자 상태 라벨만 노출한다. lease/outbox/fence/epoch 는 절대 나가지 않는다.
- **approve**: `Authority.issue()` 로 그대로 전달한다. 어댑터는 승인 권한이 없다 (T-087).

기존 `integrations/hermes-amplai/` 는 V2 DRAFT-only Slack 프로파일이며 V3-060 retirement 후보다.
