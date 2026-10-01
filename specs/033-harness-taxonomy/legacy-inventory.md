# Legacy Inventory (OD-10)

- 작성일: 2026-10-01. 상태: evidence inventory 초안, operator 승인 대기. 이 문서는 아무것도 지우거나 옮기지 않는다.
- 범위: OD-10 — V1(dev-loop/speckit skill 시대)·V2(Loop Runtime, `.ai-team`, `scripts/loopctl.py`, `/work`·`/design` controller, docs cycle, supervisor/project store)와 V3 가 쓰지 않는 것을 지운다. V3 전용 개발에 반드시 필요한 것만 V3 로 옮긴다.
- 근거: scanner report 3개 + 이 문서 작성 중 grep 으로 직접 확인한 line. 직접 확인한 근거는 `file:line` 으로 적는다. scanner 만 주장하고 직접 확인하지 않은 근거는 **확인 필요** 로 표시한다.
- 고정: `design-reference/` 와 `src/amplai_foundry/runtime/contracts/data/` (frozen 3.0.0 schemas) 는 V3 이고 남는다.
- 한계: import 관계는 grep 과 scanner 의 AST closure 다. runtime trace 와 pytest 실행은 하지 않았다.

## 1. Summary

**지우는 것.** V2 Loop Runtime 전체(`scripts/loopctl.py`, `scripts/loopv2.py`, `scripts/amplai*.py` 6개, `scripts/kit_distribute.py`, `scripts/eval.sh`, `tools/amplai-loop-kit/`, `.ai-team/` 의 contracts·knowledge·rules·runtime·verifiers, `.specify/`), 그것을 부르는 session hook(`.claude/settings.json`, `.codex/hooks.json`), V1 skill(`dev-loop`, `speckit-*` 8개, `taskify`, `code-review`, `systematic-debugging`), 그 test(`tests/ai/` 18개, `tests/test_kit_distribute.py`), V2→V3 이행 도구(`src/amplai_foundry/migration/`, `distribution/closure.py`, `distribution/cutover.py`, `packs/conformance.py`, `packs/spec_surface.py`, `knowledge_runtime/foundry.py`, `knowledge_runtime/readiness.py`), meta-harness reference 구현(D-101, `specs/033-harness-taxonomy/plan.md:156-170`), V1/V2 시대 docs·specs(001-012).

**남는 것.** `amplai` entry(`pyproject.toml:42`)에서 닿는 V3 runtime 전부, Work 033 meta-harness(untracked 파일 포함), V3 운영 script(sandbox·qualify·corpus·evaluator_requalify), `packs/`, `deployment/`, `docs/v3/`, specs 013 이후.

**V3 로 옮기는 필수 부분(코드 결합을 끊는 작업).** V2 코드를 지우려면 V3 가 그 코드를 *실제로 쓰지 않아도* import 사슬 때문에 같이 load 하는 지점 4곳을 먼저 끊어야 한다. 이것을 끊지 않고 지우면 `amplai` CLI 자체가 ImportError 로 죽는다.

1. `control_plane/__init__.py:3-13` 이 V2 control plane 9개 module 을 eager import 한다. `runtime/cli.py:15`, `runtime/local_deployment.py:32`, `runtime/deployment.py:25` 가 `control_plane.api_v3.*` 를 import 하는 순간 이 `__init__` 이 실행되고, `knowledge_intake.py:11-24` 를 통해 `curation`, `ingestion`, `proposals`, 그리고 `curation/context_builder.py:8-10` 을 통해 `parsing`, `repositories`, `search` 까지 load 된다. `api_v3/server.py` 자체의 top-level import 는 V3 module 만 쓴다(`server.py:16-26`).
2. `runtime/deployment.py:27-29,36` 이 `governance.*` 와 `foundry_authority` 를 module level 에서 import 한다. V3 local product 는 이 파일에서 `private_bytes`, `read_key` 만 쓴다(`runtime/local_deployment.py:42`). `runtime/cli.py:54,441,505,568,868` 도 key helper 만 쓰고, `RuntimeDeployment` 는 `ops serve`(`runtime/cli.py:446-480`, `:460`)만 쓴다.
3. `verification/__init__.py:3` 이 V2 `verification/runner.py` 를 import 한다. V3 `verification.runtime.*` import 가 이것을 load 한다. `runner.py` 의 유일한 외부 사용자는 V2 CLI 다(`cli.py:55,659`).
4. `meta_harness/service.py:33` 의 `PROTECTED_PATHS` 가 `src/amplai_foundry/governance/` 를, `:36` 이 `runtime/effects/` 를 문자열로 갖는다. 삭제와 함께 목록을 고친다.

**operator 결정이 먼저 필요한 것.** `amplai-foundry` knowledge CLI 와 `vault/` (CI `ci.yml:27-29` 와 verifier registry 가 부른다), Slack/Hermes governance 층, `ops serve` server mode, loop 밖 helper skill, V3 안의 미도달 module. §6 에 D-1..D-12 로 정리한다.

**개발 입구 교체.** `/work`·`/design` (V2 skill) 은 V3 `amplai work`(`runtime/cli.py:78`)·`amplai design`(`runtime/cli.py:266`)로 바뀐다. `deployment/local-container-app-amplai-foundry.json` 이 git 에 있으므로 이 저장소 자체를 V3 app 으로 쓰는 구성이 존재한다. operator 의 `~/.amplai/local/local.json` 에 이 저장소가 app 으로 등록돼 있는지는 **확인 필요** 다.

## 2. Approval List

