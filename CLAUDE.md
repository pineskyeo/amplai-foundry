# amplai-foundry — Claude Code Adapter

공통 개발 규칙은 [AGENTS.md](AGENTS.md), Loop Runtime 은
[.ai-team/README.md](.ai-team/README.md) 를 따른다. 이 파일은 Claude Code 에만 필요한
adapter 지침이다.

## Commands

사용자-facing 개발 command 는 다음 둘뿐이다.

```text
/work <goal>
/design <problem>
```

나머지 개발 capability(`speckit-*`, `taskify`, `code-review`, `dev-loop`,
`systematic-debugging`)는 controller 가 내부에서 쓴다. 직접 부르지 않는다.

loop 밖 보조 skill 은 사용자가 직접 부른다 — `/grill-me`, `/feynman`, `/eli12`,
`/grilling`. 개발 절차를 지휘하지 않는다.

## Skill Layout

공통 정본은 `.agents/skills/`다. `.claude/skills/<name>`은 같은 workflow를 가리키는
symlink mirror이며 Claude 전용 사본을 따로 두지 않는다. skill 내용은 두 host에서 같고
호출 표기만 Claude `/skill`, Codex `$skill`로 다르다.

## Communication

- 답변 언어는 한국어다. heading 은 English Title Case 를 쓴다.
- 단정 평서문으로 쓴다. 완곡·존대·부탁 표현을 쓰지 않는다.
- 기술용어는 영어 그대로 두고 한국어 어미만 붙인다.
- 코드·명령·로그·식별자는 번역하지 않는다.

## Verification

```bash
python3 scripts/loopctl.py doctor
python3 .ai-team/verifiers/run.py --profile v2
scripts/eval.sh --feature specs/<feature> --slice S01
```

`amplai-foundry verify` 의 7 check 는 verifier registry 에 등록돼 있다. 그 registry 가
이제 단일 출처다.
