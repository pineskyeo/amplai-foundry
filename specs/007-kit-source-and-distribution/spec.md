# Kit 정본 이관과 배포 — 2.3.0

`ALR-006`. `D-053` 이 승인한 것을 실행 가능한 형태로 적는다. `specs/006-supervisor-ownership`
을 흡수한다.

## What

**kit 정본을 amplai-foundry 로 이관하고 공용 kit 2.3.0 을 만들어 세 앱에 배포한다.**

```text
tools/amplai-loop-kit/                  정본 (synapse main:1c05001b 에서 이관)
├── VERSION                             2.3.0
├── PROVENANCE.md                       신규 — 출처와 이관 경위
├── manifest.json, install.py, selftest.py
├── fragments/, payload/, reference/, examples/
├── payload/store/                       신규 — Store 에 설치될 supervisor (D-052)
│   └── supervisor/{run, VERSION}
└── distribution/
    └── targets.json                     신규 — 커밋. app_id·project_id·role·path_hint

.ai-team/local/kit-targets.json          신규 — host-local(gitignore). 실제 절대경로
scripts/kit_distribute.py                신규 — 배포 래퍼
```

배포 대상은 셋이다.

```text
amplai-foundry   role: source   자기 자신에도 설치한다
synapse          role: target   기존 2.2.0 설치를 2.3.0 으로 갱신. tools/ 사본은 삭제
cortex           role: target   신규 설치
```

## Why

**`install.py --target` 은 경로를 하나만 받는다** (`install.py:894`). 세 앱에 배포하려면
래퍼가 필요하고, 그 래퍼가 읽을 대상 목록이 있어야 한다.

**정본을 옮기는 이유는 kit 이 synapse 를 전제하고 만들어졌기 때문이다.** 실측했다 — 설치
로직은 중립인데(`manifest.json`·`install.py`·`fragments/*` 에 synapse **0건**) marker 대상
셋이 synapse 에만 있고 문서는 노골적으로 한 앱을 가리킨다.

| marker 대상 | required | amplai-foundry | synapse | cortex |
|---|---|---|---|---|
| `.agents/skills/handoff/SKILL.md` | false | 없음 | 있음 | 없음 |
| `.ai-team/skills/handoff/SKILL.md` | false | 없음 | 있음 | 없음 |
| `.ai-team/AUTONOMY_POLICY.md` | false | 없음 | 있음 | 없음 |

그리고 `D-053` 이 `D-052` 의 제약을 무효화했다. 정본을 가지면 **단일 인스턴스 락을 kit
안에 넣을 수 있고, `D-052` 가 한계로 인정한 우회 경로가 사라진다.**

## Constraints

- **경로를 커밋하지 않는다.** 절대경로는 host-local 파일에만 둔다. kit 자신이
  `apps/<id>.json`(커밋)과 `.amplai/local/apps/<id>.json`(host-local)을 나누는 것과 같은
  패턴이다
- **전체 dry-run 이 통과해야 실제 배포를 시작한다.** 하나라도 실패하면 아무것도 설치하지
  않는다
- **배포는 순차다.** 실패 지점에서 멈추고 무엇까지 됐는지 보고한다
- **`src/` 제품 코드와 `vault/` canonical knowledge 를 건드리지 않는다**
- **supervisor 를 켜지 않는다** (`--run`). `D-052`·`D-053` Scope 와 같다
- **synapse 사본 삭제는 그 저장소의 PR 이 하나 더 필요하다.** 이 feature 는 삭제 PR 의
  내용까지만 준비하고 머지는 사람이 한다

## Non-goals

- HANDOFF §3 의 개선 후보 1(worktree 인식)·2(알림)·3(활성 세션 라우팅) 설계
- Store 를 여러 개 운영하는 것 — 하나를 전제한다
- kit 을 외부에 배포하는 것 (PyPI, tarball 릴리스). 세 앱 사이 배포만 다룬다
- 테스트 fixture 의 앱 이름(`synapse`, `cortex`) 중립화 — cross-app 시나리오의 현실성을
  위해 그대로 둔다. 이것은 편향이 아니라 예시 데이터다

## Acceptance

