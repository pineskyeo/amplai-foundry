# ALR-008 Plan — 적용 순서와 결함 수정 설계

## Boundary

```text
바꾼다        pyproject.toml (ruff 예외 1줄)
              AGENTS.md (깨진 참조 1줄 + 보조 skill 목록)
              .agents/skills/feynman/, .claude/skills/feynman  — 삭제
              skills-lock.json, CLAUDE.md                      — feynman 항목 제거
              scripts/loopctl.py — UP032 2건 + feynman 세 곳
              src/amplai_foundry/control_plane/*  — 결함 넷 + lint/format/mypy
              tests/test_control_plane.py         — 회귀 test 추가 + lint
              docs/workstreams/messenger-governance-closure-v3/DECISIONS.md — D-056 append
              .ai-team/knowledge/decisions.index.json
              docs/workstreams/amplai-loop-runtime-adoption/{CURRENT_ITEM.md,KIT-DISTRIBUTION.md}

안 바꾼다     scripts/amplai_hosts.py 의 3.6 호환 문법 — kit 규약이다. 예외 목록으로 푼다
              tools/amplai-loop-kit/payload/*      — kit 봉인. 고치면 CHECKSUMS 가 깨진다
              .agents/skills/, .claude/skills/     — feynman 말고는 번들이 준 그대로 받는다
              src/amplai_foundry/{governance,curation,slack}/*  — 이 Work 밖이다
              vault/ canonical knowledge
```

## 브랜치와 적용 방식

`main` 에서 `alr-008-kit-2-4-platform-0-4` 를 딴다. 패치는 `git apply` 로 넣는다 —
`patch/apply_patch.py` 는 HEAD 가 `ced83af` 가 아니면 거부하고 (`apply_patch.py:44-47`),
`--repair-zip-baseline` 은 `git reset --hard` 를 검사보다 먼저 돌린다 (`:48-50`). **둘 다 안
쓴다.**

commit 은 slice 마다 나눈다. **패치 적용 commit 하나를 먼저 만들고 그 위에 수정을 얹는다** —
번들이 준 것과 우리가 고친 것을 diff 로 구분할 수 있어야 한다.

## 결함 수정 설계

### `CP-1` canonical_ref 를 origin 신원에서 파생시킨다

**문제.** `service.py:119` 가 content digest 로 PK 를 만드는데 중복 검사는 origin 기준이다.
두 축이 어긋나 같은 내용·다른 origin 이 PK 를 깬다. `tenant_id` 가 식별자에 없어 tenant 간
충돌도 난다.

**고침.** `cp_objects` 는 이미 `UNIQUE(tenant_id, project_id, kind, origin_store, origin_ref)`
를 갖는다 (`store.py:79`). canonical_ref 를 **그 tuple 에서** 파생시키면 충돌이 구조적으로
불가능해진다.

```python
origin_identity = canonical_json(
    {"t": tenant_id, "p": project_id, "k": kind, "s": origin_store, "r": origin_ref}
)
canonical_ref = f"{kind}:{project_id}:{sha256(origin_identity).hexdigest()[:24]}"
```

`content_digest` 는 응답과 row 에 그대로 남는다. 같은 origin 의 내용이 바뀌면 기존
`ORIGIN_REF_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT` 가 계속 잡는다 (`service.py:106-107`).

**근거.** 이 모델이 origin 기준임을 코드가 말한다 — `cp_objects` 한 row 가 origin 하나를
컬럼으로 갖고 (`store.py:74-77`), `get_reference` 가 `origin_ref` 를 reference 의 속성으로
돌려주며 (`service.py:262`), `tests/test_control_plane.py:127` 이
`ref.origin_ref == "CR-1-D1"` 을 단언한다. `src/` 와 test 전체를 grep 해 **canonical_ref 에서
내용을 되뽑는 코드가 없음**을 확인했다. 불투명 식별자다.