열 설명: Gen = generation. Evidence 의 "확인 필요" 는 scanner 주장만 있고 직접 확인하지 않은 근거다.

### 2.1 Delete

| # | Paths | Gen | Evidence (file:line) | Tests affected |
|---|---|---|---|---|
| X1 | `scripts/loopctl.py`, `scripts/loopv2.py` | V2 | CI `ci.yml:32`; registry id `loop-runtime-doctor` (`.ai-team/verifiers/registry.json`); `CLAUDE.md:38`; `AGENTS.md:96-103`; ruff ignore `pyproject.toml:104-105`. `src/` 참조 없음(scanner 1, grep) | `tests/ai/test_portable_baseline.py`, `tests/ai/test_document_*.py` |
| X2 | `scripts/eval.sh` | V1 | `CLAUDE.md:40`; `AGENTS.md:104`; `tests/ai/test_portable_baseline.py:85,244` (scanner 1). `src/`, `tests/v3` 참조 없음 | `tests/ai/test_portable_baseline.py` |
| X3 | `scripts/amplai.py`, `amplai_hook.py`, `amplai_hosts.py`, `amplai_runtime.py`, `amplai_supervisor.py`, `amplai_docs.py` | V2 | hook: `.claude/settings.json:8,19`, `.codex/hooks.json:8,19`; ruff exclude `pyproject.toml:84-85` (`amplai.py`, `amplai_hook.py`, `amplai_hosts.py`, `amplai_runtime.py`, `amplai_supervisor.py`); `src/` 는 `migration/v2.py:27` 주석뿐(scanner 1·3). `amplai_docs.py` 는 X9 결정 후 | `tests/ai/test_amplai_*.py` (5), `tests/ai/test_document_*.py` (12) |
| X4 | `.claude/settings.json` hooks, `.codex/hooks.json` hooks | V2 | `.claude/settings.json:3-24`, `.codex/hooks.json:3-24` 가 `scripts/amplai_hook.py` 를 SessionStart/SessionEnd 에 부른다. X3 와 **같은 commit** 에서 지운다 | 없음 |
| X5 | `scripts/kit_distribute.py`, `tools/amplai-loop-kit/` | V2 | CI `ci.yml:30-31`; ruff exclude `pyproject.toml:83`; registry id `kit-seal`. V3 installer 는 `distribution/installer.py` 이고 `runtime/cli.py:1064-1065` 에서 쓴다. 단 `tests/v3/test_rc01_kit_installer_v3.py:249-255` 가 실제 `tools/amplai-loop-kit/VERSION == 2.4.0` 을 단언한다 | `tests/test_kit_distribute.py`, `tests/ai/test_amplai_kit_*.py`, `tools/amplai-loop-kit/tests/*`, `tests/v3/test_rc01_kit_installer_v3.py::test_t097_*` (삭제 또는 synthetic kit 으로 재작성) |
| X6 | `.ai-team/` 중 `contracts/`, `knowledge/`, `rules/`, `runtime/`, `verifiers/`, `evidence/`, `install/`, `README.md`, `AUTONOMY_POLICY.md`, `app.json` | V2 | `CLAUDE.md:4,39`; `AGENTS.md:105,114-119`. `app.json` 을 읽는 `src/` 코드 없음(grep `app\.json` 결과 `installer.py` 의 target-app 경로 문자열뿐). `installer.py:31-53` 의 `.ai-team/*` 는 **설치 대상 app** 의 경로이고 이 저장소를 읽지 않는다 | `tests/ai/*` |
| X7 | `.specify/` | V1 | `scripts/loopctl.py`, `loopv2.py`, `amplai_docs.py`, `tests/ai/test_portable_baseline.py`, `tests/ai/test_document_integration.py` 만 참조(grep). registry id `loop-shell-syntax` | `tests/ai/*` |
| X8 | `.agents/skills/{dev-loop, code-review, systematic-debugging, taskify, speckit-* 8개}` 와 `.claude/skills/` 의 같은 이름 symlink | V1 | `.claude/skills/*` 는 `../../.agents/skills/*` symlink(ls). registry id `manifest-validator` 가 `.claude/skills/taskify/scripts/validate_task_manifest.py` 를 부른다. `packs/spec_surface.py:28-33` 가 이름을 갖지만 X13 에서 같이 지운다 | `tests/ai/test_portable_baseline.py`; `tests/v3/test_rc01_host_surface.py`, `test_rc01_pack_registry.py` 의 실제 저장소 의존 여부 **확인 필요** |
| X9 | `scripts/amplai_docs.py`, `*.amplai.json` sidecar 전부, `.ai-team/policy/documentation.json` | V2 | docs cycle. `verification/runtime/render_acceptance.py:186-188` 가 `documentation.json` 을 읽지만 파일이 없으면 `None` 을 반환한다(선택적 읽기). V3 의 별도 docs-review store(D-090) 존재는 scanner 1 주장 **확인 필요** | `tests/ai/test_document_*.py` (12), `tests/v3/test_rc01_e2e_render.py` (release lookup 경로 영향 **확인 필요**) |
| X10 | `src/amplai_foundry/migration/` (`v2.py`, `retirement.py`, `archive/`), `migration/retirement-proposal.json`, `migration/README.md` | V3-unused | `src/` 안에 `migration` importer 없음(scanner 3 grep). `runtime/evidence/archive.py` 의 `EvidenceArchive` 사용자는 `migration/archive/service.py` 와 `tests/v3/test_rc01_evidence_archive.py` 뿐(grep) | `tests/v3/test_rc01_legacy_retirement.py`, `test_rc01_v2_import.py`, `test_rc01_evidence_archive.py`, `test_rc02_followups.py:12` |
| X11 | `src/amplai_foundry/distribution/closure.py`, `distribution/cutover.py`, `scripts/rc01_closure.py` | V3-unused | `closure.py:102,119,144` 가 `.ai-team/policy/approvals.jsonl@HEAD` 를 읽는다(scanner 1, **확인 필요**). importer 는 test 와 `scripts/rc01_closure.py:14` 뿐(scanner 2) | `tests/v3/test_rc01_conformance_closure.py`, `test_rc01_cutover.py`, `test_rc02_followups.py:10` |
| X12 | `src/amplai_foundry/packs/conformance.py` | V3-unused | importer 는 `tests/v3/test_rc01_pack_*.py` 뿐(scanner 2, **확인 필요**) | `tests/v3/test_rc01_pack_software.py`, `_research_ontology.py`, `_frontend.py`, `_documents.py` (conformance 사용 부분만) |
| X13 | `src/amplai_foundry/packs/spec_surface.py` | V3-unused | 사용자는 `tests/v3/test_rc01_host_surface.py` 하나(grep). `:34` `GOVERNING_DOCS` 가 `.ai-team/README.md` 를 지명 | `tests/v3/test_rc01_host_surface.py` |
| X14 | `src/amplai_foundry/knowledge_runtime/foundry.py`, `knowledge_runtime/readiness.py` | V3-unused | `foundry.py:10-11` 가 V2 `intake.models`, `repositories.base` 를 import. V3 entry closure 에 없음(scanner 2·3). `service.py`, `repo_facts.py` 는 남는다 | `tests/v3/test_rc01_knowledge_adapters.py:99-176`, `test_rc01_readiness_validator.py` (scanner 2, **확인 필요**) |
| X15 | `src/amplai_foundry/meta_harness/reference.py`, `meta_harness/pipeline_reference.py`, `ops meta-demo`, `ops evolution-demo`, `meta_harness/service.py` 의 `demo-local` 예외, `runtime/execution/meta_local.py:144` 주석 | V3-unused | D-101 표 `specs/033-harness-taxonomy/plan.md:156-164`. caller 는 `runtime/cli.py:335-363` 와 서로뿐. plan 규칙: real evaluation semantics 를 단언하는 test 는 지우지 않고 real service fixture 로 옮긴다(`plan.md:166-168`) | plan.md:160 의 test 목록 + `tests/v3/test_rc02_followups.py:11` |
| X16 | `docs/SPECKIT-GRILLME-CHAIN.md`, `docs/SPECKIT-TASKIFY-BRIDGE.md`, `docs/PORTABLE-DEVELOPMENT.md`, `docs/platform/`, `docs/reviews/`, `docs/roadmaps/` (V2 proposal·history), `docs/workstreams/HANDOFF_2026-08-*.md`, `HANDOFF_2026-09-02.md`, `docs/workstreams/amplai-loop-runtime-adoption` | V1/V2 | 참조는 V2 docs policy 와 `tests/ai/*` 뿐(scanner 3). `docs/ROADMAP.md`, `docs/VISION.md` 가 V3 정책을 담는지는 **확인 필요** → D-10 | `tests/ai/test_document_context_delivery.py`, `test_portable_baseline.py` |
| X17 | `specs/001`..`specs/011` (012 는 M5 참고) | V1/V2 | test/src 참조는 `tests/test_slack_ack_boundary.py:1861` (specs/003), registry `manifest-validator` (specs/003) 뿐(scanner 3, **확인 필요**). git history 에 남는다 | `tests/test_slack_ack_boundary.py` (D-2 에 따름) |

