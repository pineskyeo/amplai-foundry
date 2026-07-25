"""Source ingestion and verification results."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class IngestionResult:
    status: str
    source_id: str
    path: str
    content_sha256: str
    duplicate_of: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    source_id: str
    path: str
    valid: bool
    content_sha256: str
    expected_sha256: str
