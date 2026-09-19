# tests/e2e

End-to-end fixtures and sealed artifacts. The executable cases live in `tests/v3/`
(`test_rc01_e2e_cross_app.py`, `test_rc01_e2e_render.py`) because the slice evals and the
conformance ledger scan `tests/v3/` for test-catalog ids.

- `cross_app/` — package marker for V3-057 (design/27 Scenario C/E).
- `artifacts/` — V3-059 golden registry (`golden-registry.json`, human-sealed) and its README.
