# C99 / RHEL Build Rule

## Rule

Cortex daemon과 plugin은 현재 Makefile의 C99 strict flags를 통과해야 한다. 개발 호스트에서
통과한 것만으로 RHEL7/8 또는 external 32-bit/TDE build 호환을 주장하지 않는다.

## Scope

- `src/**`, `include/**`
- `plugins/cortex-ate/**`, `plugins/cortex-production/**`
- `sdk/**` 중 C/C++ build artifact
- `Makefile`, `cortex.spec`, build scripts

## Required behavior

- implicit declaration, undeclared/unused variable, layer reverse dependency를 만들지 않는다.
- plugin은 `-fPIC -fvisibility=hidden`과 runtime symbol resolution 계약을 유지한다.
- main build의 POSIX feature macro/`-lrt`와 외부 TDE static library 환경을 구분한다.
- rename 후 이전 symbol/reference를 전수 검색한다.
- API 추가는 declaration, route/bootstrap registration, test/smoke를 한 변경으로 묶는다.

## Executable enforcement

- `layers`: `bash tools/ci/check_layers.sh`
- `build-test-ci`: `make test-ci`
- `rhel8`: `make rhel8`

`fast`는 빠른 invariant, `standard`는 schema/contract, `runtime`은 Loop Runtime 구조, `full`은 product build/test, `rhel`은 RHEL compatibility까지 판정한다.
실행하지 못한 platform check는 PASS가 아니라 `UNAVAILABLE`이다.
