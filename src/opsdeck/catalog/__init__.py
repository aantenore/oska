"""Loading, validating and resolving the skill catalog."""

from opsdeck.catalog.loader import SkillParseError, discover, discover_all, load_skill
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver, join_phrases
from opsdeck.catalog.validator import (
    CatalogError,
    ValidationIssue,
    validate,
    validate_all,
)

__all__ = [
    "CapabilityResolver",
    "CatalogError",
    "SkillParseError",
    "SkillRegistry",
    "ValidationIssue",
    "discover",
    "discover_all",
    "join_phrases",
    "load_skill",
    "validate",
    "validate_all",
]
