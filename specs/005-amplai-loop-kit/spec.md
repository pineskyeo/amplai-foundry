# AMPLAI Loop Kit 2.1.0 도입

`ALR-004`. `D-051` 이 승인한 설치를 실행 가능한 형태로 적는다.

## What

synapse 판 **AMPLAI Loop Kit 2.1.0** 을 이 저장소에 **제거 가능한 층으로** 설치한다.
중앙 Project Store 를 만들고 앱을 등록해 실제로 쓰되, 언제든 흔적 없이 걷어낼 수 있는
상태를 유지한다.

kit 이 가져오는 것은 넷이다.

```text
Question → Evidence(DIRECT|LOCAL|PARALLEL) → Decision(AUTO|CHALLENGE|HUMAN)
중앙 Git Project Store   CR/Work/Contract/Decision/Evidence + event hash chain
Local Supervisor         claim, lease, heartbeat, recovery, app capacity
Claude Code -p worker    SessionStart/SessionEnd hook
```

## Why

**지금 쓰려는 것은 Decision authority 의 기계 강제다.** `async-policy.json` 이
`human_requires_approval_evidence`·`challenge_requires_independent_review`·
`done_requires_evidence` 를 강제한다. round 21 의 blocker `C21-3` 이 정확히 그 부류였다 —
`D-050` 이 CLI 출력 형식을 바꾸면서 `approvals.jsonl` 을 안 탔고, 사람도 기계도 못 잡았다.

**장기 방향과 어긋나지 않는다.** `ARC-0003` 이 이미 같은 구조를 설계했다.

> Hermes는 Work Manager 후보이고 Claude Code와 Codex는 Implementation Agent 역할을 수행할
> 수 있다.

kit 의 `Human → Hermes → Global AMPLAI → Project Store → App Runtimes` 가 그것이다. 잠시
쓰는 데 방향 위험이 없고, 나중에 이 저장소가 같은 층을 직접 만들 때 참조가 된다.

## Constraints

- **제거 가능성이 첫째 제약이다.** 어떤 변경도 `git checkout` + `rm` 으로 되돌아가야 한다.
  `pyproject.toml` 예외는 제거 대상 줄이 명확해야 하고, 규약 문서는 각주만 지우면 원문으로
  돌아가야 한다
- `src/amplai_foundry/` 제품 코드와 `vault/` canonical knowledge 를 건드리지 않는다
- `.ai-team/README.md` 의 Scope freeze 와 Directory ownership 을 **고치지 않는다.** 각주로
  예외만 표시한다 (`D-051` (3))
- `auto_start` 는 `false` 다. Supervisor 자동 실행은 이 feature 범위 밖이다
- Project Store 는 저장소 밖에 둔다. `/tmp` 를 쓰지 않는다
- kit marker(`<!-- AMPLAI-ASYNC-BEGIN/END -->`) 안쪽은 kit 소유다. 안쪽을 고치면 다음
  업데이트에서 충돌하므로 고치지 않는다

## Non-goals

- kit 의 cross-app 기능을 실제로 쓰는 것 — 등록된 앱이 하나뿐이라 지금은 효용이 없다
- `MGC-014` Hermes(제품 층 Client Partner) 구현 — kit 의 Hermes 와 다른 것이다
- 이 저장소의 `handoff.json` 을 Project Store 로 옮기는 것 — `D-051` (2) 가 두 층으로 나눴다
- kit 코드를 이 저장소 lint 규칙에 맞게 고치는 것 — upstream diff 를 유지한다
  (`loopctl.py`·`loopv2.py` 와 같은 처리)
- Package 5 round 22 의 blocker 를 닫는 것 — 별도 workstream 이다

## Acceptance

| id | 내용 |
|---|---|
| AC-001 | `.ai-team/AUTONOMY_POLICY.md` 가 존재하고 그 내용이 `policy.json` 의 `human_gates` 7개와 `forbidden_automatic_actions` 를 산문으로 푼 것이다. 지어낸 개념(Client Partner, G-번호 gate)이 없다 |
| AC-002 | `install.py --dry-run` 이 충돌 없이 27 action 을 낸다 |
| AC-003 | 설치 후 `loopctl.py doctor` 가 PASS 다 |
| AC-004 | 설치 후 `verifier --profile v2` 가 PASS 다. ruff 예외는 kit 4파일에만 걸리고 `src/`·`tests/` 는 strict 그대로다 |
| AC-005 | 설치 후 pytest 가 1425 collected 이고 실패 0이다. 기존 1409 중 어느 것도 안 깨진다 |
| AC-006 | Project Store 가 저장소 밖에 있고 `amplai.py project status` 가 앱 `amplai-foundry` 를 보여준다. `auto_start` 는 `false` 다 |
| AC-007 | `.ai-team/README.md` 에 Scope freeze·Directory ownership 예외 각주가 있고, 각주가 `D-051` 을 인용한다. 두 절의 본문은 무수정이다 |
| AC-008 | `/work` SKILL.md 의 절 번호 중복(`## 11.` 둘)이 문서에 기록돼 있다. marker 안쪽은 무수정이다 |
| AC-009 | **제거 절차가 문서에 확정돼 있고 복제본에서 실행해 `git status` 가 빈 것을 확인했다.** doctor·ruff·test 수가 설치 전과 같다 |
| AC-010 | Package 5 review 의 test 기준선 이동(1409 → 1425)이 `CURRENT_ITEM.md` 에 기록돼 다음 라운드가 안다 |

## Slices

```text
S01  AUTONOMY_POLICY.md 작성 + ruff 예외 + dry-run 확인      (설치 전 준비)
S02  Project Store 생성 + 설치 + 검증 기준선 갱신             (설치)
S03  규약 각주 + 제거 절차 확정 + review 기준선 기록          (기록)
```

각 Slice 는 독립 검증된다. S01 은 설치 없이 dry-run 으로, S02 는 doctor/verifier/pytest 로,
S03 은 복제본 제거 실행으로 확인한다.

## Risks

- **test 기준선 이동이 Package 5 review 와 겹친다.** round 22 가 열려 있으면 freeze target 과
  수치가 함께 움직인다. 순서를 정하지 않으면 review 가 혼란스러워진다
- **kit 재설치가 각주와 예외를 되돌릴 수 있다.** `install.py` 는 idempotent update 라
  marker 와 `.claude/settings.json` 을 다시 쓴다. 제거 절차가 그 사실을 담아야 한다
- **`## 11.` 중복은 marker 안쪽이라 고칠 수 없다.** 고치면 다음 업데이트에서 충돌한다.
  기록으로 남기는 것이 최선이다

## Source

- `D-051` — 이 feature 를 승인한 Decision
- `docs/workstreams/amplai-loop-runtime-adoption/KIT-2.1.0-EVALUATION.md` — 실측 평가
- `ARC-0003`, `ARC-0007` — 역할 경계와 Hermes 정의
- `.ai-team/README.md` — Scope freeze, Directory ownership
- `D-046` — cortex 판 loop runtime 이식
