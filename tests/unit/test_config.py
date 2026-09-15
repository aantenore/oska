"""Configuration: the tunables are read from it, and nothing is written in logic."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import (
    DiscoveryConfig,
    FastPathConfig,
    FastPathRoutes,
    RouterRules,
    RuntimeConfig,
    load_runtime_config,
)
from opsdeck.contracts.common import TurnKind
from opsdeck.index.discovery import Discovery
from opsdeck.index.keyword import KeywordIndex
from opsdeck.router import FastPath, RoutingContext, RuleRouter

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "opsdeck"
SCANNED = ("index", "router", "governance", "engine")

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"

# Whole numbers a rule of arithmetic needs, not values an operator would tune.
ALLOWED_NUMBERS = frozenset({0, 1, 2, -1})
PATH_LIKE = re.compile(r"/|\.(json|jsonl|sqlite|db|md|ya?ml|txt)$")
# A model name has the shape of a product name: letters, a dash, then a version
# or a size word. Matching the shape keeps the guard free of vendor names.
MODEL_LIKE = re.compile(
    r"(?i)\b[a-z]{2,}-(?:\d+(?:\.\d+)+[a-z]*|\d+[a-z]+|mini|small|large|instruct|embedding)\b"
)

# Literals the scan reports that are not tunable values, by module path, each
# with the reason it is not one.
ALLOWED_LITERALS: dict[str, set[str]] = {
    # The name of the file the improvement job writes and an index reads back.
    # It is one artifact named in one place so that both layers agree, not a
    # value an operator would tune.
    "index/base.py": {"learned-examples.json"},
}


def _discovery(registry: SkillRegistry, **overrides: object) -> Discovery:
    return Discovery(registry, KeywordIndex(), DiscoveryConfig(**overrides))


def test_discovery_reads_its_minimum_score_from_the_configuration(
    registry: SkillRegistry,
) -> None:
    generous = _discovery(registry, min_score=0.01).select(RESTART_REQUEST)
    strict = _discovery(registry, min_score=0.99).select(RESTART_REQUEST)

    assert generous.chosen == "runbook.service_restart"
    assert strict.chosen is None
    assert [item.skill_id for item in strict.ranked] == [
        item.skill_id for item in generous.ranked
    ]


def test_discovery_reads_its_tie_margin_from_the_configuration(
    registry: SkillRegistry,
) -> None:
    narrow = _discovery(registry, min_score=0.01, tie_margin=0.0).select(RESTART_REQUEST)
    wide = _discovery(registry, min_score=0.01, tie_margin=1.0).select(RESTART_REQUEST)

    assert narrow.tie is False
    assert wide.tie is True
    assert wide.chosen == narrow.chosen


def test_the_fast_path_reads_its_threshold_from_the_configuration(
    registry: SkillRegistry,
) -> None:
    def _fast_path(threshold: float) -> FastPath:
        return FastPath(
            KeywordIndex,
            registry,
            FastPathConfig(threshold=threshold, routes=FastPathRoutes(greeting=["hello"])),
        )

    context = RoutingContext()

    low = _fast_path(0.2).route("restart the checkout service", context)
    high = _fast_path(1.01).route("restart the checkout service", context)

    assert low is not None
    assert low.kind is TurnKind.new_task
    assert low.runbook_id == "runbook.service_restart"
    assert high is None


def test_the_rules_read_the_words_they_answer_to_from_the_configuration() -> None:
    waiting = RoutingContext(pending_approval=True)

    configured = RuleRouter(RouterRules(approve_tokens=["by all means"])).route(
        "by all means", waiting
    )
    unconfigured = RuleRouter(RouterRules(approve_tokens=["yes"])).route(
        "by all means", waiting
    )

    assert configured is not None
    assert configured.kind is TurnKind.approve
    assert unconfigured is None


def test_the_configuration_file_carries_what_the_defaults_declare(repo_root: Path) -> None:
    configured = load_runtime_config(repo_root / "configs" / "runtime.json")

    assert configured.model_fields_set >= set(RuntimeConfig().model_dump())
    assert configured.discovery.tie_margin == 0.05
    assert configured.router.fast_path.threshold == 0.75
    assert "yes" in configured.router.rules.approve_tokens


def _literals(path: Path) -> list[tuple[int, object]]:
    """Every literal of a file, leaving out the docstrings, which are prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: list[tuple[int, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in prose:
            found.append((node.lineno, node.value))
    return found


def test_no_threshold_limit_path_or_model_name_is_written_into_logic() -> None:
    written: list[str] = []
    for package in SCANNED:
        for path in sorted((PACKAGE_ROOT / package).rglob("*.py")):
            name = str(path.relative_to(PACKAGE_ROOT))
            allowed = ALLOWED_LITERALS.get(name, set())
            for line, value in _literals(path):
                if isinstance(value, bool) or value is None:
                    continue
                if isinstance(value, int | float) and value not in ALLOWED_NUMBERS:
                    if str(value) not in allowed:
                        written.append(f"{name}:{line}: {value!r}")
                if isinstance(value, str) and (PATH_LIKE.search(value) or
                                               MODEL_LIKE.search(value)):
                    if value not in allowed:
                        written.append(f"{name}:{line}: {value!r}")
    assert written == []
