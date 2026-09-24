"""A field only a person can supply is declared, refused elsewhere, and asked for.

These tests cover the two halves the plan validation tests do not reach: that the
declaration belongs to a procedure alone, and that the prompt handed to the model
both states the rule and names the fields it applies to.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oska.catalog.registry import SkillRegistry
from oska.contracts.skill import SkillFrontmatter
from oska.planner.prompts import planner_system


def _frontmatter(skill_id: str, kind: str, **extra: object) -> SkillFrontmatter:
    return SkillFrontmatter(
        id=skill_id,
        type=kind,
        version=1,
        description="A declaration long enough to satisfy the contract.",
        **extra,
    )


@pytest.mark.parametrize(
    ("skill_id", "kind"),
    [("entity.thing", "entity"), ("integration.system", "integration")],
)
def test_only_a_procedure_declares_a_human_input(skill_id: str, kind: str) -> None:
    with pytest.raises(ValidationError) as raised:
        _frontmatter(skill_id, kind, human_inputs=["note"])
    assert "human inputs" in str(raised.value)


def test_a_procedure_may_declare_a_human_input() -> None:
    frontmatter = _frontmatter(
        "runbook.take_a_note",
        "runbook",
        triggers=["take a note", "write this down", "record what I say"],
        runtime={"action": "take a note"},
        human_inputs=["note"],
    )
    assert frontmatter.human_inputs == ["note"]


def test_the_prompt_states_the_rule_and_names_the_fields(registry: SkillRegistry) -> None:
    hydrated = registry.closure("runbook.incident_note")
    declared = [
        field
        for skill in [hydrated.runbook, *hydrated.runbooks]
        for field in skill.frontmatter.human_inputs
    ]
    prompt = planner_system(hydrated, "scope")
    assert declared, "the reference catalog declares a human input on this procedure"
    for field in declared:
        assert field in prompt
    assert "human input" in prompt.lower()
    assert "missing" in prompt