### 2.2 Migrate Essential Part

| # | Paths | Gen | 옮길 것 / 끊을 것 | Evidence (file:line) | Tests affected |
|---|---|---|---|---|---|
| M1 | `src/amplai_foundry/control_plane/__init__.py` | V2 boundary | `__init__` 의 eager import 를 비우거나 `api_v3/` 를 V3 package 로 옮긴다. 이것이 V2 control plane·governance 삭제의 전제다 | `control_plane/__init__.py:3-13`; `knowledge_intake.py:10-24`; `curation/context_builder.py:8-10`; `api_v3/server.py:16-26` (V3 import 만) | 전체 `tests/v3` (import 경로가 바뀌면) |
| M2 | `src/amplai_foundry/runtime/deployment.py` | V3-in-use | `private_bytes`, `read_key`, `generate_key` 를 governance 비의존 module 로 옮긴다. `RuntimeDeployment`·`FoundryAuthorityBridge` 는 D-3 결정에 따른다 | `deployment.py:25-29,36,137`; `local_deployment.py:42`; `runtime/cli.py:54,441,460,505,568,868`; `api_v3/server.py:194,703,710` (lazy, optional `authority_bridge`) | `tests/v3` 중 `deployment` 를 쓰는 file. `RuntimeDeployment` 를 직접 import 하는 `tests/v3` file 은 grep 에서 찾지 못했다 |
| M3 | `src/amplai_foundry/verification/__init__.py`, `verification/runner.py` | V2 | `__init__.py:3` 의 `runner` import 를 지우고 `runner.py` 를 지운다(`amplai-foundry verify` 와 함께, D-1) | `verification/__init__.py:3`; 사용자 `cli.py:55,659` 만 | 없음(grep 상 test importer 없음) |
| M4 | `src/amplai_foundry/meta_harness/service.py` `PROTECTED_PATHS` | V3-in-use | 지운 경로(`governance/`, 그리고 D-7 에서 지우면 `runtime/effects/`)를 목록에서 뺀다 | `meta_harness/service.py:32-40` | `tests/v3/test_033_s*` 중 protected path 단언 **확인 필요** |
| M5 | `specs/012-portable-document-lifecycle/html-*-r6`, `review-r6.json` | V2 data | V3 test fixture 로 쓰인다. `tests/fixtures/` 로 옮기거나 render test 를 재작성한다 | `tests/v3/test_rc01_e2e_render.py:24-25`; `tests/e2e/artifacts/golden-registry.json:4-11` (reader 는 `test_rc01_e2e_render.py` 뿐, grep) | `tests/v3/test_rc01_e2e_render.py` |
| M6 | `contracts/` (root) | V3 duplicate | `src/amplai_foundry/runtime/contracts/data` 와 byte 동일(`diff -rq` 무출력). test 경로를 src 쪽으로 바꾸고 root 사본을 지운다 | `tests/v3/test_core_runtime.py:56` | `tests/v3/test_core_runtime.py` |
| M7 | `.github/workflows/ci.yml` | shared | `:30-32` 삭제. `:27-29` 는 D-1. `:23-26` (pytest, ruff, mypy) 유지 | `ci.yml:23-32` | 전체 |
| M8 | `pyproject.toml` | shared | ruff exclude `:77` (`.agents`, `.ai-team`, `.claude`, `.specify`), `:83-86` (kit, `scripts/amplai*.py`, `tests/ai`), per-file-ignores `:104-105` 정리. `:41` `amplai-foundry` entry 와 `:63-66` `pythonpath` 는 D-1·D-2 에 따른다 | `pyproject.toml:41,63-66,76-86,104-105` | lint 대상 변화 |
| M9 | `CLAUDE.md`, `AGENTS.md`, `README.md` | shared | V3 명령으로 다시 쓴다(§3.2). `.amplai.json` sidecar 는 X9 와 함께 지운다 | `CLAUDE.md:4,12-13,38-40`; `AGENTS.md:24,32-34,62-105,114-122,128,136` | 없음 |
| M10 | `release/`, `eval/test-catalog-status.json` | V3 data | closure test(X11)가 지워지면 `release/rc01-conformance-closure.json` 의 남은 reader 를 다시 확인한다. `eval/test-catalog-status.json` 은 `tests/v3/test_rc01_conformance_closure.py:113` 만 읽는다(scanner 1, **확인 필요**) → X11 과 같이 지울 후보 | scanner 1 | `tests/v3/test_rc01_conformance_closure.py` |

