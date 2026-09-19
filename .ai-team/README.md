# AMPLAI Loop Runtime V2 — amplai-foundry

`.ai-team/`은 agent/skill 모음이 아니라 amplai-foundry 개발을 반복 가능하게 만드는 **얇은 project-local runtime/policy layer**다.

```text
Goal
  → Knowledge Readiness / Context
  → Contract / Plan / Slice
  → Execute / Verify / Diagnose / Repair ↺
  → Evidence / Converge / Review / Handoff
  → validated knowledge candidate
```

## Public surface

사용자가 기억할 command는 둘뿐이다.

```text
Claude Code   /work <goal>       /design <problem>
Codex         $work <goal>       $design <problem>
```

두 host의 의미와 절차는 같다. `.agents/skills/`가 공통 정본이고 `.claude/skills/`는 exact
symlink mirror다.

`readiness`, `discovery`, `context`, `mine`, `semantic`, `review`, `debug`는 internal capability 또는 deterministic tool이다. 새 user-facing command로 늘리지 않는다.

## V1과 V2

| 버전 | 책임 |
|---|---|
| V1 — Reliable Single-Agent Loop | Contract, Slice, deterministic verifier/TDD, bounded repair, converge, review |
| V2 — Knowledge-Aware Evidence-Governed Loop | V1 + Knowledge Readiness, Context Pack, Evidence/Provenance, Permission/Environment, Project Mining, Semantic Runtime |

V2는 V1 dev-loop를 재구현하지 않는다. Knowledge/Evidence/Governance plane만 추가한다.

## Directory ownership

```text
.ai-team/
├── runtime/      # state transition, risk, retry/escalation
├── policy/       # readiness, permission, TDD, quadrant, documentation, gardening
├── contracts/    # Work Done contract schema/template
├── verifiers/    # executable PASS/FAIL registry + environment evidence
├── knowledge/    # knowledge map/index/status/provenance metadata + artifact schema
├── evidence/     # evidence/provenance schema와 정책
└── rules/        # amplai-foundry-specific engineering guards
```

`.ai-team` 밖에는 개발 도구 자산이 하나 더 있다.

```text
tools/amplai-loop-kit/   AMPLAI Loop Kit 정본 (D-053). 배포 대상은 이 저장소와
                         synapse·cortex 이고 scripts/kit_distribute.py 가 배포한다.
                         upstream 형식을 유지하므로 ruff 의 벤더링 예외에 있다.
                         운영은 docs/workstreams/amplai-loop-runtime-adoption/
                         KIT-DISTRIBUTION.md 를 본다.
```

Canonical project assets는 복사하지 않는다.

- canonical knowledge note: `vault/projects/<project>/`
- architecture/decision: `docs/workstreams/*/DECISIONS.md`, `specs/<feature>/`
- domain knowledge: `docs/`의 주제별 문서
- Work Memory: `specs/<feature>/contract|readiness|context|environment|trace|handoff|doc-impact|garden-report`
- 실행 evidence: code/test/git/verifier output

`.ai-team`은 문서 원문이나 Work별 report 사본을 쌓지 않는다. 정책·schema와 이를
선택·검증하는 index, 상태, provenance metadata를 둔다. Documentation policy의
review_store는 문서별 검토와 이전 검토의 단일 metadata 저장소다. 원문은 각 owner
경로에 남고, Work의 영향 분석은 이 검토에서 생성하는 view다. 검토 이력은 사람의
승인이나 canonical Vault 지식이 아니다.

> **예외 — AMPLAI Loop Kit 이 설치된 동안 (`D-051`, `D-053`).** kit 이 `.ai-team` 에 다음을
> 놓는다. 정책·schema 가 아닌 것이 셋이다.
>
> ```text
> 정책·schema (규약에 맞음)   runtime/async-policy.json, runtime/schemas/*, runtime/app.template.json
> 문서 (예외)                 runtime/{DECISION_ASYNC_PROTOCOL,LOCAL_SUPERVISOR,INSTALLATION}.md
> 이력 (예외)                 install/amplai-loop-kit.json, backups/amplai-loop-kit/
> identity (예외)             app.json, AUTONOMY_POLICY.md
> ```
>
> kit 은 앱 단위 설치 도구라 이 경로들을 쓸 수밖에 없다. **규약 본문은 고치지 않았고**
> kit 을 제거하면 이 각주도 함께 지운다.

## V2 work flow

```text
/work
  → classify
  → Contract
  → Knowledge Readiness
       READY/BYPASS → Context Pack
       DISCOVER     → Domain Discovery → re-evaluate
       BLOCKED      → human/business decision only
  → Environment fingerprint
  → Plan / taskify / slices
  → dev-loop
       implement → verify → diagnose → repair ↺
  → evidence trace
  → converge
  → Documentation Freshness       ← reviewer가 보기 전에 코드와 문서를 맞춘다
  → review
  → Incremental Gardening         ← 본 기능이 끝난 뒤 주변만 정리한다
  → semantic candidate / evidence finalize
  → handoff / DONE
```

