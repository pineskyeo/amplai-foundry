# Feature Specification: AMPLAI V3 RC-04 — Container Egress Profile

**Feature Branch**: `feat/013-amplai-v3`

**Created**: 2026-09-20

**Status**: Ready for implementation

**Input**: 사용자 `/work` (직전 보고의 다음 항목) + `specs/015-external-qualification/handoff.json` 후속: 컨테이너 안 실제 claude 턴

## Design Basis

`design-reference/design/10_AUTHORITY_SECURITY.md:30` (정책은 egress host+port 를 포함, deny 우선), `:42` (worker 는 egress deny-by-default, no home credential mounts), `:44` (credential 은 agent env 에 장기 노출하지 않는다). REQ-10, backlog V3-022.

## Mechanism

```text
agent container ──(docker --internal network, DNS/route 없음)──▶ egress proxy sidecar ──(bridge)──▶ allowlist host:port 만 CONNECT
```

agent 컨테이너는 internal network 에만 붙는다. HTTPS_PROXY 가 sidecar 를 가리키고 sidecar 는 `deployment/local-egress.json` 의 allowlist 밖 CONNECT 를 403 으로 거부하며 결정 로그를 남긴다. spike (2026-09-20): 직접 egress 는 DNS 실패, api.anthropic.com 은 통과, example.com 은 403, 컨테이너 안 `claude -p` 가 PONG 을 받았고 datadog telemetry 는 거부됐다.

## Scope

| slice | 항목 | 방식 |
|---|---|---|
| S01 | EgressProfile 계약 + ContainerSandbox (EGR-01) | src + unit test |
| S02 | sidecar 배포 + 실측 자격 (EGR-02) | sandbox_up.sh --egress, egress_qualify.py, container 마커 test |
| S03 | 컨테이너 안 실제 turn (EGR-03) | requalify_drivers.py --container → QualificationRunner |
| S04 | closure (EGR-04) | status → ledger |

## 하지 않는 것

production egress policy, credential broker (V3-022 의 나머지), user HOME mount, codex 자격 파일의 agent 측 복사, V3 FINAL 선언.
