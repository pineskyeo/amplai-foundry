# Provenance

이 kit 의 **정본은 amplai-foundry 다.** 여기서 고치고 여기서 배포한다.

```text
정본 위치     amplai-foundry:tools/amplai-loop-kit/
배포 대상     amplai-foundry, synapse, cortex
배포 명령     scripts/kit_distribute.py
```

## 출처

```text
원본          synapse:tools/amplai-loop-kit/
원본 commit   1c05001b7e82098de230b8e8b961ee8206ca7169
              ("fix(amplai): loop kit 2.2.0 — 락·CR id·데드락·설치 차단을 고친다", PR #81)
원본 버전     2.2.0
머지 시각     2026-08-28T04:29:44Z
이관 일자     2026-08-28
이관 근거     D-053
```

이관 시점에 파일 목록이 원본과 **정확히 같았다** — `git ls-tree` 와 `find` 를 대조해 차이
0을 확인했다. 그 뒤 이 파일과 `VERSION`(2.3.0)이 더해졌다.

## 정본이 둘인 기간 — 어느 쪽을 고치나

`D-053` 은 synapse 사본을 지우기로 정했지만 그것은 **그 저장소의 PR 이 따로 필요하다.**
삭제가 머지되기 전까지 같은 파일이 두 곳에 있다.

**그 기간에도 고치는 곳은 여기 하나다.** synapse 사본은 이미 배포 대상의 설치 산출물과
같은 지위이고 정본이 아니다. synapse 쪽을 고쳐야 하는 일이 생기면 여기서 고친 뒤 배포로
반영한다.

## 2.2.0 → 2.3.0 이 더하는 것

```text
supervisor 단일 인스턴스 락    amplai_supervisor.py 가 스스로 Store 락을 잡는다
Store supervisor 설치          install.py 가 <PROJECT_HOME>/supervisor/ 를 만든다
배포 대상 설정                 distribution/targets.json + host-local 경로 매핑
공용화                         특정 앱을 전제하던 문서를 중립으로 바꾼다
```

`CHANGELOG.md` 가 항목별 내용을 담는다.

## 상류 변경을 받을 때

synapse 나 다른 곳에서 이 kit 의 후속 판이 나오면 **덮어쓰지 않는다.** 이 파일에 그
출처와 commit 을 추가하고, 이쪽 변경(2.3.0 이 더한 것)과 충돌하는 지점을 먼저 나열한 뒤
합친다.