### 2.3 Keep

| # | Paths | Gen | Evidence (file:line) |
|---|---|---|---|
| K1 | `src/amplai_foundry/runtime/` (`cli.py`, `meta_cli.py`, `local_deployment.py`, `execution/`, `goals/`, `graphs/`, `budgets/`, `recovery/`, `storage/`, `evidence/cas.py`, `contracts/` 중 `foundry_authority.py` 제외), `control_plane/api_v3/`, `agent_drivers/` (`codex_app_server.py` 제외), `sandbox/`, `evaluation/`, `knowledge_runtime/{service,repo_facts}.py`, `distribution/{installer,packs}.py`, `packs/catalog.py`, `verification/runtime/{service,design_check,integration,patch_commands}.py`, `_vendor/`, `domain/{identity,enums,source}.py` | V3 | `pyproject.toml:42`; `runtime/cli.py:15-18,1064-1065`; `local_deployment.py:31-42`; `knowledge_runtime/service.py:10` 이 `domain.enums` 를, `domain/__init__.py:3` 이 `domain.source` 를 load 한다 |
| K2 | `src/amplai_foundry/evaluation/quality.py` | V3 | scanner 2 는 "src importer 없음" 이라 했으나 틀렸다: `runtime/meta_commands/evaluator.py:27` 가 import 한다 |
| K3 | `runtime/meta_commands/*`, `meta_harness/*` (X15 제외), `deployment/launchd/` (untracked) | V3 | `meta_cli.py:245-247` → `meta_commands/__init__.py:15-16` (pkgutil 동적 등록) |
| K4 | `src/amplai_foundry/runtime/reference.py` (`ReferenceDeployment`), `runtime/execution/reference.py` | V3 | `tests/v3/conftest.py:5,9-10` 의 기본 `deployment` fixture; `scripts/container_qualify.py:36,600`; `scripts/opencode_qualify.py:43,857`. scanner 2 의 "decide" 를 keep 으로 확정. `ops demo`/`ops execution-demo` 명령만 D-8 |
| K5 | `src/amplai_foundry/verification/runtime/render_acceptance.py` | V3 | `tests/v3/test_033_s15_dashboard.py:67` (Work 033 S15) 가 `inspect_page` 를 import 한다. `.ai-team` 읽기는 선택적(`:186-188`) |
| K6 | `scripts/sandbox_up.sh`, `app_image_up.sh`, `container_qualify.py`, `opencode_qualify.py`, `requalify_drivers.py`, `corpus_base_repo.py`, `corpus_check.py`, `evaluator_requalify.py`, `tb2_adapter.py` | V3 | `docs/v3/USING_AMPLAI_WORK.ko.md:10-18`; `evaluation/quality.py:243-252` 가 `scripts/evaluator_requalify.py` 를 module 로 load; `meta_harness/tb2.py:448,537`; `pyproject.toml:61`. `requalify_drivers.py`, `corpus_check.py`, `tb2_adapter.py` 근거는 scanner 1·2 (**확인 필요**) |
| K7 | `packs/`, `deployment/`, `docs/v3/`, `docs/workstreams/v3-real-execution/`, `specs/013`..`specs/033`, `design-reference/`, `src/amplai_foundry/runtime/contracts/data/` | V3 | `pyproject.toml:48-49`; `deployment/local-container-app-amplai-foundry.json` (git tracked) |
| K8 | `runtime/execution/strategies.py:18,77`, `sandbox/local.py:26`, `distribution/installer.py:31-53` 의 `.ai-team`·`.agents`·`AGENTS.md`·`CLAUDE.md` 문자열 | V3 | **설치 대상 app / sandbox workspace** 의 보호 경로다. 이 저장소의 `.ai-team` 삭제와 무관하다 |
| K9 | `.agents/skills/work`, `.agents/skills/design` | V2 | X8 와 같이 지우지 않고 D-5 가 확정될 때까지 둔다(현재 유일한 사용자-facing 개발 입구, `CLAUDE.md:12-13`) |

