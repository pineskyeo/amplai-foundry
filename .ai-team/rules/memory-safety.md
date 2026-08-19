# C Memory Safety Rule

## Rule

Cortex는 장시간 실행되는 daemon이므로 ownership, cleanup, lifetime을 명시적으로 유지한다.

- `json_get_raw_by_jpath`의 heap 반환값은 반드시 `free()`한다.
- stub test가 stack memory를 반환하는 seam에서는 handler가 직접 free하지 않도록 구분한다.
- error path와 partial initialization에서도 같은 resource를 정확히 한 번 정리한다.
- dangling pointer, use-after-free, double-free, leak을 만들지 않는다.
- hot path에 불필요한 allocation/file I/O를 추가하지 않는다.

이 규칙은 일반적인 grep만으로 정확히 판정하기 어려우므로 changed diff, test, sanitizer 또는
동등한 evidence를 code-review에서 확인한다. 실행하지 않은 memory check를 PASS라 쓰지 않는다.