Blocking unknown 또는 conflict가 남으면 implementation을 시작하지 않는다.

Documentation Freshness가 review **앞**에 있는 이유는 reviewer가 보는 시점에 코드와 문서가
이미 맞아야 하기 때문이다. Gardening이 review **뒤**에 있는 이유는 cleanup이 본 기능을
흔들지 않아야 하기 때문이다.

Gardening이 실제 file/code/config를 바꿨으면 loop로 되돌아간다.

```text
garden change → verify → converge → documentation freshness → (필요하면) review
```

일반 Work와 `garden apply`는 report-only다. safety 분류는 삭제 승인이 아니다.

`design`은 code를 구현하지 않으므로 Repository Gardening을 실행하지 않고 Documentation
Freshness만 적용한다.

## Four memory types

| Type | 질문 | Canonical asset |
|---|---|---|
| Stable Knowledge | 시스템은 원래 어떻게 동작하는가 | source/docs/ontology/active claims |
| Decision Memory | 왜 이렇게 선택했는가 | ADR/plan/decision index |
| Work Memory | 이번 Work가 어디까지 됐는가 | feature별 contract/context/trace/handoff |
| Evidence | 그 사실의 근거는 무엇인가 | code/test/API/log/git/verifier result |

`deprecated`, `superseded`, `rejected`는 기본 Context Pack에서 제외한다. 충돌을 Agent가 조용히 선택하지 않는다.

## Semantic Runtime

이 저장소는 semantic runtime(ontology TTL, SHACL, competency question, read-only MCP,
project miner)을 이식하지 않았다 (`D-046`). Knowledge Vault와 Proposal 모델이 그 자리를
대신하고, 그 층의 검사는 verifier registry의 `vault-lint`와 `schema` check가 맡는다.

Foundry의 기존 knowledge query는 read-only다. 새 공식 지식은 기존 Proposal 검토와
governed approval 경계를 따른다. 이 profile은 RDF/SHACL/CQ나 MCP 검증을 실행한 것으로
보고하지 않는다. Portable 문서 검토는 공식 Vault 승인을 만들지 않는다.

## Documentation Freshness

코드가 바뀌면 그 코드를 설명하는 canonical 문서도 같이 맞춰야 한다. 판정은
`.ai-team/policy/documentation.json`이 정하고, 결과는 `specs/<work>/doc-impact.json`에 남는다.

impacted document 후보는 다섯 축의 deterministic evidence로 먼저 좁힌다. "관련 문서를 알아서
찾아라"로 시작하지 않는다.

```text
changed files → impact rules → ontology binding → knowledge map → decision index → doc reference
```

문서 상태는 `ACTIVE / STALE / SUPERSEDED / DEPRECATED / CANDIDATE`다. 과거 결정은 덮어쓰지
않는다. 이 repository의 convention은 `docs/workstreams/<workstream>/DECISIONS.md`의 D-NN
append-only이며 뒤집힌 항목에 `Superseded: ... 은 D-MM 이 대체한다` 줄을 달고,
`.ai-team/knowledge/decisions.index.json`이 `status`/`superseded_by`로 같은 사실을 유지한다.

`docs validate --repo`는 canonical 문서가 존재하지 않는 repository 경로를 선언하는지 본다.
문서가 실제와 어긋났음을 기계적으로 판정할 수 있는 신호다.

현행 guide는 policy의 명시적 source roots와 단일 metadata owner로 선택한다.
`document_source_routes`는 설치용 Markdown을 담당 guide의 입력으로 연결하고 과거
기록은 baseline 원문 그대로 보존한다. 필수 impact rule과 미분류 source 검사는 유지한다.
이 분류는 지식 승인·삭제 권한이 아니며, source bytes가 바뀌면 담당 guide 검토가 만료된다.

## Gardening

Gardening은 다섯 영역을 덮는다.

```text
Documentation   stale / superseded / duplicate / orphan
Knowledge       stale claim / missing evidence
Ontology        duplicate relation / collision
Harness         obsolete policy / unused verifier / legacy AI artifact
Repository      dead code / obsolete script / stale config / orphan fixture / generated garbage
```

범위는 둘로 나뉜다. 일반 `/work`는 incremental이고 changed scope 주변만 본다. repository
전체 scan은 `work_type = repository_gardening`인 explicit Work에서만 한다.

삭제는 세 단계다.

| Level | 대상 | 자동 삭제 |
|---|---|---|
| `SAFE_AUTO` | 호환 label; 소유·재생성·승인·복구 증거는 별도 | 실행 안 함 |
| `EVIDENCE_REQUIRED` | orphan fixture, obsolete internal script, broken reference | 불가 — reference/build/test/packaging 근거 필요 |
| `HUMAN_GATED` | tracked 문서, ADR/incident, 사용자 backup, worktree, production/policy/private 자산 | 금지 |