`tests/test_control_plane.py:238` 이 단언하는 `"evidence:project-a:"` 접두는 유지된다.

**기각한 대안.** content 주소 유지 + `cp_object_origins` alias table 신설. "같은 내용 = 같은
ref" 를 보존하지만 schema 변경과 두 번째 유일성 모델이 필요하다. 지금 계약이 요구하지 않는
성질이므로 보수적인 쪽을 고른다.

**추가 방어.** 그래도 `sqlite3.IntegrityError` 는 `CP-4` 에서 409 로 매핑한다. 앞으로 다른
경로에서 충돌이 나도 WSGI 계약을 깨지 않는다.

### 함께 — 인증 문맥 재조회를 없앤다

`_idempotent` 가 이미 `principal` 을 갖는다 (`service.py:43`). `operation` closure 에
`principal` 을 넘기고 `service.py:99-101`·`111-115`·`200-204` 의 raw_token 재조회를 지운다.
`AUTH_CONTEXT_LOST` 분기도 함께 사라진다. **raw_token 이 service 안쪽으로 흘러들지 않는다.**

### `CP-2` 숫자 필드를 경계 있는 정수로 강제한다

```python
def _bounded_int(value, *, default, minimum, maximum, field):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(field + "_INVALID")
    try:
        parsed = int(value)
    except ValueError:
        raise ValidationError(field + "_INVALID") from None
    if not minimum <= parsed <= maximum:
        raise ValidationError(field + "_OUT_OF_RANGE")
    return parsed
```

`bool` 을 먼저 배제한다 — `isinstance(True, int)` 가 참이다.

```text
limit         default 20, 1..500
max_attempts  default 3,  1..10
```

**호환성 주의.** 현재는 `int(request.get("limit") or 20)` 이라 `limit: 0` 이 falsy 로 20 이
된다. 새 함수는 `0` 을 `OUT_OF_RANGE` 로 거절한다. 의도된 교정이고, 구현 시 기존 test 가
`limit` 을 넘기는지 먼저 확인한다.

### `CP-3` 요청 body 에 상한을 둔다

```text
MAX_BODY_BYTES = 1 MiB
CONTENT_LENGTH 없음/빈 값   → {} 로 본다 (현행 유지)
정수 아님                   → 400  CONTENT_LENGTH_INVALID
음수                        → 400  CONTENT_LENGTH_INVALID
> MAX_BODY_BYTES            → 413  PAYLOAD_TOO_LARGE
읽은 바이트 < 선언 길이     → 400  REQUEST_BODY_TRUNCATED
```

`errors.py` 에 `PayloadTooLargeError` 를 더한다.

### `CP-4` 예외 분류를 닫는다

`http_api.py:36-48` 의 사슬 끝에 더한다.

```text
sqlite3.IntegrityError  → 409  STATE_CONFLICT
sqlite3.Error           → 503  STORE_UNAVAILABLE
Exception               → 500  {"error": "INTERNAL_ERROR"}
```

**catch-all 은 `str(error)` 를 응답에 넣지 않는다.** 내부 구조가 새 나가지 않게 고정 문자열만
돌려주고 원문은 stderr 로 남긴다. 순서상 `ControlPlaneError` 계열이 먼저 잡히므로 기존 코드
경로의 의미는 바뀌지 않는다.

## Slices

각 slice 는 독립으로 검증된다. **회귀 test 는 수정 전에 FAIL 하는 것을 실행으로 확인한 뒤
수정한다** — `D-054` 의 AC-005·AC-006 과 같은 방식이다.

### `S01` — 패치 적용과 kit lint 경계 복구

```text
1. 브랜치를 따고 git apply 로 번들 패치를 넣는다.  commit 하나 ("번들 그대로")
2. pyproject.toml extend-exclude 에 "scripts/amplai_hosts.py" 를 더한다.
   기존 넷과 같은 줄에 두고 이유 주석을 잇는다 (D-051, D-053)
3. AGENTS.md:135 의 agents/openai.yaml 을
   .agents/skills/<name>/agents/openai.yaml 로 고친다
```

