# Proposals

## Domain Contract

Proposal은 Source evidence에서 파생한 canonical knowledge 변경 제안이다. 상태는 `draft`, `reviewed`, `approved`, `applied`, `rejected`, `superseded`다.

Operation은 `CREATE`, `UPDATE`, `LINK`, `MERGE`, `SPLIT`, `SUPERSEDE`, `CONFLICT`, `IGNORE`를 지원한다. Confidence는 사실 여부가 아니라 curator 판단 강도이며 `low`, `medium`, `high`만 사용한다.

Proposal artifact는 `.amplai/proposals/{proposal_id}/`에 저장한다. `proposal.yaml`, `summary.md`, `drafts/`, `patches/`를 Git에 포함한다.

## Validation

```bash
amplai-foundry proposal validate .amplai/proposals/PROP-.../proposal.yaml
amplai-foundry proposal show PROP-...
amplai-foundry proposal diff PROP-...
```

Validation은 Pydantic schema, Source, evidence Source, target, draft scope를 확인한다. Draft는 Proposal의 `drafts/` directory 밖을 참조할 수 없다.

Source는 evidence로 참조할 수 있지만 Proposal로 생성하거나 변경할 수 없다. `CREATE kind=source`와 Source 대상 `UPDATE`, `LINK`, `SUPERSEDE`, `MERGE`, `SPLIT`은 거부한다.

Proposal의 `source_ids`, evidence Source, target, draft는 모두 `Proposal.project`와 같은 project에 속한다. Draft namespace는 `Proposal.namespace`와 일치한다. CREATE destination은 draft가 아니라 검증된 Proposal project root에서 계산한다.

## Optimistic Concurrency

새 `draft`, `reviewed`, `approved` Proposal의 `UPDATE`, `LINK`, `SUPERSEDE` operation은 다음 precondition을 기록한다.

```yaml
expected_revision: 3
expected_target_sha256: 64자리-lowercase-sha256
```

`expected_target_sha256`은 Proposal 생성 시점 canonical target Markdown raw bytes의 SHA-256이다. Apply lock 안에서 현재 revision과 hash를 다시 검사한다. 하나라도 다르면 `PROPOSAL_STALE_REVISION` 또는 `PROPOSAL_STALE_TARGET_HASH`로 전체 Proposal을 거부한다. Vault와 Proposal status는 변경하지 않는다.

이미 `applied`인 v1 Proposal은 역사적 artifact다. Precondition이 없어도 계속 load와 validation이 가능하며 기존 artifact를 다시 쓰지 않는다.

## Approval And Apply

```bash
amplai-foundry proposal approve PROP-... --approved-by user
amplai-foundry proposal apply PROP-...
```

Apply는 `approved` 상태만 허용한다. Apply lock 안에서 Source 불변성, concurrency precondition, lifecycle transition을 다시 확인한다. 변경 전 Vault와 staging Vault에서 lint ERROR 0을 요구한다. Staging 결과가 통과하면 같은 filesystem에서 Vault directory를 swap한다. Proposal 상태 저장이 실패하면 backup Vault를 복원한다.

Minimal apply는 draft 기반 `CREATE`, `UPDATE`, `LINK`, `SUPERSEDE`와 write가 없는 `IGNORE`를 처리한다. `CONFLICT`가 있으면 전체 apply를 거부한다. `MERGE`와 `SPLIT`은 현재 apply에서 거부하고 human이 새 Proposal shape를 결정한다.

Git commit SHA는 apply commit 뒤의 후속 Proposal report commit에 기록한다. Commit이 자기 hash를 같은 commit content에 기록할 수 없으므로 두 단계 기록을 사용한다.
