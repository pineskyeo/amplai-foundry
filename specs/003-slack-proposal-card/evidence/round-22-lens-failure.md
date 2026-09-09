# Round 22 — Failure/Recovery Lens (MGC-012-P5)

**판정: PASS.** P0 0 / P1 0 / Blocking-P2 0 / Advisory 0.

target 착수와 종료에 `108 rows`, mismatch 0, aggregate
`34f02bdd64a090ffe2f847a07c6e829e96c16671d9b345f7005798c9aba11f71`을 확인했다.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  tests/test_cli.py tests/test_slack_ack_boundary.py
# 155 passed
```

non-positive limit, corrupt row, unreadable recovery, exhausted retry, expired final-attempt lease,
`dead_letter`, `recovery_hold`, exact-limit boundary와 int64 상한 경로를 검토했다. 실제 disk-full,
WAL corruption, multi-process stress는 이 bounded lens에서 실행하지 않았다.

human-ledger commit은 failure/recovery 결함이 아니므로 이 lens의 finding으로 중복 집계하지 않았다.
