# Implementation Plan: AMPLAI V3 RC-01

**Branch**: `feat/013-amplai-v3` | **Date**: 2026-09-18 | **Spec**: `spec.md`

## Summary

DEV-03 snapshot (commit 8480cf8) 위에서 (1) 품질 게이트를 닫고 (2) `design-reference/implementation/tasks.yaml` 의 미착수 20 task 를 wave 의존 순서로 구현한다. 설계는 바꾸지 않는다.

## Technical Context

**Language/Version**: Python 3.11 (`.venv/bin/python`)
**Primary Dependencies**: pydantic 2, typer, jsonschema, cryptography (Ed25519), httpx, fastapi, uvicorn. optional `visual`: playwright, pillow
**Storage**: local SQLite WAL (`runtime/storage/store.py`) + CAS (`runtime/evidence/cas.py`)
**Testing**: pytest (`tests/v3`, `tests/e2e`), `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
**Target Platform**: 단일 control plane, 로컬 worker. 원격/컨테이너는 unqualified
**Constraints**: mypy strict, ruff E/F/I/UP/B/SIM/RUF, line 100. `design-reference/` 무변경
**Verifier profile**: v2

## Constitution Check

- `/work`, `/design` 외 public command 추가 없음 — V3-035 는 SpecKit 을 pack 으로 **내리는** 작업이지 새 entry 를 만드는 작업이 아니다
- Canonical Vault 무변경. V3-012 는 read/context adapter 만
- Kit (`tools/amplai-loop-kit`) 은 Python 3.6 호환 유지 (D-056). V3-050 은 payload 만 만진다
- 삭제는 report-only. V3-060 은 제안서만 낸다

## Slice Strategy

한 slice = 한 V3 task. 순서는 tasks.yaml DAG. 같은 wave 라도 `depends_on` 이 있으면 순차다.

| Slice | Task | Wave | 선행 (이 Work 안) | 주요 산출 경로 |
|---|---|---|---|---|
| S00 | GATE (Phase B) | — | — | pyproject.toml, src/** 타입 주석 |
| S01 | V3-007 | 1 | S00 | src/amplai_foundry/runtime/storage/ |
| S02 | V3-012 | 2 | S00 | src/amplai_foundry/knowledge_runtime/ |
| S03 | V3-013 | 2 | S02 | knowledge_runtime/readiness.py |
| S04 | V3-014 | 2 | S03 | knowledge_runtime/repo_facts.py |
| S05 | V3-034 | 3 | S00 | packs/, src/amplai_foundry/packs/ |
| S06 | V3-035 | 3 | S05 | tools/amplai-loop-kit/payload/.agents/skills/, packs/spec/ |
| S07 | V3-036 | 3 | S05 | packs/software/ |
| S08 | V3-037 | 3 | S05 | packs/frontend/ |
| S09 | V3-038 | 3 | S05 | packs/documents/ |
| S10 | V3-039 | 3 | S04, S05 | packs/research/, packs/ontology/, packs/semiconductor-dc/ |
| S11 | V3-049 | 3 | S00 | integrations/hermes/ |
| S12 | V3-050 | 4 | S05, S06 | tools/amplai-loop-kit/ |
| S13 | V3-051 | 4 | S01 | src/amplai_foundry/migration/ |
| S14 | V3-052 | 4 | S02, S03, S09, S12 | src/amplai_foundry/migration/archive/ |
| S15 | V3-054 | 4 | S12 | deployment/, tools/amplai-loop-kit/ |
| S16 | V3-057 | 4 | S15 | tests/e2e/cross_app/ |
| S17 | V3-059 | 4 | S08, S09, S15 | tests/e2e/artifacts/ |
| S18 | V3-060 | 5 | S12, S13, S14, S16, S17 | migration/, src/amplai_foundry/governance/ |
| S19 | V3-061 | 5 | S15~S18 | release/, eval/ |
| S20 | V3-062 | 5 | S11, S12, S13, S15, S19 | deployment/, release/ |

DEV-02/03 에서 이미 부분 구현된 선행 task (V3-001~006, 008~011, 015~033, 040~048, 053, 055, 056, 058) 는 이 Work 의 slice 가 아니다. 그것들의 "partial → fully accepted" 승격은 각 slice 가 의존하는 범위에서 test 를 추가하며 증거를 쌓고, V3-061 (S19) 에서 REQ/task/test 단위로 닫는다.

## Eval per Slice

각 slice 의 `Eval:` 은 task-manifest 의 required command 다. 기본형:

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/v3/test_rc01_<slice>.py
.venv/bin/python -m ruff check <changed paths>
.venv/bin/python -m mypy
```

전체 profile 은 `scripts/eval.sh --feature specs/013-amplai-v3 --full` (= v2).

## Project Structure

```text
specs/013-amplai-v3/
├── spec.md, plan.md, work-contract.json
├── knowledge-readiness.json, context-pack.json, environment.json
├── dev03-delivery-evidence.json
├── task-manifests/{index.yaml, AMPLAI-V3-T0xx.yaml}
├── tasks.md (generated)
├── evidence-trace.jsonl
└── DESIGN_CHANGE_PROPOSAL.md (필요 시)
```

## Risks

- **mypy 1216 건** — 대부분 주석 부재. 61 파일. 의미 변경 없이 닫아야 하며 slice 마다 477 test 로 회귀 확인
- **pack 엔진 범위** — `design/13 §3`: interface·validation profile conformance 가 acceptance 다. 실제 엔진(브라우저 렌더 등)은 available 할 때만 실행하고 unavailable 은 NOT PASS 로 남긴다
- **외부 자격 9 건** — 이 Work 로 닫을 수 없다. `disabled/unqualified` 경계 + human gate
- **Kit 3.6 호환** — V3-050/054 가 payload 를 바꾸면 `seal.py --verify` 와 3.6 문법 검사를 통과해야 한다