검증: `ruff check .` 에서 `scripts/amplai_hosts.py` 11건이 사라진다.
`loopctl docs validate --repo` 가 `FRESH` 로 돌아온다.

### `S02` — `feynman` skill 제거

**패치 뒤에 한다.** 패치가 `scripts/loopctl.py` 의 `expected_skills`·`allowed_public` 영역과
`AGENTS.md` 의 보조 skill 줄을 함께 고치므로, 먼저 지우면 hunk 가 깨진다.

```text
1. git rm -r .agents/skills/feynman
2. git rm .claude/skills/feynman            (symlink)
3. scripts/loopctl.py 세 곳 — expected_skills, allowed_public, 그리고 그 위 주석에서
   feynman 을 뺀다.  주석은 "eli12/grilling" 만 남게 고친다
4. skills-lock.json 의 feynman 항목 삭제
5. CLAUDE.md / AGENTS.md 의 보조 skill 목록에서 뺀다
6. docs/workstreams/amplai-loop-runtime-adoption/{FACTS.md,PORT-LOG.md} 는 이력이다.
   지우지 않고 제거 사실을 덧붙인다
```

검증: `loopctl doctor` PASS (skill 집합·visibility·symlink 전부). `ls .claude/skills` 18개.
`grep -ri feynman` 이 이력 문서 둘만 남긴다. **`tests/` 는 skill 집합을 단언하지 않으므로
pytest 수가 변하지 않는다** — grep 으로 확인했다.

kit 은 skill 을 배포하지 않는다 (`manifest.json#owned_files` 23개 중 skill 0개). 대상 저장소는
영향받지 않는다.

### `S03` — control plane 을 저장소 표준에 맞춘다

```text
1. ruff check --fix (안전 수정 11건)
2. ruff format 을 대상 9개 파일에 적용
3. 남은 E501 27건을 줄바꿈
4. F841 2건 (outbox.py:96, worker.py:105) — 할당을 지우고 호출만 남긴다.
   _require_lease 는 guard 목적이므로 반환을 안 쓰는 것이 맞다
5. F401 1건 (auth.py:8 sqlite3) 삭제
6. mypy 2건 (outbox.py:133, worker.py:189) — 반환에 명시 cast
7. scripts/loopctl.py UP032 2건 — f-string 으로
```

**동작을 바꾸지 않는다.** 검증: `verifier --profile v2` PASS, `pytest` 수가 S01 과 같다.

### `S04` — `CP-1` 과 인증 문맥 정리

```text
1. 재현 test 를 먼저 쓴다 — 같은 payload 를 다른 origin_ref 로 두 번 publish.
   지금은 sqlite3.IntegrityError 로 터진다.  FAIL 을 실행으로 확인한다
2. tenant 간 test — 같은 project_id 문자열, 다른 tenant, 같은 payload
3. canonical_ref 파생을 origin identity 로 바꾼다
4. operation closure 에 principal 을 넘기고 raw_token 재조회 셋을 지운다
5. 기존 test 가 단언하는 접두 "evidence:project-a:" 가 유지되는지 확인
```

검증: 새 test 둘이 PASS, `tests/test_control_plane.py` 기존 12건 전부 PASS.

### `S05` — `CP-2`·`CP-3`·`CP-4`

```text
1. 재현 test 셋을 먼저 쓴다
   limit: [1]        → 지금 TypeError 로 밖으로 나간다
   CONTENT_LENGTH 큼 → 지금 그대로 read 한다
   handler 예외      → 지금 start_response 가 안 불린다
2. _bounded_int 도입, limit·max_attempts 에 적용
3. _body 에 상한과 길이 검증, PayloadTooLargeError 추가
4. __call__ 의 except 사슬을 닫는다
5. 모든 오류 경로가 start_response 를 호출하고 JSON 을 돌려주는 것을 단언한다
```

