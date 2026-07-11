"""Closed vocabularies used by the memory domain."""

from enum import StrEnum


class MemoryKind(StrEnum):
    """Kinds of reviewed, long-lived project knowledge."""

    SOURCE = "source"
    CONCEPT = "concept"
    PRINCIPLE = "principle"
    DECISION = "decision"
    QUESTION = "question"
    ARCHITECTURE = "architecture"
    EXPERIMENT = "experiment"
    MAP = "map"


class MemoryStatus(StrEnum):
    """Lifecycle states of official knowledge."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"
    MERGED = "merged"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class RelationType(StrEnum):
    """Directed relationships between memory objects."""

    RELATED_TO = "related_to"
    DEPENDS_ON = "depends_on"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    IMPLEMENTS = "implements"
    DERIVED_FROM = "derived_from"
