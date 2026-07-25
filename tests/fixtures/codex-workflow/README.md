# Codex Workflow Fixtures

외부 LLM 없이 Curator classification 계약을 검토하는 sample이다.

| Fixture | Expected Operation |
|---|---|
| `01-new-concept.md` | `CREATE` |
| `02-concept-update.md` | `UPDATE` |
| `03-decision-conflict.md` | `CONFLICT` |
| `04-duplicate.md` | `IGNORE` 또는 `LINK` |
| `05-open-question.md` | `CREATE` (`kind=question`) |
| `06-mixed-long-response.md` | `SPLIT` 후 후보별 operation |
