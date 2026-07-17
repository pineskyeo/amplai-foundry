# Apply Report

## Approval

- Proposal: `PROP-20260714-122C8F66`
- Approved by: `user`
- Approved at: `2026-07-17T09:09:23+09:00`

## Applied Changes

- Created `ARC-0002` — Knowledge Representation Pipeline은 저장·검색·문맥 표현을 분리한다.
- Created `QUE-0005` — Scope 모델의 최소 공식 field는 무엇인가.
- Updated `MAP-0001` — Representation Pipeline section과 structured relations를 추가했다.

## Verification

- `python -m pytest` — 50 passed
- `ruff check .` — passed
- `ruff format --check .` — passed
- `mypy src` — passed
- `amplai-foundry schema check` — passed
- `amplai-foundry proposal validate .amplai/proposals/PROP-20260714-122C8F66/proposal.yaml` — passed
- `amplai-foundry lint vault/` — 0 errors, 0 warnings, 30 notes scanned

## Apply Commit

`36bd92f` — `feat: apply memory representation proposal`
