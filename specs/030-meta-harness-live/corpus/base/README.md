# amplai-demo-app

A small consumer of `amplai ops version` used as the second app of AMPLAI multi-app goals.

- `demo_app/version_report.py` — `summarize(info)` turns the version JSON into one line.
- `tests/` — unit tests (`python -m pytest -q tests`).
- `tests/integration/check_foundry_version.py` — run by AMPLAI's cross-app integration check
  against amplai-foundry's real `amplai ops version` output.
