# Deployment profiles

정본은 `design-reference/design/31_DEPLOYMENT_PROFILES.md` 와 `contracts/runtime-defaults.json` 이다.
구현은 `src/amplai_foundry/runtime/profiles.py` (`DeploymentProfile`, `support_matrix`).

- `support-evidence.json` — profile 별 실제 근거. `pass` 는 `evidence_refs` 가 있어야 `supported` 가 된다.
- `support-matrix.json` — 위 근거에서 생성한 matrix. 손으로 고치지 않는다.
  재생성: `.venv/bin/python -c "from amplai_foundry.runtime.profiles import write_matrix; import json; write_matrix('deployment/support-matrix.json', json.load(open('deployment/support-evidence.json')))"`

현재 `supported` 는 local-dev (이 host, Python 3.11.15) 뿐이다. onprem/hybrid/offline/legacy-app-client/managed-optional 은
credential·격리·실제 OS matrix 가 없어 `disabled` 다 (EXTERNAL_QUALIFICATION_PENDING). 활성화 전제조건 9개 중
placeholder 가 하나라도 남으면 write activation 은 HOLD 이고 read-only status 만 허용된다 (T-112).