`.bak`와 `.orig`, untracked, 파일 나이와 Git history는 자동 삭제 근거가 아니다.
scan 한도·Git 오류·접근 불가 범위는 incomplete로 남으며 PASS가 아니다.
문서 split/merge는 `docs preserve --input <request.json>`으로 원본의 모든 section을
원문 유지 또는 정확한 목적지에 연결한다. 요약은 보존 증명이 아니다.
`docs plan-retirement --input <request.json>`은 reference/retention/backup/approval
필요성을 읽기 전용으로 보고한다. planning 파일을 checkout 밖에 두면 자기 참조를 피한다.
tracked retirement public CLI는 없다. 별도 trusted authority/verifier adapter를 받는
internal apply/recovery는 disposable fixture에서만 검증한다. 실제 대상의 승인 생성,
Git publication, material 삭제와 production 적용은 별도다.

static text reference가 없다는 사실은 dead code 근거가 아니다. entry point는 CLI subcommand
등록, plugin/provider registry, configuration-driven dispatch로 진입할 수 있고, runner가
glob이나 디렉토리로 수집하는 test/fixture도 이름 참조가 없는 것이 정상이다. packaging과
conditional build가 참조하는 자산도 마찬가지다.

`repository-garden-integrity`의 PASS 기준은 candidate가 0개인 것이 아니다.

```text
모든 candidate가 safety/classification/evidence를 가진다
HUMAN_GATED candidate가 자동 삭제되지 않았다
삭제된 artifact에 verifier evidence가 있다
broken reference가 남지 않았다
```

## Main commands

```bash
python3 scripts/loopctl.py doctor
python3 scripts/loopctl.py classify --working
python3 scripts/loopctl.py contract validate specs/<feature>/work-contract.json
python3 scripts/loopctl.py readiness init specs/<feature>
python3 scripts/loopctl.py readiness evaluate specs/<feature>/knowledge-readiness.json
python3 scripts/loopctl.py discovery scan specs/<feature>
python3 scripts/loopctl.py context build specs/<feature>
python3 scripts/loopctl.py environment capture specs/<feature>
python3 scripts/loopctl.py trace verify specs/<feature>/evidence-trace.jsonl
python3 scripts/loopctl.py permission check <action>
python3 scripts/loopctl.py quadrant audit
python3 scripts/loopctl.py docs impact specs/<feature>
python3 scripts/loopctl.py docs validate --repo
python3 scripts/loopctl.py garden scan
python3 scripts/loopctl.py garden incremental specs/<feature>
python3 scripts/loopctl.py garden full <gardening-feature> --report-only
python3 .ai-team/verifiers/run.py --profile v2
```

`garden full`은 high-risk repository_gardening Work를 명시해야 실행된다.
`--apply-safe`는 승인 없이 차단한다. 어떤 normal gardening command도 파일을 지우지 않는다.

Semantic tools:

```bash
python3 tools/ontology/semanticctl.py summary
python3 tools/ontology/semanticctl.py cq
python3 tools/ontology/semanticctl.py search "retest result"
python3 tools/ontology/mcp_server.py --self-test
```

## DONE contract

Done은 아래 evidence가 모두 있을 때만 선언한다.

1. Knowledge Readiness가 READY/BYPASS
2. Context Pack/Environment가 required contract와 일치
3. 모든 Slice의 acceptance/eval PASS
4. 선택된 verifier profile PASS
5. converge clean
6. 필요한 review/human gate PASS
7. evidence trace/handoff가 재검증 가능
8. ontology candidate가 active model에 직접 쓰이지 않음
9. 의미 변경이 있는 Work에서 Documentation Impact가 평가됨
10. 관련 canonical documentation이 ACTIVE이거나 의도적으로 SUPERSEDED/DEPRECATED 처리됨
11. 중요한 architecture decision이 historical provenance를 유지함
12. 필요한 범위에 Incremental Gardening이 실행됨
13. 모든 garbage candidate가 evidence와 risk를 가짐
14. HUMAN_GATED candidate가 자동 삭제되지 않음
15. Gardening으로 바뀐 repository가 다시 verifier를 통과함
16. Full Gardening은 explicit repository_gardening Work에서만 수행됨
17. `/work`, `/design` 외 public command가 추가되지 않음

`LLM이 완료라고 말함`은 evidence가 아니다.

## Scope freeze

V2에는 parallel worker, fan-out/fan-in, Work DAG scheduler, remote worker, resource scheduler, multi-repo merge coordinator가 없다. 그것은 V3다.

> **예외 — AMPLAI Loop Kit 이 설치된 동안 (`D-051`, `D-053`).** kit 의 Local Supervisor 는
> claim·lease·heartbeat·app capacity 를 다루고 WAITING/READY dependency 를 돈다. 위 문구가
> V3 로 미뤄 둔 것과 겹친다. kit 은 `default_app_concurrency: 1` 로 한 앱 한 Work 를
> 강제하고 global planning·merge scheduling·publish/deploy 를 구현하지 않으므로 V3
> execution fabric 은 아니다. **`auto_start` 는 `false` 이고 supervisor 를 켜는 것은
> 아직 정하지 않았다.** kit 을 제거하면 이 각주도 함께 지운다.