### 2.4 Decide

| # | Paths | Gen | 왜 operator 결정인가 | Evidence (file:line) | Tests affected |
|---|---|---|---|---|---|
| D1 | `amplai-foundry` CLI: `src/amplai_foundry/cli.py`, `schema.py`, `lint/`, `intake/`, `projects/`, `roadmaps/`, `semantics/`, `reporting/`, `parsing/`, `repositories/`, `search/`, `ingestion/`, `curation/`, `proposals/`, `domain/{lifecycle,models,project,semantic}.py` + 자산 `vault/`, `schemas/`, `templates/`, `plans/`, `.amplai/` (repo 안, project.yaml·proposals), knowledge docs (`docs/KNOWLEDGE-*.md`, `LIFECYCLE.md`, `RELATIONSHIPS.md` 등 13개) | V2 시대 knowledge product | V3 `amplai` 는 이 CLI 를 부르지 않는다(`runtime/cli.py:15-18`). 그러나 CI(`ci.yml:27-29`)와 registry(`schema`, `vault-lint`, `project-pack`)와 `AGENTS.md:24,32-34` 가 부른다. `cli.py:86-89` 가 V3 ops 를 `amplai-foundry v3` 로도 mount 한다. 코드 결합은 M1 로 끊긴다 | `pyproject.toml:41`; `cli.py:19-55,86-89`; `schema.py:10-13`; `curation/context_builder.py:13-15` (docs 3개 런타임 읽기, scanner 3) | `tests/test_cli.py`, `test_lint.py`, `test_curation.py`, `test_ingestion.py`, `test_search.py`, `test_markdown_parser.py`, `test_proposals.py`, `test_proposal_hardening.py`, `test_repository.py`, `test_domain_models.py`, `test_golden_memory_contract.py`, `test_intake_e2e.py`, `test_project_identity.py`, `test_project_packs.py`, `test_roadmap_changes.py`, `test_semantic_comparison.py`, `tests/v3/test_rc02_graph.py` (schemas 의존, scanner 1 **확인 필요**), fixtures |
| D2 | Slack/Hermes governance: `src/amplai_foundry/governance/`, `control_plane/` (api_v3 제외), `integrations/hermes/`, `integrations/hermes-amplai/`, `scripts/amplai_orchestration_bridge.py`, `scripts/amplai_work_status_projection.py`, `docs/MESSENGER-PROPOSAL-CONTROL.md`, `docs/workstreams/messenger-governance-closure-v3` | V2 | V3 local product 는 governance symbol 을 쓰지 않는다. 결합은 M1(`__init__`)·M2(`deployment.py`)·`foundry_authority.py:15-22`·`server.py:703` 뿐이다. 그러나 Slack/Hermes 기능 자체를 V3 에서 다시 만들 계획인지는 operator 만 안다. `cli.py:920,972,1054,1140` 가 bridge script 를 module 이름으로 lazy import 한다 | 위 line; `tests/test_hermes_integration.py:7-14` | `tests/test_slack_*.py` (5), `test_governance_*.py` (3), `test_apply_jobs.py`, `test_authority.py`, `test_decisions.py`, `test_definition_object_store.py`, `test_active_proposals.py`, `test_git_publish.py`, `test_review_cards.py`, `test_work_activation.py`, `test_work_status_projection.py`, `test_ingress.py`, `test_control_plane.py`, `test_orchestration_bridge.py`, `test_legacy_migration.py`, `test_hermes_integration.py`, `test_direct_mutation_architecture.py` (`:9-17`, governance·intake import + `src` 전체 scan), `tests/v3/test_rc01_hermes_adapter.py:9`, `tests/v3/test_rc02_followups.py:9` |
| D3 | `ops serve` server mode: `RuntimeDeployment`, `runtime/contracts/foundry_authority.py`, `api_v3/server.py` 의 `authority_bridge` route | V3-in-use (server) | operator 가 `ops serve` 를 쓰는지 모른다. 지우면 governance 결합이 완전히 사라진다. 남기면 governance 의 authority·store·object_store·definitions·models·review_cards 를 V3 로 옮겨야 한다 | `runtime/cli.py:446-480`; `deployment.py:137`; `foundry_authority.py:15-22`; `server.py:194,703-710` | **확인 필요** (직접 import 하는 tests/v3 file 은 grep 에서 없음) |
| D4 | `.agents/skills/{eli12, grill-me, grilling}`, `skills-lock.json` | shared | 개발 절차를 지휘하지 않는 helper(`CLAUDE.md` Commands). OD-10 의 "V3 가 쓰지 않는 것" 에 해당하나 개발 도구가 아니다 | `CLAUDE.md` Commands; `packs/spec_surface.py:34-40` (X13 과 함께 사라짐) | `tests/v3/test_rc01_legacy_retirement.py` (X10 과 함께 사라짐) |
| D5 | `.agents/skills/{work, design}` 와 symlink, `.claude/skills/` 디렉터리 | V2 | V3 `amplai work`/`amplai design` 으로 바꾸는 시점. 이 저장소를 V3 app 으로 등록하는 운영 확인이 먼저다 | `CLAUDE.md:12-13`; `runtime/cli.py:78,266`; `deployment/local-container-app-amplai-foundry.json` | 없음 |
| D6 | V3 안의 미도달 module: `runtime/effects/`, `tool_broker/`, `runtime/profiles.py`, `runtime/evidence/archive.py`, `runtime/execution/reuse.py`, `verification/runtime/commands.py`, `verification/runtime/visual.py`, `agent_drivers/codex_app_server.py` | V3-unused | `amplai` entry 에서 static 경로 없음(scanner 2 AST closure). 사용자는 test 뿐(grep). `visual.py` 는 `render_acceptance.py:219` 가 lazy import 한다(직접 확인) → K5 가 남으므로 `visual.py` 는 지우지 않거나 그 분기를 같이 지운다. `codex_app_server_default_enabled` 가 `runtime-defaults.json:23` (frozen data)에 있다 | grep 결과 위 표 | `tests/v3/test_rc02_effects.py`, `test_dev02_tool_broker.py`, `test_rc02_boundary.py`, `test_rc01_deployment_profiles.py`, `test_rc02_graph.py`, `test_rc02_driver.py` |
| D7 | `ops demo`, `ops execution-demo` 명령 | V3 | caller 는 CLI 뿐. 필요 근거 없음. `ReferenceDeployment` 자체는 K4 로 남는다 | `runtime/cli.py:305-334` | **확인 필요** |
| D8 | V3 probe script: `broker_probe.py`, `opencode_auth_probe.py`, `opencode_uid_probe.py`, `egress_qualify.py`, `limits_qualify.py`, `opencode_container_up.sh`, `meta_smoke.py` | V3 | src/tests 참조 없음(scanner 1). specs·release 기록이 인용한다. 일회성 측정 도구인지 재측정 절차인지 operator 판단 | scanner 1 (**확인 필요**) | 없음 |
| D9 | Kit 2.x downstream fleet 배포 중단 | V2 | X5 삭제는 downstream repo 가 아직 Kit 2.x 를 받는지에 달렸다 | scanner 1 (**확인 필요**) | — |
| D10 | 기록 문서: `docs/workstreams/*/DECISIONS.md`, `docs/ROADMAP.md`, `docs/VISION.md`, `docs/VERSIONING.md`, `docs/CODEX-CURATION-WORKFLOW.md`, `V3_DEV01.ko.md`, `harnesses/codex-knowledge-curator.md` | mixed | 결정 기록 보존 vs 삭제(git history 에는 남는다). `V3_DEV01.ko.md`, `harnesses/` 는 참조 없음(grep) | `AGENTS.md:121-122` (Decision 기록 위치) | `tests/ai/*` 만 |
| D11 | `.ai-team/policy/` (`approvals.jsonl` 등) | V2 | 승인 ledger 이력. X11 이 지워지면 reader 가 사라진다. 보존 위치를 정해야 한다 | `distribution/closure.py:102,119,144` (scanner 1, **확인 필요**) | `tests/v3/test_rc01_conformance_closure.py:148-153` (synthetic, X11 과 함께) |
| D12 | `tests/fixtures/codex-workflow`, `gpt-response.md` 등 V2 CLI fixture | V2 | D1 에 종속 | scanner 3 | D1 test |

