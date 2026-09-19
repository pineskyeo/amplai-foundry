# Feature Specification: AMPLAI V3 RC-03 — External Qualification

**Feature Branch**: `feat/013-amplai-v3`

**Created**: 2026-09-19

**Status**: Ready for implementation

**Input**: 사용자 지시 "머지하고 다음도 진행해줘 전부 하는걸로해" + `specs/013-amplai-v3/dev03-delivery-evidence.json` external_qualification_pending 9건

## Scope

| slice | 항목 | 방식 |
|---|---|---|
| S01 | 설치 패키지 의존성 (EXT-06) | wheel → 빈 venv → tests/v3 |
| S02 | Python 3.12/3.13 매트릭스 (EXT-05a) | uv venv × 2 |
| S03 | claude/codex 실제 바이너리 (EXT-01) | 최소 1턴 + 실제 decoder/normalizer + QualificationRunner |
| S04 | OpenCode 서버 경계 (EXT-02) | opencode serve + 실제 OpenCodeDriver.probe |
| S05 | CI 브라우저 fixture 경합 (EXT-05b) | kit fs.rm retry + reseal |
| S06 | docker/OTLP/holdout/Responses/통계 (EXT-03/04/07/08/09) | probe 후 UNAVAILABLE 장부 |
| S07 | closure | external status → ledger |

## 하지 않는 것

V3 FINAL 선언, production cutover, 유료 API 대량 호출, 가짜 pass.
