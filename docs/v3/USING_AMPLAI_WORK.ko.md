# AMPLAI 로컬 실행 사용법 (Mac, 단일 운영자)

목표 한 줄을 주면 AMPLAI 가 계약 초안을 만들고, 운영자가 승인하면 Codex 가 격리된 컨테이너에서
작업하고, 계약의 테스트로 검증된 결과만 draft PR 로 돌아온다 (Work 018, D-065..D-072).

## 한 번만 하는 준비

순서대로 실행한다. 경로는 amplai-foundry checkout 기준이다.

1. 컨테이너 런타임과 egress 를 올린다: `scripts/sandbox_up.sh`, `scripts/sandbox_up.sh --egress`
2. 대상 app 의 worker image 를 만든다: `scripts/app_image_up.sh <app> <repo 경로>`
   → `deployment/local-container-app-<app>.json`
3. Codex credential 의 scoped 사본을 **운영자가 직접** 만든다 (agent 는 credential 을 복사하지 않는다):
   `scripts/sandbox_up.sh --codex-home ~/.amplai-sandbox-probes/codex-home`
4. 그 image 에서 Codex 를 자격 측정한다 (image 가 바뀔 때마다 다시):

   ```bash
   .venv/bin/python scripts/container_qualify.py --driver codex \
     --codex-home ~/.amplai-sandbox-probes/codex-home \
     --container-profile deployment/local-container-app-<app>.json \
     --out specs/018-v3-real-execution/driver-qualification-<app>.json \
     --artifacts specs/018-v3-real-execution/artifacts
   ```

   9개 probe 가 모두 pass 가 아니면 serve 가 기동을 거부한다 (`DRIVER_UNQUALIFIED`).
5. 설정·키·운영자 token 을 만든다 (모두 0600):

   ```bash
   .venv/bin/amplai ops local-init --app <app> --repo <repo 경로> \
     --codex-home ~/.amplai-sandbox-probes/codex-home \
     --container-profile deployment/local-container-app-<app>.json \
     --qualification-report specs/018-v3-real-execution/driver-qualification-<app>.json \
     --verifier 'unit=python -m pytest -q -p no:cacheprovider tests/v3 | v3 unit tests' \
     --verifier 'lint=ruff check src tests | ruff lint'
   ```

   `--verifier` 는 계약의 acceptance 가 고를 수 있는 **유일한** 검증 명령 목록이다.

## 매번 쓰는 흐름

```bash
.venv/bin/amplai ops local-serve &            # API + 실행 루프 (loopback)
export AMPLAI_TOKEN_FILE=~/.amplai/local/operator.token
amplai work "로그인 실패 메시지를 한국어로 바꿔줘" --app <app>
```

`amplai work` 는 목표를 제출하고, Codex 가 base commit 의 사본을 **읽기만 해서** 만든 계약 초안
(objective, 범위, 하지 않을 것, acceptance 와 검증 명령, 위험도)을 보여준다. 질문이 있으면 계약을
만들지 않고 질문을 보여준다.

| 명령 | 하는 일 |
|---|---|
| `amplai approve <goal>` | 초안을 승인한다. 이 승인이 실행 권한과 draft PR 권한의 유일한 근거다 |
| `amplai status [<goal>]` | 상태, attempt 별 검증 결과, PR 링크 |
| `amplai cancel <goal>` | 승인을 철회하고 멈춘다. 실행 중 컨테이너는 다음 확인에서 멈춘다 |

승인 뒤 일어나는 일:

1. `main`(설정의 `base_branch`) commit 의 사본이 컨테이너에 들어간다. `.git`, 사용자 HOME, 실제 checkout 은
   들어가지 않는다. 인터넷은 egress allowlist 로만 나간다.
2. Codex 가 작업한다. 끝나면 host 가 변경을 patch 로 계산한다 (agent 가 쓴 파일을 믿지 않는다).
3. 새 사본에 patch 를 적용하고 네트워크 없는 컨테이너에서 acceptance 명령을 실행한다.
4. 실패하면 실패 출력과 함께 다시 시도한다. 첫 시도 포함 최대 3번, 승인부터 30분.
5. 모두 통과하면 `amplai/<goal>` branch 를 push 하고 draft PR 을 만든다. merge 는 사람이 한다.

## 보장과 한계

- 사용자 checkout(현재 branch, index, 커밋 안 한 파일)과 `main` 은 어떤 경우에도 쓰지 않는다. branch 는
  plumbing 으로 만든 commit 을 push 만 하고, 원격에 다른 commit 이 있으면 덮어쓰지 않는다.
- Codex credential 은 run 마다 새 home 에 넣었다가 끝나면 지운다. 갱신된 token 만 사본에 되돌려 쓴다.
- 검증 명령은 설치된 목록에서만 고른다. 모델은 verifier, 권한, 예산을 정할 수 없다.
- 한 번에 goal 하나만 실행한다. 회사(Windows) 환경, 원격 worker, 여러 사용자는 아직 지원하지 않는다.
- 취소·실패로 멈춘 goal 은 승인이 철회된 상태로 남는다. 다시 하려면 새 `amplai work` 를 연다.
