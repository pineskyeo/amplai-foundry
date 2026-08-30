## D-056 — Adopt Platform 0.4.0 Control Plane With Its Reachable Defects Closed First

- Status: **PROPOSED**
- Date: 2026-08-30
- Work: `ALR-008`
- Decision: **Platform 0.4.0 control plane 을 이 저장소에 들이되, 재현되는 결함 넷을 닫고
  block gate 를 초록으로 만든 뒤에 들인다.** 번들을 그대로 받지 않는다.

  **(1) 도입 범위.** `src/amplai_foundry/control_plane/` 을 기존 Governance Store 와 분리된
  신규 bounded context 로 받는다. 기존 proposal/authority table 과 Loop Kit Project Store 를
  건드리지 않는다. `cp_*` table 은 이 저장소에 배포된 적이 없으므로 되돌릴 데이터가 없다.

  **(2) canonical_ref 는 origin 신원에서 파생한다.** 번들은 content digest 로 PK 를 만들고
  중복 검사는 origin 기준이라 두 축이 어긋난다. 같은 내용을 다른 `origin_ref` 로 발행하면
  `sqlite3.IntegrityError` 로 터지고, `tenant_id` 가 식별자에 없어 tenant 간에도 충돌한다.

  ```python
  origin_identity = canonical_json(
      {"t": tenant_id, "p": project_id, "k": kind, "s": origin_store, "r": origin_ref}
  )
  canonical_ref = f"{kind}:{project_id}:{sha256(origin_identity).hexdigest()[:24]}"
  ```

  `cp_objects` 가 이미 `UNIQUE(tenant_id, project_id, kind, origin_store, origin_ref)` 를
  가지므로 충돌이 구조적으로 불가능해진다. `content_digest` 는 무결성 값으로 남고 식별자가
  아니다. 형식 `{kind}:{project_id}:{24hex}` 는 유지한다.

  **(3) 신뢰 경계에서 입력을 강제한다.** 요청 JSON 의 숫자 필드는 경계 있는 정수로만 받는다
  (`limit` 1..500, `max_attempts` 1..10, `bool` 배제). 요청 body 에 1 MiB 상한을 두고
  `CONTENT_LENGTH` 의 음수·비정수를 거절한다.

  **(4) WSGI 는 어떤 예외에도 계약을 지킨다.** `sqlite3.IntegrityError` → 409,
  `sqlite3.Error` → 503, 나머지 → 500 `INTERNAL_ERROR`. **catch-all 은 예외 원문을 응답에
  넣지 않는다.** 원문은 stderr 로 남긴다.

  **(5) 인증 문맥은 principal 로만 흐른다.** 이미 인증된 `principal` 이 있는데 service
  operation 이 raw token digest 로 tenant 를 재조회하던 경로 셋을 없앤다. raw token 이
  service 안쪽으로 흐르지 않는다.

  **(6) kit 이 설치하는 script 는 lint 예외 목록에 함께 등록한다.** `D-051`·`D-053` 이 세운
  경계다 — kit 은 Python 3.6 호환을 지키고 이 저장소는 그것을 lint 대상에서 뺀다. kit 이
  설치 목록에 script 를 더할 때 `pyproject.toml` 의 예외도 같이 더한다. 배포 대상 두 곳은
  현재 `ruff`·`mypy` 를 쓰지 않으므로 이 결함은 foundry 한정으로 활성이고 그쪽에서는 잠복이다.

  **(7) fleet 배포는 `--project-home` 없이 `origin/main` worktree 에 한다.** `install.py:935-936`
  이 그 인자가 없으면 `configure_project_store` 를 즉시 반환하고, 그 결과
  `store.register_app(..., repo_path=self.target, ...)` (`install.py:953-955`) 이 안 불린다 —
  `ALR-007` 이 겪은 Store `repo_path` 덮어쓰기 사고의 원인이 그 줄이다. 건너뛰는 것은
  gitignore 된 host-local `.ai-team/local/project.json` 하나뿐이고 committed 산출물인
  `.ai-team/app.json` 은 영향받지 않는다. 따라서 **대상 저장소의 로컬 작업 사본을 동기화하거나
  정리하지 않고 배포한다.** Store 바인딩이 필요하면 배포와 분리해서 실제 체크아웃에서 한다.

