# AMPLAI Loop Kit 2.4 — Host and Federation Contract

## HostAdapter

`amplai_hosts.py` is the only place that knows how Claude Code, Codex, or a generic command builds argv, redacts prompts, extracts continuation IDs, and shapes host-specific hook output.

Public entry points remain:

- Claude Code: `/work`, `/design`
- Codex: `$work`, `$design`

Both enter the same Work/Question/Evidence/Decision protocol.

## Decision/Evidence promotion

Local objects are authoritative for repository work and are never silently turned into Platform knowledge.

```text
LOCAL
  ↓ mark_promotion_candidate
CANDIDATE
  ↓ mark_promotion_submitted
SUBMITTED
  ├─ resolve accepted → ACCEPTED + canonical_ref
  └─ resolve rejected → REJECTED + reason
                         ↓ reconsider
                      CANDIDATE
```

`promotion_envelope()` carries origin store/project/object reference, local content hash, deterministic idempotency key, and payload.  A Platform outage does not invalidate the local Project Store.

## Compatibility

The added `federation` member is optional in the JSON schemas.  Existing 2.3.1 Project Store Decision/Evidence files therefore remain readable and do not require a destructive migration.  Newly created 2.4 objects include federation metadata.
