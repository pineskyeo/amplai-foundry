<!-- AMPLAI-ASYNC-BEGIN -->
## Decision & Async Cross-App Runtime

`AMPLAI_PROJECT_HOME`과 `.ai-team/app.json`이 있으면 현재 작업을 공용 Work와 연결한다.
사용자에게 internal Decision/CR/Handoff 단계를 선택하게 하지 않는다.

1. supervised 실행이면 `AMPLAI_WORK_ID`의 Context를 먼저 읽는다.

```bash
python3 scripts/amplai.py work context --id "$AMPLAI_WORK_ID"
```

2. Open Question을 발견하면 repository에서 확인 가능한 사실을 먼저 조사하고 구조화한다.

```text
Question → Evidence mode(DIRECT|LOCAL|PARALLEL)
         → policy minimum(AUTO|CHALLENGE|HUMAN)
         → Decision → Verify
```

- LOCAL이 기본이다. 독립적인 evidence lane이 2개 이상이고 병렬 이익이 있을 때만 PARALLEL이다.
- Primary Agent가 최종 Decision owner다. Subagent는 evidence worker/challenger다.
- CHALLENGE는 독립 ACCEPT review evidence, HUMAN은 human_approval evidence가 있어야 닫힌다.
- 필수 OPEN Question이 남은 Work는 DONE으로 끝내지 않는다.

3. 다른 앱 작업이 필요하면 prose-only handoff를 만들지 않는다. 같은 CR에 target Work를 만들고
acceptance, dependency, contract/decision/evidence reference를 연결한 뒤 현재 Work를 WAITING으로
전환한다. target view가 필요할 때만 `work context --format markdown`으로 렌더링한다.

4. worker가 종료되기 전에 현재 Work를 반드시 다음 중 하나로 durable transition한다.

```text
DONE | WAITING | BLOCKED | HUMAN_REQUIRED | FAILED
```

- WAITING: 구조화된 dependency가 끝나면 Supervisor가 자동 재개한다.
- BLOCKED: 자동 해제 조건이 아직 모델링되지 않았다.
- HUMAN_REQUIRED: 사람의 결정 없이는 자동 재개하지 않는다.

`AMPLAI_LEASE_TOKEN`은 이미 환경에 있고 `scripts/amplai.py`가 직접 읽는다. 명령줄에 토큰을
쓰거나 출력하지 않는다.

죽은 upstream 때문에 막혔다면 사람에게 다음을 제안한다. Agent가 임의로 실행하지 않는다.

```text
work cancel --cascade | work retarget --depends-on | work reset-attempts
```

5. Work가 DONE이면 result evidence를 남기고 dependency를 해제한다. 독립적인 다른 앱 Work는 한
Work의 HUMAN_REQUIRED와 무관하게 계속 진행될 수 있다.

중앙 Store나 app identity가 없는 단일 repo 작업은 기존 Loop V2 경로로 정상 동작한다.
<!-- AMPLAI-ASYNC-END -->
