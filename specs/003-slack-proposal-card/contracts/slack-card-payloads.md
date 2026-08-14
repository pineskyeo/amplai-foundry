# Contract: Slack Card Payloads

## Common Envelope

Both Card types use one top-level fallback and one Card block.

```json
{
  "text": "<bounded fallback>",
  "blocks": [
    {
      "type": "card",
      "title": {"type": "plain_text", "text": "<bounded title>"},
      "subtitle": {"type": "plain_text", "text": "<bounded subtitle>"},
      "body": {"type": "mrkdwn", "text": "<bounded body>"},
      "subtext": {"type": "mrkdwn", "text": "<bounded subtext>"}
    }
  ]
}
```

The HTTP transport adds `channel` and existing `metadata` marker after rendering.

## Result Card

### Required Content

- Title: `Proposal approved`, `Proposal rejected`, or `Changes requested`
- Subtitle: Proposal ID
- Body: project, content revision, state revision
- Subtext: decision epoch and historical-result label
- Actions: absent
- Fallback: Proposal ID + unambiguous result

### Semantic Mapping

| `proposal_status` | Title | Tone |
|---|---|---|
| `approved` | `Proposal approved` | success |
| `rejected` | `Proposal rejected` | destructive outcome |
| `changes_requested` | `Changes requested` | revision outcome |

Status color is not a source of meaning. Text remains sufficient.

## Review Card

### Required Content

- Title: `Proposal review`
- Subtitle: Proposal ID and qualified project namespace + project ID
- Body: content revision, operation type counts, first three titles, remaining count
- Subtext: designated reviewer and exact ISO 8601 UTC expiration, including non-zero fractional seconds
- Actions: exactly three
- Fallback: Proposal ID + review requested + reviewer + the same exact expiration

### Actions

| Action ID | Label | Style | Value |
|---|---|---|---|
| `approve` | `Approve` | `primary` | `{token_id}.{raw_credential}` |
| `request_changes` | `Request changes` | default | `{token_id}.{raw_credential}` |
| `reject` | `Reject` | `danger` | `{token_id}.{raw_credential}` |

Each action includes Slack `confirm` with action-specific title, body, confirm label, and `Cancel`.
The confirm body states that the action applies to the shown Proposal snapshot and names its content
and state revisions.

## Bounds

| Value | Limit | Behavior |
|---|---|---|
| Fallback `text` | 200 chars | deterministic ellipsis |
| Card title | 150 chars | renderer-owned fixed text |
| Card subtitle | 150 chars | deterministic ellipsis |
| Card body | 200 chars | omit optional detail before truncation |
| Card subtext | 200 chars | deterministic ellipsis |
| Operation preview | 3 titles | stable definition order |
| Operation title | 48 chars | deterministic ellipsis |
| Action count | 3 | exact |

Renderer counts Unicode code points. Truncation never touches the stored Proposal definition.

## Safe Content

Allowed:

- Proposal ID
- project namespace and project ID
- revisions and decision epoch
- operation type counts
- operation titles
- reviewer Slack external key
- UTC expiration

Forbidden:

- reason or full evidence
- draft body or draft path
- local filesystem path
- signing secret or bot token
- raw credential outside button value in the in-memory request
- internal exception text

## Validation

Renderer rejects unknown status, malformed operation summary, non-Slack review channel, wrong action
set, mismatched snapshot, and overlong unbounded input. It returns immutable mappings and does not log
input objects.