## 3. V2 Developer Capability Replacement

### 3.1 Capability Map

| V2 capability | V2 근거 | V3-only 대체 | 근거 |
|---|---|---|---|
| `/work <goal>` (skill controller) | `CLAUDE.md:12`; `AGENTS.md:62,74` | `amplai work` → `amplai approve` / `status` / `steer` / `replan` / `cancel`, 결과는 draft PR | `runtime/cli.py:78,223,229,238,246,254`; `docs/v3/USING_AMPLAI_WORK.ko.md:3-4` |
| `/design <problem>` | `CLAUDE.md:13` | `amplai design` | `runtime/cli.py:266` |
| 개발 대상 등록 | (없음, 저장소 자체) | `amplai ops local-init`, `ops local-add-app`, `ops local-driver`, `ops local-verifier` | `runtime/cli.py:481,640,696,760`; 이 저장소용 container profile `deployment/local-container-app-amplai-foundry.json`. operator config 등록 여부 **확인 필요** |
| `loopctl contract/readiness/context validate` | `AGENTS.md:98-100` | 계약은 `amplai work` 가 초안을 만들고 operator 가 approve 한다. 문서 단위 검증은 `amplai ops validate <kind> <document>` (3.0.0 schema) | `runtime/cli.py:403-416`; `USING_AMPLAI_WORK.ko.md:3` |
| `loopctl doctor` | `CLAUDE.md:38`; `ci.yml:32` | none: removed. 환경 점검은 driver qualification(`container_qualify.py`, `opencode_qualify.py`)과 `amplai ops version` 이 맡는다 | `runtime/cli.py:277`; `USING_AMPLAI_WORK.ko.md:18-27` |
| `loopctl classify`, `permission check`, `garden` | `AGENTS.md:97,101,103` | none: removed | — |
| `loopctl docs validate`, docs cycle (`amplai_docs.py`, sidecar) | `AGENTS.md:102` | none: removed. V3 docs-review store(D-090)가 대체하는지 **확인 필요** | — |
| `scripts/eval.sh --feature --slice` | `CLAUDE.md:40`; `AGENTS.md:104` | none: removed. V3 에서는 계약의 test suite 를 sandbox 에서 돌리는 verifier 가 검증한다 | `local_deployment.py:37` (`SuiteVerifier`, `NonEmptyChangeCheck`) |
| `.ai-team/verifiers/run.py --profile v2` | `CLAUDE.md:39`; `AGENTS.md:105` | CI 단계를 직접 부른다: `python -m pytest`, `ruff check .`, `ruff format --check .`, `mypy src` | `ci.yml:23-26` |
| `amplai-foundry verify` (7 check) | `CLAUDE.md` Verification 문단; `verification/runner.py` | none: removed (D1 이 CLI 를 남기면 유지) | `cli.py:659` |
| dev-loop / code-review / systematic-debugging / taskify / speckit-* | `.agents/skills/*` | V3 execution loop 안의 strategy·decider·judge 가 같은 역할을 하는지 **확인 필요** (Work 033 S9·S10). 개발자가 직접 부르는 대체는 없다: removed | — |
| SessionStart/SessionEnd hook (supervisor 등록) | `.claude/settings.json:3-24`, `.codex/hooks.json:3-24` | none: removed | — |
| supervisor / project store (`amplai_supervisor.py`, `amplai_runtime.py`) | scanner 1·3 | V3 local server 와 Store(`runtime/storage/store.py`)가 대체하는지 **확인 필요** | — |
| Loop Kit 2.x 배포 (`kit_distribute.py`, `tools/amplai-loop-kit`) | `ci.yml:30-31` | `amplai ops kit` (서명된 ownership-aware install/update/recover) | `runtime/cli.py:26,28,1064-1065,1117` |
| `eli12`, `grill-me`, `grilling` | `CLAUDE.md` Commands | D4 결정 | — |

