"""Pure Slack Block Kit presentation for Proposal projection payloads."""

from __future__ import annotations

import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from types import MappingProxyType
from typing import Final, TypeAlias, cast

from amplai_foundry.governance.decisions import DecisionAction
from amplai_foundry.governance.events import DecisionProjectionPayload, ReviewProjectionPayload
from amplai_foundry.governance.review_cards import PreparedReviewActionSet

_FALLBACK_LIMIT: Final = 200
_TITLE_LIMIT: Final = 150
_SUBTITLE_LIMIT: Final = 150
_BODY_LIMIT: Final = 200
_SUBTEXT_LIMIT: Final = 200

FrozenJson: TypeAlias = object

_RESULT_TITLES: Final[Mapping[str, str]] = {
    "approved": "Proposal approved",
    "rejected": "Proposal rejected",
    "changes_requested": "Changes requested",
}


class SlackCardRenderingError(RuntimeError):
    """A semantic projection cannot be represented by the frozen Card contract."""

    code = "SLACK_CARD_RENDER_FAILED"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SlackProposalCardRenderer:
    """Render bounded provider presentation without changing semantic payloads."""

    def render_result(self, payload: DecisionProjectionPayload) -> Mapping[str, object]:
        title = _RESULT_TITLES.get(payload.proposal_status)
        if title is None:
            raise SlackCardRenderingError("unsupported_result_status")
        proposal_id = payload.aggregate_ref.proposal_id
        project = payload.aggregate_ref.project_ref
        fallback = _truncate(
            f"{proposal_id}: {title.lower()} in {project.project_id} "
            f"(content r{payload.content_revision}, state r{payload.state_revision}).",
            _FALLBACK_LIMIT,
        )
        card: dict[str, object] = {
            "type": "card",
            "block_id": _truncate(
                f"result:{proposal_id}:state:{payload.state_revision}",
                255,
            ),
            "title": _text_object(title, _TITLE_LIMIT),
            "subtitle": _text_object(proposal_id, _SUBTITLE_LIMIT),
            "body": _text_object(
                self._result_body(payload),
                _BODY_LIMIT,
                kind="mrkdwn",
            ),
            "subtext": _text_object(
                f"Decision epoch {payload.decision_epoch} · Historical result",
                _SUBTEXT_LIMIT,
                kind="mrkdwn",
            ),
        }
        return _freeze_mapping({"text": fallback, "blocks": [card]})

    def render_review(
        self,
        payload: ReviewProjectionPayload,
        action_set: PreparedReviewActionSet,
    ) -> Mapping[str, object]:
        try:
            return self._render_review(payload, action_set)
        except BaseException as error:
            _clear_exception_frames(error)
            raise

    def _render_review(
        self,
        payload: ReviewProjectionPayload,
        action_set: PreparedReviewActionSet,
    ) -> Mapping[str, object]:
        self._validate_action_set(payload, action_set)
        proposal_id = payload.aggregate_ref.proposal_id
        project = payload.aggregate_ref.project_ref
        counts = " · ".join(
            f"{name} {count}" for name, count in sorted(payload.operation_counts.items())
        )
        body = self._review_body(payload, counts)
        expires = action_set.view.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        subtitle_prefix = f"{proposal_id} · "
        project_label = _bounded_project_label(
            project.namespace,
            project.project_id,
            _SUBTITLE_LIMIT - len(subtitle_prefix),
        )
        card: dict[str, object] = {
            "type": "card",
            "block_id": _truncate(
                f"review:{proposal_id}:state:{payload.state_revision}",
                255,
            ),
            "title": _text_object("Proposal review", _TITLE_LIMIT),
            "subtitle": _text_object(
                f"{subtitle_prefix}{project_label}",
                _SUBTITLE_LIMIT,
            ),
            "body": _text_object(body, _BODY_LIMIT, kind="mrkdwn"),
            "subtext": _text_object(
                f"Reviewer <@{payload.reviewer_external_key}> · Expires {expires}",
                _SUBTEXT_LIMIT,
                kind="mrkdwn",
            ),
            "actions": [
                self._button(
                    action=DecisionAction.APPROVE,
                    label="Approve",
                    value=action_set.button_value(DecisionAction.APPROVE),
                    proposal_id=proposal_id,
                    content_revision=payload.content_revision,
                    state_revision=payload.state_revision,
                    style="primary",
                ),
                self._button(
                    action=DecisionAction.REQUEST_CHANGES,
                    label="Request changes",
                    value=action_set.button_value(DecisionAction.REQUEST_CHANGES),
                    proposal_id=proposal_id,
                    content_revision=payload.content_revision,
                    state_revision=payload.state_revision,
                ),
                self._button(
                    action=DecisionAction.REJECT,
                    label="Reject",
                    value=action_set.button_value(DecisionAction.REJECT),
                    proposal_id=proposal_id,
                    content_revision=payload.content_revision,
                    state_revision=payload.state_revision,
                    style="danger",
                ),
            ],
        }
        fallback_prefix = f"{proposal_id}: review requested from "
        fallback_suffix = f"; actions expire {expires}."
        reviewer_limit = _FALLBACK_LIMIT - len(fallback_prefix) - len(fallback_suffix)
        # 현재 상수로는 도달하지 않는다. `ProposalId` 는 `^PROP-[0-9]{8}-[A-F0-9]{8}$` 로
        # 22자 고정이고 (`proposals/models.py:12`) expiry 도 고정폭이라 reviewer_limit 은
        # 항상 116 이다. 지우지 않는 이유는 그 불변이 상수 하나에 달려 있기 때문이다 —
        # `_FALLBACK_LIMIT` 이나 접두·접미 문구가 바뀌면 즉시 도달한다. 도달 가능성 판정은
        # D-034 / MGC-012-P5-T007 에 기록했다.
        if reviewer_limit < 2:
            raise SlackCardRenderingError("review_fallback_exceeds_limit")
        fallback = (
            f"{fallback_prefix}"
            f"{_truncate(payload.reviewer_external_key, reviewer_limit)}"
            f"{fallback_suffix}"
        )
        return _freeze_mapping({"text": fallback, "blocks": [card]})

    @staticmethod
    def _result_body(payload: DecisionProjectionPayload) -> str:
        revision_lines = (
            f"*Content revision:* {payload.content_revision}",
            f"*State revision:* {payload.state_revision}",
        )
        fixed_length = len("*Project:* \n" + "\n".join(revision_lines))
        project_limit = _BODY_LIMIT - fixed_length
        if project_limit < 4:
            raise SlackCardRenderingError("result_identity_exceeds_body_limit")
        project = payload.aggregate_ref.project_ref
        project_label = _bounded_project_label(
            project.namespace,
            project.project_id,
            project_limit,
        )
        return "\n".join((f"*Project:* {project_label}", *revision_lines))

    @staticmethod
    def _review_body(payload: ReviewProjectionPayload, counts: str) -> str:
        fixed = [
            f"*Content revision:* {payload.content_revision}",
            f"*Operations:* {counts}",
        ]
        if payload.remaining_operation_count:
            fixed.append(f"+{payload.remaining_operation_count} more operation(s)")
        fixed_length = len("\n".join(fixed))
        title_count = len(payload.operation_titles)
        separators = title_count
        available = max(0, _BODY_LIMIT - fixed_length - separators)
        per_title = max(2, min(48, available // title_count - 2))
        previews = [f"• {_truncate(title, per_title)}" for title in payload.operation_titles]
        return _truncate("\n".join((*fixed, *previews)), _BODY_LIMIT)

    @staticmethod
    def _validate_action_set(
        payload: ReviewProjectionPayload,
        action_set: PreparedReviewActionSet,
    ) -> None:
        if action_set.view.state != "issued":
            raise SlackCardRenderingError("action_set_state_mismatch")
        if len(action_set.issued) != 3:
            raise SlackCardRenderingError("action_set_count_mismatch")
        actions = {issued.record.allowed_action for issued in action_set.issued}
        if actions != set(DecisionAction):
            raise SlackCardRenderingError("action_set_action_mismatch")
        for issued in action_set.issued:
            record = issued.record
            if (
                record.proposal_ref != payload.aggregate_ref
                or record.active_definition_digest != payload.active_definition_digest
                or record.content_revision != payload.content_revision
                or record.state_revision != payload.state_revision
                or record.decision_epoch != payload.decision_epoch
                or record.allowed_actor_ref.actor_id != payload.reviewer_actor_id
                or record.bound_channel_ref != payload.bound_channel_ref
                or record.expires_at != action_set.view.expires_at
            ):
                raise SlackCardRenderingError("action_set_binding_mismatch")

    @staticmethod
    def _button(
        *,
        action: DecisionAction,
        label: str,
        value: str,
        proposal_id: str,
        content_revision: int,
        state_revision: int,
        style: str | None = None,
    ) -> dict[str, object]:
        confirmation: dict[str, object] = {
            "title": _text_object(f"Confirm {label.lower()}", 100),
            "text": _text_object(
                f"Apply this action to the shown {proposal_id} snapshot "
                f"(content r{content_revision}, state r{state_revision})?",
                300,
            ),
            "confirm": _text_object(label, 30),
            "deny": _text_object("Cancel", 30),
        }
        button: dict[str, object] = {
            "type": "button",
            "text": _text_object(label, 75),
            "action_id": action.value,
            "value": value,
            "accessibility_label": f"{label} {proposal_id}",
            "confirm": confirmation,
        }
        if style is not None:
            button["style"] = style
            confirmation["style"] = style
        return button


def slack_presentation_payload(
    payload: Mapping[str, object],
    *,
    renderer: SlackProposalCardRenderer,
) -> Mapping[str, object]:
    """Render supported decision payloads and preserve unrelated provider payloads."""

    status = payload.get("proposal_status")
    if status not in _RESULT_TITLES:
        return payload
    try:
        decision = DecisionProjectionPayload.model_validate(payload)
    except Exception as error:
        raise SlackCardRenderingError("invalid_decision_payload") from error
    return renderer.render_result(decision)


def _text_object(text: str, limit: int, *, kind: str = "plain_text") -> dict[str, object]:
    rendered: dict[str, object] = {
        "type": kind,
        "text": _truncate(text, limit),
    }
    if kind == "plain_text":
        rendered["emoji"] = False
    else:
        rendered["verbatim"] = False
    return rendered


def _truncate(text: str, limit: int) -> str:
    if limit < 2:
        raise ValueError("text limit은 2 이상이어야 합니다.")
    normalized = (
        " ".join(text.split())
        if "\n" not in text
        else "\n".join(" ".join(line.split()) for line in text.splitlines())
    )
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _bounded_project_label(namespace: str, project_id: str, limit: int) -> str:
    suffix = f" ({project_id})"
    namespace_limit = limit - len(suffix)
    if namespace_limit < 2:
        raise SlackCardRenderingError("project_identity_exceeds_field_limit")
    return f"{_truncate(namespace, namespace_limit)}{suffix}"


def _freeze(value: object) -> FrozenJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _freeze(value))


def _clear_exception_frames(error: BaseException) -> None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
            current.__traceback__ = None
        next_error = current.__cause__ or current.__context__
        current.__cause__ = None
        current.__context__ = None
        current = next_error
