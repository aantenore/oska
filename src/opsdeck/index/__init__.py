"""Ranking the catalog against a request."""

from opsdeck.index.base import SkillIndex
from opsdeck.index.discovery import Discovery
from opsdeck.index.embedding import EmbeddingIndex
from opsdeck.index.keyword import KeywordIndex

__all__ = ["Discovery", "EmbeddingIndex", "KeywordIndex", "SkillIndex"]