### 3.2 AGENTS.md, CLAUDE.md, CI Changes

- `CLAUDE.md`
  - `:4` 의 `.ai-team/README.md` 참조를 지운다.
  - Commands(`:12-13`)를 `amplai work "<goal>"`, `amplai design "<problem>"`, `amplai approve|status|steer|replan|cancel` 로 바꾼다. 내부 skill 목록 문단을 지운다.
  - Skill Layout 문단은 D4 결과에 따라 지우거나 helper 3개만 남긴다.
  - Verification(`:38-40`)을 `.venv/bin/python -m pytest`, `ruff check .`, `ruff format --check .`, `mypy src` 로 바꾼다. "registry 가 단일 출처" 문장을 지운다.
- `AGENTS.md`
  - `:24` 에서 knowledge lint 는 D1 에 따른다.
  - `:32-34` (`amplai-foundry ingest`, `curate prepare`)는 D1 에 따른다.
  - `:62-105` V2 controller·`loopctl`·`eval.sh`·verifier runner 블록을 §3.1 의 V3 명령으로 바꾼다.
  - `:114-122` `.ai-team/` 구조 설명을 지운다. Decision 기록 위치(`:121-122`)는 D10 결정으로 다시 쓴다.
  - `:128` (`vault-lint`)와 `:136` (`loopctl doctor` skill 검사)을 지운다.
- `README.md`: knowledge docs 링크는 D1 에 따른다(scanner 3, 줄 번호 **확인 필요**).
- CI `.github/workflows/ci.yml`
  - `:30-32` 를 지운다.
  - `:27-29` 는 D1 이 delete 면 지우고, keep 이면 둔다.
  - `:23-26` 은 그대로 둔다. pytest 는 directory 기반(`pyproject.toml:57` `testpaths`)이라 test file 삭제로 범위가 자동으로 준다.
- `pyproject.toml`: M8.

## 4. Risks and Deletion Order

### 4.1 Risks

