# Evidence and Provenance

V2의 완료 판정은 설명이 아니라 재현 가능한 evidence chain을 사용한다.

```text
Work ID → Context Pack → changed files → verifier profile/result
        → repair attempts/escalation → review verdict → git checkpoint
```

Canonical evidence 자체는 code/test/log/commit/feature artifact에 남기며, 이 디렉터리는 schema와
정책만 소유한다.
