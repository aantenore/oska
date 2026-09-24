"""Turning a finished session into a candidate procedure, and keeping the file.

A draft records what the session actually did: the write operations it ran, the
integrations they belong to and the inputs they carried. With a model provider
the wording comes from the conversation; without one it comes from the same
recorded operations. Either way the draft is conservative, and it is a draft:
nothing here moves a proposal forward.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from oska.catalog.registry import SkillRegistry
from oska.contracts.common import Message
from oska.contracts.proposal import (
    ProposalState,
    ProposalTransition,
    SkillProposal,
)
from oska.contracts.receipt import WriteReceipt
from oska.contracts.skill import MIN_TRIGGERS, SkillType
from oska.learning import quality
from oska.planner.provider import ModelProvider

POLICY_FILENAME = "./policy.json"
MAX_WRITES_PER_SESSION = 1

DRAFT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "description",
        "triggers",
        "action",
        "purpose",
        "preconditions",
        "steps",
        "verification",
        "rollback",
    ],
    "properties": {
        "description": {"type": "string", "minLength": 20, "maxLength": 200},
        "triggers": {
            "type": "array",
            "minItems": MIN_TRIGGERS,
            "items": {"type": "string", "minLength": 1},
        },
        "action": {"type": "string", "minLength": 1},
        "purpose": {"type": "string", "minLength": 1},
        "preconditions": {"type": "string"},
        "steps": {"type": "string", "minLength": 1},
        "verification": {"type": "string", "minLength": 1},
        "rollback": {"type": "string"},
    },
}

DRAFT_RULES = (
    "Describe only what the recorded operations and the conversation show.",
    "Never invent an operation, an identifier or a value.",
    "Triggers are what a person would say to ask for this procedure; give at least"
    f" {MIN_TRIGGERS}, all different from each other.",
    "Verification says what is read back to confirm the result; the response to a"
    " write is not evidence.",
    "Plain sentences. No praise, no promises.",
)

_SECTION_TITLES = (
    ("purpose", "Purpose"),
    ("preconditions", "Preconditions"),
    ("steps", "Steps"),
    ("verification", "Verification"),
    ("rollback", "Rollback"),
)
_MODEL_TEXT_KEYS = (
    "description",
    "action",
    "purpose",
    "preconditions",
    "steps",
    "verification",
    "rollback",
)
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_NAME_ALLOWED = re.compile(r"[^a-z0-9]+")


class ProposalStore:
    """Proposals kept as one JSON file each, named after the proposal id."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Where the files are kept."""
        return self._directory

    def save(self, proposal: SkillProposal) -> Path:
        """Write a proposal, replacing any earlier copy. Returns its path."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(proposal.proposal_id)
        payload = proposal.model_dump(mode="json")
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def get(self, proposal_id: str) -> SkillProposal:
        """Read one proposal. Raises `KeyError` when it is not there."""
        path = self._path(proposal_id)
        if not path.is_file():
            raise KeyError(f"unknown proposal: {proposal_id}")
        return SkillProposal.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, state: ProposalState | str | None = None) -> list[SkillProposal]:
        """Every proposal, newest state filter applied, sorted by id."""
        wanted = ProposalState(state) if state is not None else None
        proposals: list[SkillProposal] = []
        if not self._directory.is_dir():
            return proposals
        for path in sorted(self._directory.glob("*.json")):
            proposal = SkillProposal.model_validate_json(path.read_text(encoding="utf-8"))
            if wanted is None or proposal.state is wanted:
                proposals.append(proposal)
        return proposals

    def delete(self, proposal_id: str) -> None:
        """Remove one proposal. Raises `KeyError` when it is not there."""
        path = self._path(proposal_id)
        if not path.is_file():
            raise KeyError(f"unknown proposal: {proposal_id}")
        path.unlink()

    def _path(self, proposal_id: str) -> Path:
        name = Path(proposal_id).name
        if not name or name != proposal_id:
            raise KeyError(f"unusable proposal id: {proposal_id!r}")
        return self._directory / f"{name}.json"


def draft_from_session(
    session_id: str,
    history: list[Message],
    receipts: list[WriteReceipt],
    registry: SkillRegistry,
    provider: ModelProvider | None,
    *,
    runbook_id_hint: str | None = None,
    schema_dir: Path | None = None,
) -> SkillProposal:
    """Build a draft procedure from what one session wrote."""
    skill_id = _skill_id(runbook_id_hint, receipts, session_id)
    integrations = _integrations(receipts, registry)
    entities = _entities(integrations, registry)
    writes = _distinct(receipt.operation for receipt in receipts)
    fields = _payload_fields(receipts)
    content = _template(skill_id, writes, fields, registry)
    if provider is not None:
        content = _model_content(provider, history, receipts, content)
    markdown = _render(skill_id, content, entities, integrations, writes)
    policy = build_policy(skill_id, writes)
    report = quality.check(markdown, registry, policy=policy, schema_dir=schema_dir)
    return SkillProposal(
        proposal_id=str(uuid.uuid4()),
        state=ProposalState.draft,
        source_session_id=session_id,
        skill_markdown=markdown,
        policy=policy,
        quality=report,
        history=[
            ProposalTransition(
                state=ProposalState.draft,
                at=datetime.now(UTC),
                by=session_id,
                note="drafted from the session",
            )
        ],
    )


def build_policy(skill_id: str, writes: Sequence[str]) -> dict:
    """A policy that asks for a second approval and allows one write per session."""
    return {
        "skill_id": skill_id,
        "default": "deny",
        "rules": [
            {
                "operation": operation,
                "effect": "require_second_approval",
                "max_per_session": MAX_WRITES_PER_SESSION,
                "reason": "Recorded from a session and not reviewed yet.",
            }
            for operation in writes
        ],
    }


def split_skill(markdown: str) -> tuple[dict, str]:
    """Return the frontmatter mapping and the body of a skill file."""
    match = _FRONTMATTER.match(markdown)
    if match is None:
        raise ValueError("no YAML frontmatter between --- lines")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data, markdown[match.end() :]


def render_skill(frontmatter: dict, body: str) -> str:
    """Join a frontmatter mapping and a body back into a skill file."""
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{header}---\n{body}"


def _render(
    skill_id: str,
    content: dict[str, object],
    entities: list[str],
    integrations: list[str],
    writes: list[str],
) -> str:
    frontmatter: dict = {
        "id": skill_id,
        "type": SkillType.runbook.value,
        "version": 1,
        "status": "draft",
        "description": content["description"],
        "triggers": list(content["triggers"]),  # type: ignore[arg-type]
    }
    if entities:
        frontmatter["entities"] = entities
    if integrations:
        frontmatter["integrations"] = integrations
    frontmatter["capabilities"] = {"writes": writes}
    frontmatter["runtime"] = {"action": content["action"]}
    frontmatter["policy"] = POLICY_FILENAME
    parts = []
    for key, title in _SECTION_TITLES:
        text = str(content.get(key, "")).strip()
        if text:
            parts.append(f"## {title}\n\n{text}\n")
    return render_skill(frontmatter, "\n" + "\n".join(parts))


def _template(
    skill_id: str,
    writes: list[str],
    fields: list[str],
    registry: SkillRegistry,
) -> dict[str, object]:
    phrase = skill_id.split(".", 1)[1].replace("_", " ")
    listed = ", ".join(writes) if writes else "no write operation"
    inputs = ", ".join(f"`{field}`" for field in fields) if fields else "no recorded input"
    steps = [
        f"{position}. Call `{name}` with the recorded inputs."
        for position, name in enumerate(writes, start=1)
    ]
    steps.append(f"{len(steps) + 1}. Read the result back and compare it with what was expected.")
    return {
        "description": _clamp(
            f"Repeats {listed} as one procedure, recorded from a session that ran it."
        ),
        "triggers": _triggers(phrase, writes, fields, registry),
        "action": f"run {phrase}",
        "purpose": (
            f"Carries out {listed} in the order a session already ran them. It is a"
            " record of what was done, not an explanation of when it is the right"
            " thing to do; a reviewer adds that before it is published."
        ),
        "preconditions": f"- The inputs are known: {inputs}.",
        "steps": "\n".join(steps),
        "verification": (
            "Each write is confirmed by the read-back its integration declares. The"
            " response to a write says nothing about the outcome; if the read-back"
            " disagrees, report the operation as unverified and do not repeat it."
        ),
        "rollback": (
            "The session this came from recorded no way to undo these operations."
            " A reviewer states one here, or states that there is none."
        ),
    }


def _triggers(
    phrase: str,
    writes: list[str],
    fields: list[str],
    registry: SkillRegistry,
) -> list[str]:
    candidates = [phrase, f"run {phrase}", f"start the {phrase} procedure"]
    for name in writes:
        candidates.append(f"run {name}")
    for field in fields:
        candidates.append(f"{phrase} for a given {field}")
    taken = {
        trigger.strip().lower()
        for skill in registry.published()
        if skill.type is SkillType.runbook
        for trigger in skill.frontmatter.triggers
    }
    return _distinct(
        candidate for candidate in candidates if candidate.strip().lower() not in taken
    )


def _model_content(
    provider: ModelProvider,
    history: list[Message],
    receipts: list[WriteReceipt],
    template: dict[str, object],
) -> dict[str, object]:
    system = "\n\n".join(
        [
            "You write the file that records a procedure a session has just carried out.",
            "Recorded operations:\n" + _receipt_lines(receipts),
            "Rules:\n" + "\n".join(f"- {rule}" for rule in DRAFT_RULES),
        ]
    )
    message = Message(role="user", content=_transcript(history, receipts))
    answer = provider.complete_json(system, [message], DRAFT_SCHEMA)
    content = dict(template)
    for key in _MODEL_TEXT_KEYS:
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            content[key] = _clamp(value.strip()) if key == "description" else value.strip()
    triggers = answer.get("triggers")
    if isinstance(triggers, list):
        written = _distinct(item.strip() for item in triggers if isinstance(item, str))
        if len(written) >= MIN_TRIGGERS:
            content["triggers"] = written
    return content


def _transcript(history: list[Message], receipts: list[WriteReceipt]) -> str:
    lines = [f"{message.role}: {message.content}" for message in history]
    return "\n".join(
        [
            "Conversation:",
            "\n".join(lines) if lines else "(nothing was recorded)",
            "",
            "Operations:",
            _receipt_lines(receipts),
        ]
    )


def _receipt_lines(receipts: list[WriteReceipt]) -> str:
    if not receipts:
        return "(none)"
    return "\n".join(
        f"- {receipt.integration_id} {receipt.operation} "
        f"{json.dumps(receipt.payload, sort_keys=True)} -> {receipt.status}"
        for receipt in receipts
    )


def _skill_id(hint: str | None, receipts: list[WriteReceipt], session_id: str) -> str:
    if hint:
        name = hint.split(".", 1)[1] if hint.startswith(f"{SkillType.runbook.value}.") else hint
    elif receipts:
        name = receipts[0].operation
    else:
        name = f"session {session_id}"
    return f"{SkillType.runbook.value}.{_slug(name)}"


def _slug(value: str) -> str:
    name = _NAME_ALLOWED.sub("_", value.strip().lower()).strip("_")
    return name or "procedure"


def _integrations(receipts: list[WriteReceipt], registry: SkillRegistry) -> list[str]:
    found = []
    for reference in _distinct(receipt.integration_id for receipt in receipts):
        skill = registry.get(reference)
        if skill is not None and skill.type is SkillType.integration:
            found.append(reference)
    return found


def _entities(integrations: list[str], registry: SkillRegistry) -> list[str]:
    names: list[str] = []
    for reference in integrations:
        skill = registry.get(reference)
        if skill is None:
            continue
        names.extend(skill.frontmatter.entities)
    return _distinct(names)


def _payload_fields(receipts: list[WriteReceipt]) -> list[str]:
    return _distinct(field for receipt in receipts for field in receipt.payload)


def _distinct(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _clamp(description: str) -> str:
    if len(description) <= quality.DESCRIPTION_MAX:
        return description
    return description[: quality.DESCRIPTION_MAX - 1].rstrip() + "."
