"""Decides whether a declared write may run, and says why.

A procedure that writes carries a policy file next to it. Without one nothing
is allowed: the absence of a rule is not permission. Reads are governed by the
catalog alone, so they never reach the rules here, and an operation the catalog
does not declare at all is refused rather than taken for a read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.contracts.common import PolicyDecision
from opsdeck.contracts.policy import CONDITION_OPERATORS, Policy, PolicyRule
from opsdeck.contracts.skill import OperationSpec, Skill
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.ledger import WriteLedger

_MISSING = object()

_READ_REASON = "read operation"
_NO_POLICY_REASON = "the procedure declares no policy, so nothing may be written"
_NO_RULE_REASON = "no rule covers this operation"
_UNDECLARED_REASON = "undeclared operation"


class PolicyError(Exception):
    """A policy file is missing or does not parse."""


class PolicyEngine:
    """Applies a procedure's policy to one operation."""

    def __init__(self, registry: SkillRegistry, ledger: WriteLedger) -> None:
        self._registry = registry
        self._ledger = ledger
        self._capabilities = CapabilityRegistry(registry)
        self._cache: dict[tuple[str, str], Policy | None] = {}

    def load(self, skill: Skill) -> Policy | None:
        """Read the policy declared by a procedure, resolved against its own file."""
        reference = skill.frontmatter.policy
        if not reference:
            return None
        cache_key = (skill.id, skill.fingerprint)
        if cache_key in self._cache:
            return self._cache[cache_key]
        path = Path(reference)
        if not path.is_absolute():
            path = skill.path.parent / path
        try:
            with path.open(encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyError(f"{skill.id}: policy {path} could not be read: {exc}") from exc
        policy = Policy.model_validate(document)
        self._cache[cache_key] = policy
        return policy

    def evaluate(
        self,
        skill: Skill,
        operation: str,
        payload: dict,
        context: dict | None = None,
        session_id: str = "",
    ) -> PolicyDecision:
        """Decide what happens to one operation of this procedure."""
        spec = self._declared(skill, operation)
        if spec is None:
            return PolicyDecision(effect="deny", rule=None, reason=_UNDECLARED_REASON)
        if not spec.write:
            return PolicyDecision(effect="allow", rule=None, reason=_READ_REASON)
        policy = self.load(skill)
        if policy is None:
            return PolicyDecision(effect="deny", rule=None, reason=_NO_POLICY_REASON)
        subject = {**payload, **(context or {})}
        for rule in policy.rules:
            if not _matches(rule, operation, subject):
                continue
            if rule.effect != "deny" and rule.max_per_session is not None:
                used = self._ledger.count(session_id, operation)
                if used >= rule.max_per_session:
                    return PolicyDecision(
                        effect="deny",
                        rule=rule,
                        reason=(
                            f"this session has already reached the limit of "
                            f"{rule.max_per_session} for {operation}"
                        ),
                    )
            return PolicyDecision(effect=rule.effect, rule=rule, reason=rule.reason)
        return PolicyDecision(effect=policy.default, rule=None, reason=_NO_RULE_REASON)

    def explain(self, decision: PolicyDecision) -> str:
        """One sentence naming the effect and the reason behind it, for the reply."""
        wording = {
            "allow": "Allowed",
            "deny": "Refused",
            "require_second_approval": "Allowed with a second approval",
        }[decision.effect]
        if decision.reason:
            return f"{wording}: {decision.reason}"
        return f"{wording}."

    def _declared(self, skill: Skill, operation: str) -> OperationSpec | None:
        """The catalog's declaration of this operation, from the integrations in reach.

        An operation nobody declares is not a read: it is something the catalog
        has no account of, so the decision is a refusal.
        """
        for integration_id in skill.frontmatter.integrations:
            spec = self._capabilities.operation_spec(integration_id, operation)
            if spec is not None:
                return spec
        return self._capabilities.spec_for(operation)


def _matches(rule: PolicyRule, operation: str, subject: dict) -> bool:
    if rule.operation not in ("*", operation):
        return False
    return all(_holds(subject, path, condition) for path, condition in rule.when.items())


def _holds(subject: dict, path: str, condition: dict[str, Any]) -> bool:
    actual = _lookup(subject, path)
    if actual is _MISSING:
        return False
    return all(_compare(actual, operator, expected) for operator, expected in condition.items())


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    try:
        match operator:
            case "eq":
                return bool(actual == expected)
            case "ne":
                return bool(actual != expected)
            case "in":
                return actual in expected
            case "not_in":
                return actual not in expected
            case "lt":
                return bool(actual < expected)
            case "lte":
                return bool(actual <= expected)
            case "gt":
                return bool(actual > expected)
            case "gte":
                return bool(actual >= expected)
            case _:
                raise ValueError(
                    f"unknown condition operator {operator!r}; "
                    f"expected one of {', '.join(CONDITION_OPERATORS)}"
                )
    except TypeError:
        # Values of different shapes never satisfy an ordering condition.
        return False


def _lookup(subject: Any, path: str) -> Any:
    current: Any = subject
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return _MISSING
    return current