1. **CLI 가 import 에서 죽는다.** M1·M2·M3 를 먼저 하지 않고 `governance/`, `curation/`, `verification/runner.py` 를 지우면 `runtime/cli.py:15` → `control_plane/__init__.py:5` 사슬과 `local_deployment.py:42` → `deployment.py:27-29` 사슬이 ImportError 를 낸다.
2. **모든 session 에서 hook 이 실패한다.** `scripts/amplai_hook.py` 를 hook 설정(`.claude/settings.json:8,19`, `.codex/hooks.json:8,19`)보다 먼저 지울 때 생긴다.
3. **`tests/test_direct_mutation_architecture.py:17-21`** 이 `src/amplai_foundry` 전체를 scan 한다. 이 test 는 governance·intake 를 import 한다(`:9-15`). D2 가 delete 면 같이 지우고, 그 architecture guard 가 V3 에 필요한지 따로 본다(**확인 필요**).
4. **실제 저장소 상태를 단언하는 V3 test 가 깨진다.** `tests/v3/test_rc01_kit_installer_v3.py:249-255` (kit VERSION), `test_rc01_legacy_retirement.py` (저장소 전체 retirement plan 재생성 비교), `test_rc01_e2e_render.py:24-25` (specs/012). 삭제와 같은 commit 에서 고친다.
5. **진행 중인 작업과 충돌한다.** 이 worktree 에 Work 033 의 수정·untracked 파일이 있다(`runtime/execution/*.py`, `meta_harness/{dashboard,nightly,quota,surrogate}.py`, `deployment/launchd/`). 삭제는 그 wave 가 merge 된 뒤 별도 branch 에서 한다.
6. **D-101 의 test 이식 순서.** `meta_harness/reference.py` 를 쓰는 test 중 real evaluation semantics 를 단언하는 것은 지우지 않고 옮긴다(`plan.md:166-168`). 어느 test 가 semantics 를 단언하는지 file 단위로 읽지 않았다(**확인 필요**).
7. **ruff 범위가 바뀐다.** exclude(`pyproject.toml:76-86`)를 먼저 지우고 대상 파일을 나중에 지우면 그 사이 CI 가 kit 의 `%` 포맷 등으로 실패한다. exclude 는 대상 삭제와 같은 commit 이거나 그 뒤에 지운다.
8. **scanner 간 불일치.** 이 문서에서 직접 확인해 바로잡은 것:
   - scanner 2 의 "`search`/`parsing`/`repositories` 는 V3 에서 닿지 않음" 은 틀렸다. M1 사슬로 닿는다(`context_builder.py:8-10`).
   - scanner 2 의 "`evaluation/quality.py` importer 없음" 도 틀렸다(`meta_commands/evaluator.py:27`).
   - scanner 3 의 "governance test 는 live V3 코드를 지킨다" 는 package `__init__`·module-level import 의 부수 효과 때문에 생긴 판단이다. 기능상 V3 local 경로는 governance 를 쓰지 않는다.
   - scanner 1 의 "installer 가 `.ai-team` 을 읽는다" 는 대상 app 경로 문자열이다(K8).
   - scanner 2 가 governance 를 참조한다고 센 `tests/v3` file 중 import 로 확인된 것은 `test_rc01_hermes_adapter.py:9`, `test_rc02_followups.py:9` 둘뿐이다. 나머지는 문자열 언급일 수 있다(**확인 필요**).

### 4.2 Deletion Order

0. **Decisions.** operator 가 D1-D12 를 정한다. Work 033 wave 를 merge 한다.
1. **References first** (한 PR). hook 설정 삭제(X4), CI `:30-32` 삭제(M7), registry 와 `.ai-team/verifiers` 를 지운다(X6). `CLAUDE.md`·`AGENTS.md` 를 V3 명령으로 바꾼다(M9).
2. **Decouple** (한 PR, 동작 변화 없음). M1(`control_plane/__init__.py`), M2(key helper 이동), M3(`verification/__init__.py`), M4(`PROTECTED_PATHS`), M6(`test_core_runtime.py:56` 경로). 이 시점에 `amplai` 가 V2 package 없이 import 되는지 확인한다: 예) `python -X importtime -c "import amplai_foundry.runtime.cli"` 결과에 `governance`·`curation` 이 없어야 한다.
3. **Code.** X1, X2, X3, X5, X7, X8, X10-X15. D1·D2·D3·D6·D7·D8 은 결정 결과대로 지운다.
4. **Tests.** 지운 코드의 test 를 지운다. D-101 대상은 real fixture 로 이식한다. M5 fixture 를 옮긴다. `tests/v3/test_rc01_kit_installer_v3.py::test_t097_*` 를 고친다.
5. **Docs and data.** X9(sidecar), X16, X17, D10, M10. `pyproject.toml` 정리(M8).
6. **Green CI.** `python -m pytest -n auto --dist worksteal`, `ruff check .`, `ruff format --check .`, `mypy src` 가 통과해야 끝이다(`ci.yml:23-26`).

## 5. Open Items For The Operator

1. **D1.** `amplai-foundry` knowledge CLI·`vault/`·`schemas/`·repo `.amplai/` 를 V3-only 개발에서 계속 쓰는가? CI `ci.yml:27-29` 가 이 결정에 달렸다.
2. **D2.** Slack/Hermes governance 층(`governance/`, V2 `control_plane/`, `integrations/hermes*`, bridge script 2개)을 지우는가?
3. **D3.** `amplai ops serve` (server mode + Live Foundry authority link)를 쓰는가? 쓰지 않으면 `RuntimeDeployment`·`foundry_authority.py` 를 지우고 governance 결합이 0 이 된다.
4. **D4.** helper skill `eli12`, `grill-me`, `grilling` 과 `skills-lock.json` 을 남기는가?
5. **D5.** `/work`·`/design` skill 을 지우고 `amplai work`·`amplai design` 으로 바꾸는 시점. 이 저장소가 `~/.amplai/local/local.json` 에 app 으로 등록돼 있는지(**확인 필요**).
6. **D6.** V3 안의 미도달 module 8개를 지우는가? 특히 `runtime/effects/` 와 `tool_broker/` 는 설계상 남겨 둔 boundary 인지 operator 확인이 필요하다.
7. **D7·D8.** `ops demo`·`ops execution-demo` 와 일회성 probe script 7개를 지우는가?
8. **D9.** downstream repo 가 아직 Kit 2.x 배포를 받는가?
9. **D10·D11.** `DECISIONS.md` 이력과 `.ai-team/policy/approvals.jsonl` 승인 ledger 를 어디에 보존하는가? 아니면 git history 로 충분한가?
10. 이 문서가 scanner 에만 기대고 확인하지 않은 것: X11·X12·X14 의 test importer 목록, K6 의 일부 script 근거, X9 의 D-090 docs-review store, `README.md` 줄 번호. 삭제 PR 전에 grep 으로 다시 확인한다.
