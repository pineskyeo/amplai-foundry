"""Intent-driven, project-safe knowledge intake."""

from typing import TYPE_CHECKING

from amplai_foundry.intake.models import IntakeResult, IntentRequest

if TYPE_CHECKING:
    from amplai_foundry.intake.service import KnowledgeIntakeService

__all__ = ["IntakeResult", "IntentRequest", "KnowledgeIntakeService"]


def __getattr__(name: str) -> object:
    if name == "KnowledgeIntakeService":
        from amplai_foundry.intake.service import KnowledgeIntakeService

        return KnowledgeIntakeService
    raise AttributeError(name)
