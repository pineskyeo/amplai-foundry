"""Typed metadata for immutable ingested source notes."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SourceMetadata(BaseModel):
    """Source-specific provenance and integrity metadata."""

    model_config = ConfigDict(extra="forbid")

    source_type: NonEmptyString
    content_sha256: Sha256
    normalized_sha256: Sha256
    original_filename: NonEmptyString | None = None
    media_type: NonEmptyString
    ingested_at: datetime
    created_by: NonEmptyString
