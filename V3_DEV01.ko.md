# AMPLAI V3 DEV-01 — Foundation & Runtime Core

이 소스는 **1단계 개발 스냅샷**이다. V3 전체 제품 릴리스·운영 승인을 의미하지 않는다.
패키지 버전은 `3.0.0.dev1`, wire/schema 버전은 설계 그대로 `3.0.0`이다.
기존 README의 Foundry 0.4 및 Kit 2.5 설명은 입력 원본의 release train을 설명한다.
이번에 기존 Kit 버전·서명·설치 이력을 V3로 위장해서 올리지 않았다.

## 실행

Python 3.11 이상을 사용한다. 이번 실제 검증 인터프리터는 전달 보고서를 기준으로 한다.
`pip install .` 후 다음 명령으로 개발 버전과 결정론적 참조 실행을 확인한다.

```bash
amplai ops version
amplai ops schemas
```

> 2026-10-07 legacy 정리(specs/033-harness-taxonomy/legacy-inventory.md D7)에서 `ops demo`·`ops execution-demo` 명령을 지웠다. 같은 경로는 `tests/v3/test_core_runtime.py`(`run_reference`)와 `tests/v3/test_dev02_execution.py`(`run_execution_reference`)가 실행한다.

`demo`는 비어 있는 새 디렉터리를 요구한다. 실제 JSON 파일을 생성하고 해시 기반
증거 수집·독립 검증·앱 간 전역 검증을 수행한다. LLM을 호출하거나 외부 계정을
검증하지 않는다. 결과는 출력 경로의 `reference-report.json`에 남는다.

`amplai work`와 `amplai design`은 인증된 Control Plane에 의도를 제출하는 진입점이다.
임의의 사내 시스템 접속 권한이나 실행 승인을 만들어 주지 않는다. 서버 배포,
실제 Claude/Codex/OpenCode 호출 및 운영환경 적합성은 DEV-02 이후 검증 대상이다.

## DEV-01에서 새로 보강한 핵심

- 목표 scope와 현재 Intent/Resolution을 고정하고 오래된 초안 재사용을 차단한다.
- 공백 목표·없는 지식 근거·앱 ID/alias 충돌을 차단하고 한국어 조사 경계를 인식한다.
- 검증 계획의 중복·누락·추가 기준, 근거 약화, 비독립 판정, 안전 기준의 주관적
  자기채점을 차단한다. mandatory governing context는 실제 앱·정책에서 도출한다.
- 그래프의 실제 계약 해시, 자원 claim, 대상 앱 권한, 검증 프로필, 산출물 슬롯을
  검증한다. 의존 그래프는 stable topological order로 정규화한다.
- 검증자는 작업자와 분리한다. 오래된 실행 epoch/attempt의 증거는 수락하지 않는다.
- 중복 JSON key 및 NaN/Infinity/overflow를 성공 결과로 인정하지 않는다.
- Critic의 수정은 복사본으로만 수행하며 최대 두 회차 후 미해결이면 HOLD한다.
- 임의 전략 입력을 거절한다. Direct/Loop/Deliberative/Discovery의 선택 원칙을 시험했다.
  외부 실행 전략의 전체 Driver·Sandbox 통합은 DEV-02에서 마무리한다.

## 보존 및 정리

체크포인트에서 빠진 `.claude/skills` 링크 18개를 원본에서 복원했다.
체크포인트가 수정해 기존 Kit checksum과 어긋났던 브라우저 시험 파일은 원본 bytes로
복원했다. root 환경에 맞춰 보안 시험을 느슨하게 하거나 검사값을 덮어쓰지 않았다.
캐시·로컬 가상환경·Git 내부 파일·글꼴 binary는 전달 소스에서 제외한다.
참조 중인 governance/legacy와 기존 skill을 섣불리 삭제하지 않았다. 그 정리 후보와
이관 조건은 전달 패키지의 `migration/RETIREMENT_REVIEW.json`에 있다.

## 시험과 범위

```bash
PYTHONPATH=src python -m pytest tests/v3
```

설계의 구조 예제 통과는 권한이나 제품 완료를 의미하지 않는다. 시험은 실제 실행된
항목만 합산하며, 초기 실패 로그는 수정 이후 통과 로그와 분리한다.
전체 legacy 회귀시험의 최초 실행은 시간 제한에 걸렸다. 별도 명시한 1단계 관련
회귀시험 집합의 완료 결과와 섞어 표시하지 않는다. 전체 regression·migration·
운영 전환·메타하네스 인수 기준은 후속 단계의 필수 종료 조건으로 남아 있다.