| id | 내용 |
|---|---|
| AC-001 | `tools/amplai-loop-kit/` 가 synapse `main:1c05001b` 의 내용과 **파일 목록이 같고**, 바뀐 파일은 2.3.0 이 의도적으로 바꾼 것뿐이다. `PROVENANCE.md` 가 출처 commit 과 이관 일자를 적는다 |
| AC-002 | `selftest.py` 가 통과한다. `CHECKSUMS.sha256` 이 실제 파일 hash 와 일치한다 |
| AC-003 | 문서에서 synapse 전제가 제거됐다 — `README.md` 첫 줄, `reference/SYNAPSE_INTEGRATION.md`(이름 포함), `INSTALLATION.md`·`QUICKSTART` 의 예시. **테스트 fixture 는 제외**하고 센다 |
| AC-004 | marker 대상 중 특정 앱에만 있는 셋(`handoff` 둘, `AUTONOMY_POLICY.md`)이 **왜 optional 인지 manifest 나 문서에 적혀 있다.** 공용 kit 이 특정 앱 배치를 전제하지 않는다 |
| AC-005 | `install.py` 가 `<PROJECT_HOME>/supervisor/` 와 `.amplai/locks/` 를 설치한다. Store 가 없으면 이 단계를 건너뛰고 그 사실을 보고한다 |
| AC-006 | `amplai_supervisor.py` 가 **자기 안에서** `.amplai/locks/supervisor.lock` 을 잡는다. **두 번째 실행이 거부되고 exit code 로 구분된다.** 첫 번째를 SIGKILL 하면 락이 회수돼 다음 실행이 성공한다 |
| AC-007 | **우회 경로가 없다.** 앱 repo 의 `scripts/amplai_supervisor.py` 를 직접 실행해도 같은 락을 잡으므로 두 번째가 거부된다. `D-052` 가 한계로 적었던 것이 닫힌 것을 재현으로 확인한다 |
| AC-008 | `distribution/targets.json` 이 세 앱을 담고 절대경로를 담지 않는다. `.ai-team/local/kit-targets.json` 이 실제 경로를 담고 **`.gitignore` 에 걸려 있다** |
| AC-009 | `kit_distribute.py --dry-run` 이 세 대상 전부의 계획을 보이고 아무것도 안 바꾼다. **하나라도 실패하면 실제 배포가 시작되지 않는다** |
| AC-010 | `kit_distribute.py --all` 이 세 앱에 2.3.0 을 설치한다. 끝나면 각 대상의 `.ai-team/install/amplai-loop-kit.json` 이 2.3.0 이다 |
| AC-011 | 배포 중 한 대상이 실패하면 **그 지점에서 멈추고** 무엇까지 설치됐는지 보고한다. 이미 설치된 앱을 되돌리지는 않는다 (kit 이 앱별로 rollback 한다) |
| AC-012 | `kit_distribute.py --verify` 가 각 대상의 설치 버전을 kit `VERSION` 과 대조하고 어긋난 앱을 이름으로 보고한다 |
| AC-013 | synapse `tools/amplai-loop-kit/` 삭제 PR 의 내용이 준비돼 있고, **그 저장소에서 kit 을 참조하는 자리를 전수로 세어** 깨지는 것이 없음을 확인했다 |
| AC-014 | 세 앱 전부에서 설치 후 각자의 검증이 통과한다 — amplai-foundry 는 `doctor` + `verifier --profile v2`, 나머지는 각 저장소의 doctor |
| AC-015 | 제거 절차가 있다. `install.py --uninstall` 로 각 앱을 되돌리고 Store 의 `supervisor/` 를 지우면 원상이다. 복제본에서 확인한다 |

## Slices

```text
S01  정본 이관 + 공용화              AC-001~004        (2.3.0 준비, 배포 없음)
S02  supervisor 를 kit 에 통합       AC-005~007        (락을 kit 안으로)
S03  배포 설정 + 래퍼                AC-008, 009, 012  (dry-run 까지)
S04  실제 배포 + 검증                AC-010, 011, 014  (세 앱)
S05  synapse 정리 + 제거 절차        AC-013, 015
```

S01~S03 은 배포 없이 완결된다. S04 가 처음으로 다른 저장소를 바꾼다.

## Risks

- **다른 저장소를 바꾼다.** cortex 와 synapse 는 이 저장소의 verifier 밖이다. 각자의
  doctor 로 확인해야 하고(AC-014), 실패해도 이쪽 검증은 통과할 수 있다
- **synapse 는 이미 2.2.0 이 깔려 있다.** 2.3.0 이 갱신이 되고 marker·hook·policy 가 다시
  쓰인다. kit 의 idempotent update 가 동작하는지 그 앱에서 확인해야 한다
- **정본이 둘이 되는 기간이 있다.** 이관 뒤 synapse 삭제 PR 이 머지되기 전까지다.
  그동안 어느 쪽을 고쳐야 하는지 `PROVENANCE.md` 가 명시해야 한다
- **`D-051` 이 아직 미실행이다.** amplai-foundry 에 kit 이 안 깔려 있다. 이 feature 의
  S04 가 그 설치를 겸한다 — `specs/005` 와 겹치므로 아래에서 정리한다
- **테스트 수가 움직인다.** kit 의 `tests/ai/` 3파일이 설치되면 이 저장소 pytest 가
  1409 → 1409+N 이 된다. Package 5 review 기준선이 이동한다

## specs/005·006 과의 관계

```text
specs/005-amplai-loop-kit          kit 설치 (2.1.0 기준, D-051). 상태 ready, 미실행
specs/006-supervisor-ownership     supervisor 소유 (D-052). 상태 blocked
specs/007 (이 feature)             정본 이관 + 2.3.0 + 배포 (D-053)
```

- **006 은 superseded 다.** S01·S02 가 kit 2.3.0 기능으로 흡수됐다. `D-052` 의 실질은
  살아 있고 `D-053` 이 amend 했다
- **005 는 이 feature 에 흡수된다.** 005 는 2.1.0 을 amplai-foundry 에 설치하는 것인데,
  이 feature 의 S04 가 2.3.0 을 세 앱에 설치하므로 그것을 포함한다. `D-051` 은
  APPROVED 로 남고 **버전만 2.3.0 으로 올라간다** — `D-051` 이 정한 조건(제거 가능한
  층으로 설치, 규약 각주, handoff 두 층)은 그대로 지켜야 한다

## Source

- `D-053` — 이 feature 를 승인한 Decision
- `D-052` — supervisor 소유 (`D-053` 이 amend)
- `D-051` — kit 설치 조건 (유효. 버전만 2.3.0 으로)
- synapse `main:1c05001b` `tools/amplai-loop-kit/` — 이관 원본
- `HANDOFF_2026-08-28_amplai-loop-kit-2.2.0.md` — 2.2.0 의 수정 내역과 supervisor 과제
