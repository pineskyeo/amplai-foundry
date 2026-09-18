# Fixture 읽는 법

`valid/`의 의미는 **JSON Schema 구조가 유효하다**는 뜻뿐이다. ID·digest·예산·시간·서명·프로필은 모두 합성 예시다. 실제 승인/환경/실험이 존재하거나 운영에 사용할 수 있다는 뜻이 아니다. 특히 TEST-KEY-NOT-TRUSTED 서명은 schema 모양만 맞춘 0 바이트이므로 실제 AuthorityService가 반드시 거절해야 한다.

`invalid/`는 구조 단계에서 거절될 사례다. `semantic-negative/`는 구조는 맞지만 cross-reference·time order·authority·graph 같은 의미 검증에서 거절돼야 하는 사례다. 이번 설계 검수에서는 앞의 구조적 성공/실패만 실제 검사하고 semantic-negative의 런타임 거절은 테스트 명세로 남긴다.

`eval/test-catalog.json`은 다음 구현에서 실행해야 하는 테스트 목록이다. NOT_RUN 표기를 테스트 성공으로 바꾸지 않는다. JSONSchema에서 통과한다고 실제 효과·권한·Goal 달성이 보장되는 것은 아니다.
