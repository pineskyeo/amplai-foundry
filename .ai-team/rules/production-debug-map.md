# Production Debug Evidence Map

## Rule

production/lot/session/artifact 문제는 증상을 고정하고 evidence를 수집한 뒤 root cause를
확정한다. production data나 설비 상태를 조사 목적으로 임의 변경하지 않는다.

## Cortex evidence map

- log level: `TRCLOG`, `DBGLOG`, `INFOLOG`, `ERRLOG`; config `zlog.conf`
- production/session/runtime: `src/runtime/`, `plugins/cortex-production/`
- API: `src/api/`; smoke: `tools/ci/smoke_test.sh`
- artifact verify: `POST /api/v1/session/verify`, returned `artifacts_dir`
- fetch/config: `conf/fetch/`, `conf/user-domain.json`
- run artifact는 `/var/lib/cortex/artifacts/<run-id>/` 계열 operational evidence로 확인

## Diagnostic sequence

```text
symptom → reproducible signature → evidence path → first failing boundary
→ root cause → minimal repair → targeted verifier → regression verifier
```

repo에 없는 설비 사실은 추측하지 않는다. 필요한 production command나 deployment는
`production_operation` human gate로 올린다. secret, equipment identifier, raw customer data를
보고서에 그대로 복사하지 않는다.
