from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.events import DecisionProjectionPayload, OutboxEventView, OutboxState
from amplai_foundry.governance.models import ProposalRef
from amplai_foundry.governance.slack_cards import (
    SlackCardRenderingError,
    SlackProposalCardRenderer,
    slack_presentation_payload,
)
from amplai_foundry.governance.slack_projection import (
    SlackHistoryPage,
    SlackProjectionDestination,
    SlackSendResult,
    payload_digest,
)

PROPOSAL = ProposalRef(
    project_ref=ProjectRef(project_id="amplai", namespace="amplai/project/amplai"),
    proposal_id="PROP-20260812-ABCDEF12",
)
DIGEST = "sha256:" + "a" * 64
DESTINATION = "provider:slack:" + "b" * 64


def _decision(status: str = "approved") -> DecisionProjectionPayload:
    actions = {
        "approved": "approve",
        "rejected": "reject",
        "changes_requested": "request_changes",
    }
    return DecisionProjectionPayload(
        action=actions[status],
        active_definition_digest=DIGEST,
        aggregate_ref=PROPOSAL,
        content_revision=4,
        decision_epoch=2,
        proposal_status=status,
        state_revision=7,
    )


@pytest.mark.parametrize(
    ("status", "expected_title"),
    [
        ("approved", "Proposal approved"),
        ("rejected", "Proposal rejected"),
        ("changes_requested", "Changes requested"),
    ],
)
def test_result_card_has_required_identity_and_no_actions(
    status: str,
    expected_title: str,
) -> None:
    rendered = SlackProposalCardRenderer().render_result(_decision(status))

    assert PROPOSAL.proposal_id in str(rendered["text"])
    blocks = rendered["blocks"]
    assert isinstance(blocks, tuple) and len(blocks) == 1
    card = blocks[0]
    assert isinstance(card, Mapping)
    assert card["type"] == "card"
    assert card["title"] == {
        "type": "plain_text",
        "text": expected_title,
        "emoji": False,
    }
    serialized = str(card)
    assert "amplai/project/amplai" in serialized
    assert "Content revision:* 4" in serialized
    assert "State revision:* 7" in serialized
    assert "actions" not in card


def test_result_card_preserves_required_fields_for_a_long_valid_namespace() -> None:
    namespace = "/".join((*("segment" for _ in range(80)), "project", "amplai"))
    proposal = PROPOSAL.model_copy(
        update={"project_ref": ProjectRef(project_id="amplai", namespace=namespace)}
    )
    payload = _decision().model_copy(update={"aggregate_ref": proposal})

    rendered = SlackProposalCardRenderer().render_result(payload)
    body = str(rendered["blocks"][0]["body"]["text"])  # type: ignore[index]

    assert len(body) <= 200
    assert "(amplai)" in body
    assert "Content revision:* 4" in body
    assert "State revision:* 7" in body


def test_result_card_presentation_is_recursively_immutable() -> None:
    rendered = SlackProposalCardRenderer().render_result(_decision())
    card = rendered["blocks"][0]  # type: ignore[index]

    with pytest.raises(TypeError):
        rendered["text"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        card["body"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        rendered["blocks"].append({})  # type: ignore[union-attr]


def test_result_card_fields_stay_inside_slack_limits() -> None:
    rendered = SlackProposalCardRenderer().render_result(_decision())
    card = rendered["blocks"][0]  # type: ignore[index]

    assert len(str(rendered["text"])) <= 200
    assert len(card["title"]["text"]) <= 150  # type: ignore[index]
    assert len(card["subtitle"]["text"]) <= 150  # type: ignore[index]
    assert len(card["body"]["text"]) <= 200  # type: ignore[index]
    assert len(card["subtext"]["text"]) <= 200  # type: ignore[index]


def test_unrelated_projection_payload_is_preserved() -> None:
    payload = {"action": "request_apply", "proposal_status": "apply_requested"}

    assert (
        slack_presentation_payload(
            payload,
            renderer=SlackProposalCardRenderer(),
        )
        is payload
    )


def test_malformed_decision_payload_fails_closed() -> None:
    with pytest.raises(SlackCardRenderingError, match="SLACK_CARD_RENDER_FAILED"):
        slack_presentation_payload(
            {"proposal_status": "approved", "action": "approve"},
            renderer=SlackProposalCardRenderer(),
        )


class CapturingTransport:
    def __init__(self) -> None:
        self.payload: Mapping[str, object] | None = None
        self.marker: Mapping[str, object] | None = None

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        self.payload = payload
        self.marker = marker
        return SlackSendResult(channel=channel, ts="1700000000.000001")

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage:
        return SlackHistoryPage()


def test_destination_renders_result_but_marker_keeps_semantic_digest() -> None:
    semantic = _decision().model_dump(mode="json")
    event = OutboxEventView(
        event_id="OBX-0123456789ABCDEF",
        proposal_ref=PROPOSAL,
        aggregate_sequence=1,
        destination_ref=DESTINATION,
        destination_sequence=1,
        source_state_revision=7,
        payload_digest=payload_digest(semantic),
        payload=semantic,
        state=OutboxState.LEASED,
        attempts=1,
        claim_generation=1,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    transport = CapturingTransport()
    destination = SlackProjectionDestination(
        transport,
        destination_ref=DESTINATION,
        channel="C123",
        app_id="A123",
        max_attempts=3,
    )

    receipt = destination.send(event)

    assert receipt == "slack:C123:1700000000.000001"
    assert transport.payload is not None
    assert transport.payload != semantic
    assert transport.payload["blocks"][0]["type"] == "card"  # type: ignore[index]
    assert transport.marker is not None
    marker_payload = transport.marker["event_payload"]
    assert isinstance(marker_payload, dict)
    assert marker_payload["payload_digest"] == event.payload_digest