- Reason: 번들의 `validation/` 은 seal·selftest·doctor·부분 pytest 만 기록했고 이 저장소의
  정본 gate 인 `.ai-team/verifiers/run.py --profile v2` 와 `ruff`·`mypy` 를 한 번도 돌리지
  않았다. 실측하니 block check 셋이 FAIL 이고 documentation freshness 가 STALE 이었다.
  control plane 의 결함 둘은 WSGI 를 통해 재현했고 둘 다 `start_response` 가 호출되지 않은 채
  예외가 밖으로 나갔다 — HTTP 계약을 깬 상태다. `D-053` 으로 이 저장소가 kit 정본이므로
  `scripts/amplai_hosts.py` 의 lint 경계를 여기서 닫지 않으면 fleet 셋이 같은 자리에서 깨진다.

- Consequence:
  - Platform version 이 `0.4.0`, Loop Kit 이 `2.4.0` 이 된다.
  - 결함 넷 각각에 회귀 test 를 붙이고, **수정 전에 FAIL 하는 것을 실행으로 확인한 뒤** 고친다.
  - `limit: 0` 이 기존에는 falsy 로 기본값 20 이 됐으나 이제 `OUT_OF_RANGE` 로 거절된다.
    의도된 교정이다.
  - CI 는 계정 결제로 멈춰 있어 이 Work 는 초록불을 얻을 수 없다. merge 는 로컬 evidence 로
    하고 `git_publish` gate 를 사람이 다시 연다 — `ALR-007` 과 같은 형태다.
  - 실제 Claude/Codex 계정 E2E 는 여전히 미수행이다. `auto_start` 는 계속 끈다.
  - `feynman` skill 을 제거한다. 사용자가 쓰지 않기로 했다. loop 밖 보조 skill 이라
    `D-046` 의 공개 표면 규칙(개발 controller 는 `work`/`design` 둘)을 건드리지 않는다.
    `loopctl doctor` 의 `expected_skills` 와 `allowed_public` 양쪽에서 빼고 `skills-lock.json`
    의 vendored 출처 기록(`neurofoo/agent-skills`)도 지운다. kit 은 skill 을 배포하지 않으므로
    대상 저장소는 영향받지 않는다.
  - fleet 배포는 별도 Work `ALR-009` (`specs/010-kit-2-4-fleet-distribution/`)로 뗀다.
    우리 수정은 `manifest.json#owned_files` 23개를 하나도 건드리지 않으므로 배포될 kit 은
    번들이 준 것과 byte 단위로 같고 `CHECKSUMS.sha256` 재생성이 없다.

- Rejected: **번들을 그대로 받고 결함을 열린 항목으로 기록.** block gate 셋이 깨진 채로
  `main` 에 들어가고, CI 가 복구되면 곧바로 빨간불이 된다. `PR #2` 의 41 commit 이 CI 로
  확인된 적 없는 상태에서 같은 부채를 하나 더 쌓는다.
- Rejected: **content 주소 유지 + `cp_object_origins` alias table 신설.** "같은 내용 = 같은
  ref" 를 보존하지만 schema 변경과 두 번째 유일성 모델이 필요하다. 현재 계약이 그 성질을
  요구하지 않는다 — `get_reference` 가 `origin_ref` 를 reference 의 속성으로 돌려주고
  `tests/test_control_plane.py:127` 이 그것을 단언한다. 보수적인 쪽을 고른다.
- Rejected: **kit 2.4.0 만 먼저 넣고 control plane 을 분리.** 번들이 단일 패치라 분리 비용이
  결함 수정 비용보다 크다. 결함 넷은 합쳐 한나절 규모다.
- Rejected: **`scripts/amplai_hosts.py` 의 3.6 문법을 py311 로 고치는 것.** kit 은 배포
  대상들과 문법을 맞춰야 한다 (`D-051`, `D-053`). 고치면 다음 설치가 되돌린다.
- Scope: Platform 0.4.0 도입과 그 결함 수정, kit 2.4.0 적용 시 lint 경계 복구다.
  token 만료 정책, `"*"` permission 모델, connector HMAC 검증 helper, 실제 host E2E,
  cortex·synapse 로의 2.4.0 배포는 포함하지 않는다.