검증: 새 test 전부 PASS, `verifier --profile v2` PASS.

### `S06` — governance 와 문서

```text
1. DECISIONS.md 에 D-056 을 append (Platform 0.4.0 도입과 결함 수정 방침)
2. .ai-team/knowledge/decisions.index.json 갱신
3. CURRENT_ITEM.md / KIT-DISTRIBUTION.md 갱신 — kit 2.4.0, 남은 것 목록
4. loopctl docs impact / docs validate --repo
5. evidence-trace.jsonl 기록
```

검증: `docs validate --repo` FRESH, `loopctl trace verify`.

**`D-055` 승인 항목은 이 Work 가 쓰지 않는다.** `.ai-team/policy/approvals.jsonl` 은 사람이
넣는다.

## Failure path

```text
패치 적용이 실패한다           HEAD 가 움직였다는 뜻이다.  worktree 로 재확인하고 멈춘다
ruff 수정이 test 를 깬다       format 이 문자열 리터럴을 건드린 경우다.  해당 파일만 되돌린다
CP-1 수정이 기존 test 를 깬다  접두 단언(:238)이 근거다.  형식을 유지하는 쪽으로 되돌린다
catch-all 이 test 실패를 감춘다 catch-all 이 잡은 것을 stderr 로 반드시 남긴다.
                               test 는 500 INTERNAL_ERROR 를 명시로 단언한다
verifier 가 worktree 에서 127  .venv 가 없다.  본 저장소 .venv 를 symlink 한다
```

## Validation

```bash
# 전 slice 공통
.venv/bin/python .ai-team/verifiers/run.py --profile v2
.venv/bin/python scripts/loopctl.py doctor
.venv/bin/python scripts/loopctl.py docs validate --repo

# kit 무결성
.venv/bin/python tools/amplai-loop-kit/seal.py --verify
.venv/bin/python tools/amplai-loop-kit/selftest.py

# 결함 회귀
.venv/bin/python -m pytest tests/test_control_plane.py -q
```

**agent 세션 안에서도 한 번 돌린다.** kit 의 SessionStart hook 이 `AMPLAI_PROJECT_HOME` 을
심어 test 결과를 바꾼 전례가 있다 (`D-054`, kit 2.3.1). `env -u` 로 지운 것과 그대로 둔 것
둘 다 확인한다.

## CI

Actions 가 계정 결제로 멈춰 있다. 확인한 가장 최근 run `33281916935` 이 `steps=0` 으로 죽었고
annotation 은 `recent account payments have failed or your spending limit needs to be
increased` 다. **이 Work 는 CI 초록불을 얻을 수 없다.** merge 는 로컬 evidence 로 하되
`git_publish` gate 를 사람이 다시 연다 — `ALR-007` 과 같은 형태다. Actions 가 살아나면
`main` 에서 한 번 돌리는 것이 그다음 우선순위다.

## 다음 Work — `ALR-009` fleet 배포

`synapse`·`cortex` 로의 kit 2.4.0 배포는 이 Work 밖이다. 설계는
`specs/010-kit-2-4-fleet-distribution/` 이 갖는다. 이 Work 가 `main` 에 들어간 뒤 시작한다.

**우리 수정은 kit payload 를 건드리지 않는다.** `manifest.json#owned_files` 23개 중 우리가
고치는 것은 하나도 없다 — 결함 수정은 `src/amplai_foundry/control_plane/`, gate 수정은
`pyproject.toml`·`AGENTS.md`·`scripts/loopctl.py` 이고 `loopctl.py` 는 kit-owned 가 아니다.
따라서 배포될 kit 2.4.0 은 번들이 준 것과 byte 단위로 같고 `CHECKSUMS.sha256` 을 다시 만들 일이
없다. 그래도 foundry gate 가 초록이 된 뒤에 배포한다 — 검증되지 않은 정본을 퍼뜨리지 않는다.
