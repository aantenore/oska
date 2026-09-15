"""The conformance document read back: what the levels claim, and what proves it.

The scenario runner scores scenarios and knows nothing about requirement
identifiers, so nothing else in the suite notices when a level list and the
checklist drift apart. This file reads `spec/12-conformance.md` as text: it
expands the ranges the level lists name, collects the checklist rows, and holds
the two to each other.

What it cannot check is whether a proof proves anything. That a named test file
carries an assertion about the requirement beside it, and that a scenario
category really exercises what the level claims of it, are judgments a reader
has to make; this file checks only that every requirement a level names is
claimed once, has a proof, and is not resting on a scenario run by a catalog
the level list is not written about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = REPO_ROOT / "spec" / "12-conformance.md"

LEVELS = "## Levels"
DEPLOYMENT = "### Required of a deployment, not of a level"
UNDECIDED = "### Still undecided"
CHECKLIST = "## Checklist"

# The suite the level lists are written about. A requirement whose only proof is
# a scenario of the other reference catalog is proven by a suite that runs
# other categories, which is not what a level list says about this one. A
# scenario is a document under `examples/`; the runner that plays them is a test
# like any other.
SCENARIO_ROOT = "examples/"
SCENARIOS = "/scenarios/"
LEVEL_SUITE = "examples/ops/scenarios/"

_ID = re.compile(r"`(OSKA-[A-Z]+-\d+[a-z]?)`")
_RANGE = re.compile(r"`(OSKA-[A-Z]+-\d+[a-z]?)`\s+to\s+`(OSKA-[A-Z]+-\d+[a-z]?)`")
_ROW = re.compile(r"^\|\s*(OSKA-[A-Z]+-\d+[a-z]?)\s*\|(.*)\|(.*)\|\s*$")
_PATH = re.compile(r"`([^`]+)`")
_WILDCARD = re.compile(r"OSKA-[A-Z]+-\*")


def _text() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """One section of the document: from its heading to the next of the same depth or above."""
    text = _text()
    start = text.index(heading) + len(heading)
    depth = len(heading) - len(heading.lstrip("#"))
    following = [
        match.start()
        for match in re.finditer(r"^#{1,6} ", text[start:], flags=re.MULTILINE)
        if len(match.group(0)) - 1 <= depth
    ]
    return text[start : start + following[0]] if following else text[start:]


def _checklist() -> dict[str, list[str]]:
    """Every checklist row: the requirement, and the paths its proof cell names."""
    rows: dict[str, list[str]] = {}
    for line in _section(CHECKLIST).splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        assert match.group(1) not in rows, f"{match.group(1)} has two checklist rows"
        rows[match.group(1)] = _PATH.findall(match.group(3))
    return rows


def _declared(paragraph: str, known: list[str]) -> list[str]:
    """Every requirement a paragraph names, with `A` to `B` expanded over the checklist."""
    named: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _RANGE.finditer(paragraph):
        first, last = match.group(1), match.group(2)
        assert first in known, f"{first} is named in the levels and has no checklist row"
        assert last in known, f"{last} is named in the levels and has no checklist row"
        low, high = known.index(first), known.index(last)
        assert low <= high, f"{first} to {last} runs backwards"
        named.extend(known[low : high + 1])
        spans.append(match.span())
    for match in _ID.finditer(paragraph):
        inside = any(start <= match.start() < end for start, end in spans)
        if not inside:
            named.append(match.group(1))
    return named


def _level_lists() -> list[str]:
    """The three paragraphs that say what a level requires, and nothing else."""
    levels = _section(LEVELS).split(DEPLOYMENT)[0]
    return [
        paragraph
        for paragraph in levels.split("\n\n")
        if paragraph.lstrip().startswith("Requires:")
    ]


def _required_by_a_level() -> list[str]:
    """Every requirement the three level lists name, in the order they name it."""
    known = list(_checklist())
    required: list[str] = []
    for paragraph in _level_lists():
        required.extend(_declared(paragraph, known))
    return required


def _named_in(heading: str) -> list[str]:
    """Every requirement one subsection names, ranges expanded."""
    return _declared(_section(heading), list(_checklist()))


def _is_scenario(path: str) -> bool:
    """Whether a proof is a scenario document rather than a test that asserts something."""
    return path.startswith(SCENARIO_ROOT) and SCENARIOS in path


def test_every_requirement_a_level_names_has_a_row_that_proves_it() -> None:
    rows = _checklist()
    missing = [
        requirement
        for requirement in _required_by_a_level()
        if not rows.get(requirement) or rows[requirement] == ["pending"]
    ]
    assert missing == []


def test_a_level_claims_nothing_that_only_another_catalog_proves() -> None:
    """A level list may not rest on a scenario the suite it describes never runs."""
    rows = _checklist()
    borrowed = [
        requirement
        for requirement in _required_by_a_level()
        if rows[requirement]
        and all(_is_scenario(path) for path in rows[requirement])
        and not any(path.startswith(LEVEL_SUITE) for path in rows[requirement])
    ]
    assert borrowed == []


def test_no_level_list_names_a_wildcard() -> None:
    """A wildcard sweeps in whatever is written next; the lists name ranges instead.

    Only the lists are read. The prose around them says what the wildcards used
    to do, and has to be able to write one down to say it.
    """
    assert [found for paragraph in _level_lists() for found in _WILDCARD.findall(paragraph)] == []


def test_every_requirement_is_claimed_exactly_once() -> None:
    """A level, a deployment concern, or an open question: one of the three, and only one."""
    levels = set(_required_by_a_level())
    deployment = set(_named_in(DEPLOYMENT))
    undecided = set(_named_in(UNDECIDED))
    rows = set(_checklist())

    assert levels & deployment == set()
    assert levels & undecided == set()
    assert deployment & undecided == set()
    assert levels | deployment | undecided == rows


def test_a_level_requires_a_requirement_once() -> None:
    """A range that overlaps another says the same thing twice and hides what changed."""
    required = _required_by_a_level()
    repeated = sorted({name for name in required if required.count(name) > 1})
    assert repeated == []


@pytest.mark.parametrize("requirement", sorted(_checklist()))
def test_every_proof_the_checklist_names_is_there(requirement: str) -> None:
    """A proof that has moved or been renamed is a checklist row that proves nothing."""
    named = [path for path in _checklist()[requirement] if "/" in path]
    missing = [path for path in named if not (REPO_ROOT / path).exists()]
    assert missing == []
