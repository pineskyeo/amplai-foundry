# amplai-foundry — Claude Code Adapter

공통 개발 규칙은 [AGENTS.md](AGENTS.md) 를 따른다. 이 파일은 Claude Code 에만 필요한 adapter
지침이다.

## Commands

개발 작업의 입구는 V3 `amplai` CLI 다. 이 저장소를 local product 의 app 으로 등록해 쓴다
(`deployment/local-container-app-amplai-foundry.json`, 등록 절차는
[docs/v3/USING_AMPLAI_WORK.ko.md](docs/v3/USING_AMPLAI_WORK.ko.md)).

```text
amplai work "<goal>"        목표를 내고 계약 초안을 받는다
amplai approve <goal-id>    계약을 승인한다. 검증된 결과만 draft PR 로 돌아온다
amplai status [<goal-id>]   계획·시도·검증·PR 을 본다
amplai steer <goal-id> ...  실행 중인 agent 에게 방향을 준다
amplai replan <goal-id> ... 실행 중인 목표의 계획을 바꾼다
amplai cancel <goal-id>     승인을 거두고 멈춘다
amplai design "<problem>"   구현 없이 설계 문서만 만든다
```

loop 밖 보조 skill 은 사용자가 직접 부른다 — `/grill-me`, `/eli12`, `/grilling`. 개발 절차를
지휘하지 않는다.

## Skill Layout

보조 skill 3개의 정본은 `.agents/skills/` 다. `.claude/skills/<name>` 은 같은 skill 을 가리키는
symlink 다. `skills-lock.json` 이 외부 출처를 고정한다.

## Communication

- 답변 언어는 한국어다. heading 은 English Title Case 를 쓴다.
- 단정 평서문으로 쓴다. 완곡·존대·부탁 표현을 쓰지 않는다.
- 기술용어는 영어 그대로 두고 한국어 어미만 붙인다.
- 코드·명령·로그·식별자는 번역하지 않는다.

## Verification

CI(`.github/workflows/ci.yml`)와 같은 네 가지가 통과해야 완료다.

```bash
.venv/bin/python -m pytest -n auto --dist worksteal
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
```
