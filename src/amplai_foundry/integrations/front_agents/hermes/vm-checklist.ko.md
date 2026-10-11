# Hermes VM 점검표 (H-9, H-14, AC-H8)

Hermes 를 켜기 전과 버전을 올린 뒤마다 확인한다. 한 칸이라도 아니면 Hermes 를 켜지 않는다.
"확인 필요" 칸은 이 저장소가 아직 검증하지 못한 부분이다. 운영자가 방법을 정하고 결과를 남긴다.

## Placement

- [ ] Hermes 전용 VM 이다. 다른 서비스·개인 파일이 없다 (OD-8: 처음에는 이 Mac 의 VM).
- [ ] AMPLAI 운영자 토큰(`operator.token`), authority 키, 제품 store, 알림 webhook URL 파일이 VM 에 없다.
- [ ] VM 안의 비밀은 `$HERMES_HOME/.env`(0600)의 Slack bot·app 토큰과 intake adapter 토큰뿐이다.

## Version

- [ ] Hermes 버전이 `v0.21.6` 으로 고정돼 있다. 올릴 때는 새 버전을 이 점검표로 다시 확인한 뒤 바꾼다 (staged).
- [ ] Hermes 공개 보안 권고(CVE)를 이 버전 기준으로 확인했다 — 확인 필요 (plan.md §1.3: 수정 여부 미확인).
- [ ] 설치 방법과 고정 방법(어느 설치 경로로 v0.21.6 을 고정하는지) — 확인 필요.

## Hermes Settings (`config.yaml.template`)

- [ ] `terminal.backend: docker`, `terminal.docker_volumes: []`, `terminal.docker_network: false`.
- [ ] `agent.disabled_toolsets` 에 `delegation`, `code_execution`, `cronjob`, `browser`, `catalog`, `web`, `search` 가 있다.
- [ ] `skills.write_approval: true`, `memory.write_approval: true`.
- [ ] `mcp_servers.amplai.tools.include` 가 `kit.json` 의 도구 계약 버전(1.0)의 도구 8개와 같다.
- [ ] `SLACK_ALLOWED_USERS` 에 운영자 한 명만 있다.
- [ ] 외부 skill hub 설치를 막는 Hermes 설정 키는 v0.21.6 에서 찾지 못했다. 운영자가 `hermes skills install` 을 쓰지 않는다 (운영 규칙).
- [ ] Hermes 가 스스로 쓴 메모리·skill 이 주입을 지속시키는지 주기적으로 `/skills pending` 과 메모리 대기 목록을 검토한다 (plan.md §8).

## Egress (AC-H8)

허용: Slack, 모델 API(또는 사내 게이트웨이), AMPLAI 접수 경로. 그 밖은 막는다.

- [ ] VM 의 egress 허용 목록 방법(VM 소프트웨어의 방화벽, pf, 프록시 등) — 확인 필요.
- [ ] 허용 목록 밖 주소로의 연결이 실패하는 것을 VM 안에서 직접 시험했다 (예: 임의 외부 HTTPS 주소). 결과와 날짜를 남긴다.
- [ ] Hermes 의 Docker terminal 컨테이너에서 외부 연결이 실패한다 (`docker_network: false`).

## If The VM Is Compromised (H-14)

영향 범위: 연결된 운영자 사칭(같은 operator 귀속으로 goal 제출), 운영자 goal 조회, 실행 중 에이전트에 steer 문장 주입,
일시정지·취소, 메신저를 통한 피싱. 범위 밖: 승인, grant 발급, 권한·파괴적 변경 질문 답변, 운영자가 멈춘 goal 재개.

대응 순서:

1. AMPLAI 호스트에서 adapter 토큰 파일을 지운다. 그 adapter 의 다음 요청부터 401 이다 (AC-H7).
2. Slack 에서 Hermes 앱의 토큰을 폐기한다.
3. `amplai status` 로 최근 goal 과 steer 를 확인하고, 필요하면 운영자 경로로 취소한다.
4. 알림 채널(별도 Slack 앱)의 일시정지·취소 알림을 대조한다. 이 채널은 Hermes 와 독립이다 (F-5).
