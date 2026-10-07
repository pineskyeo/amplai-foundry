# AMPLAI Foundry

AMPLAI V3 는 자연어 목표를 운영자가 승인한 계약으로 바꾸고, 격리된 컨테이너에서 coding agent
(Codex, Claude, OpenCode)를 실행하고, 계약의 테스트로 독립 검증된 결과만 draft PR 로 돌려준다.
같은 저장소의 meta-harness 는 harness 부품을 버전으로 나누고, 실험과 사람 승인을 거친 변경만
배포한다.

Python package 는 `3.0.0.dev3`, wire schema 는 `3.0.0` 이다.

## Install

Python 3.11 이상이 필요하다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## Use

```bash
amplai work "<goal>"         # 계약 초안을 만든다
amplai approve <goal-id>     # 승인하면 실행하고, 검증되면 draft PR 을 연다
amplai status [<goal-id>]
amplai steer | replan | cancel
amplai design "<problem>"    # 설계 문서만, 구현 없음
amplai ops --help            # local-init, local-add-app, local-serve 등 운영 명령
amplai meta --help           # meta-harness gate
```

처음 준비(컨테이너 런타임, worker image, driver 자격 측정, `amplai ops local-init`)와 매일 쓰는
흐름은 [로컬 실행 사용법](docs/v3/USING_AMPLAI_WORK.ko.md) 에 있다.

## Layout

| Path | 내용 |
|---|---|
| `src/amplai_foundry/runtime/` | `amplai` CLI, local product server, 실행 loop, store, 계약 |
| `src/amplai_foundry/control_plane/api_v3/` | local API server 와 client |
| `src/amplai_foundry/agent_drivers/`, `sandbox/` | coding agent driver 와 컨테이너 sandbox |
| `src/amplai_foundry/verification/runtime/` | 독립 verifier |
| `src/amplai_foundry/evaluation/`, `meta_harness/` | 평가와 meta-harness |
| `packs/`, `deployment/` | domain pack, 컨테이너·verifier 구성 |
| `design-reference/` | V3 설계 정본 (frozen) |
| `docs/v3/` | 운영·설계 설명 |
| `specs/013-*` 이후 | Work 별 spec·plan·증거 |

## Docs

- [로컬 실행 사용법 (Mac, 단일 운영자)](docs/v3/USING_AMPLAI_WORK.ko.md)
- [DEV-02 Execution](docs/v3/DEV02_EXECUTION.ko.md)
- [DEV-03 Observatory & Meta-Harness](docs/v3/DEV03_OBSERVATORY_META.ko.md) · [Primary-source notes](docs/v3/DEV03_RESEARCH_SOURCES.md)
- [결정 기록](docs/workstreams/v3-real-execution/DECISIONS.md)

## Validation

```bash
python -m pytest -n auto --dist worksteal
ruff check .
ruff format --check .
mypy src
```

모든 기본 test 는 network, 브라우저, 컨테이너 없이 돈다. 실제 브라우저(`visual`)와 컨테이너
(`container`) test 는 marker 로 따로 고른다.
