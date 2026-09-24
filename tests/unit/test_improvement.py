"""The autonomy ladder, level by level, and the ceiling over all of it."""

from __future__ import annotations

import ast
import builtins
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oska.catalog.registry import SkillRegistry
from oska.cli.commands_improve import app
from oska.config import DiscoveryConfig, ImprovementConfig, Settings, load_runtime_config
from oska.contracts.common import RankedSkill
from oska.contracts.proposal import ProposalState, SkillProposal
from oska.contracts.receipt import VerificationStatus
from oska.governance.ledger import WriteLedger
from oska.improvement.evaluation import TIE, RoutingCase, load_cases, run_evaluation
from oska.improvement.feedback import (
    FeedbackStore,
    TurnFeedback,
    feedback_root,
    latest,
    scores,
)
from oska.improvement.gaps import find_gaps, unmatched
from oska.improvement.job import run_job
from oska.improvement.learned import (
    LearnedPhrase,
    learn,
    learned_path,
    load_redactions,
    read_learned,
    redact,
    revert,
    write_learned,
)
from oska.improvement.preferences import (
    Preference,
    classify_comment,
    learn_preferences,
    preferences_root,
    read_preferences,
    revert_preferences,
    write_preferences,
)
from oska.improvement.report import read_report, render_json, render_text
from oska.improvement.signals import TurnSignal, load_signals
from oska.improvement.tuning import rank_cases, tune, write_overrides
from oska.index.base import LearnedPhrases
from oska.index.discovery import Discovery
from oska.index.keyword import KeywordIndex
from oska.learning.proposals import ProposalStore
from oska.ontology.graph import OntologyGraph
from oska.planner.scripted import hash_bag_vector

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "oska"
IMPROVEMENT = PACKAGE_ROOT / "improvement"
COMMANDS = PACKAGE_ROOT / "cli" / "commands_improve.py"

MOMENT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
BEYOND_DRAFT = ("submitted", "approved", "published")


# --- fixtures and helpers -------------------------------------------------


@pytest.fixture
def evaluation_dir(repo_root: Path) -> Path:
    """Where the labeled set and the redaction patterns are kept."""
    return repo_root / "examples" / "ops" / "evaluation"


def event(kind: str, session: str, turn: str, data: dict, minute: int = 0) -> str:
    """One audit line, stamped a given number of minutes after the moment."""
    return json.dumps(
        {
            "kind": kind,
            "session_id": session,
            "turn_id": turn,
            "at": (MOMENT + timedelta(minutes=minute)).isoformat(),
            "data": data,
        }
    )


def write_audit(path: Path, lines: list[str]) -> Path:
    """Write an audit trail, with one unreadable line to prove it is skipped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*lines, "{not json"]) + "\n", encoding="utf-8")
    return path


def signal(text: str, skill: str | None, **overrides: object) -> TurnSignal:
    """A signal with the fields a test cares about and defaults for the rest."""
    values: dict = {
        "session_id": "session-1",
        "turn_id": text[:8],
        "text": text,
        "language": "en",
        "route_source": "fast_path",
        "chosen_runbook": skill,
        "score": 0.5,
        "outcomes": ["service.status"] if skill else [],
        "at": MOMENT,
    }
    values.update(overrides)
    return TurnSignal.model_validate(values)


def improvement_config(**overrides: object) -> ImprovementConfig:
    """An improvement configuration with the defaults the runtime declares."""
    return ImprovementConfig.model_validate(overrides)


# --- level 0: signals -----------------------------------------------------


def test_signals_are_read_from_the_trail_and_the_ledger(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    confirmed = ledger.record(
        ledger.new_receipt("session-1", "key-1", "integration.alerting", "alert.silence", {})
    )
    ledger.update(confirmed.receipt_id, verification=VerificationStatus.verified)
    claimed = ledger.record(
        ledger.new_receipt("session-1", "key-2", "integration.alerting", "alert.silence", {})
    )
    audit = write_audit(
        tmp_path / "audit.jsonl",
        [
            event("turn", "session-1", "t1", {"turn": 1, "text": "silenzia l allarme"}),
            event(
                "route",
                "session-1",
                "t1",
                {"kind": "new_task", "source": "fast_path", "language": "it", "confidence": 0.9},
            ),
            event(
                "discovery",
                "session-1",
                "t1",
                {
                    "ranked": [{"skill_id": "runbook.alert_silence", "score": 0.42}],
                    "chosen": "runbook.alert_silence",
                    "tie": False,
                },
            ),
            event(
                "execute",
                "session-1",
                "t1",
                {"operation": "alert.silence", "stage": "call", "ok": True, "write": True},
            ),
            event(
                "verify",
                "session-1",
                "t1",
                {"receipt_id": confirmed.receipt_id, "verification": "verified"},
            ),
            event("turn", "session-1", "t2", {"turn": 2, "text": "and note it"}, minute=1),
            event(
                "verify",
                "session-1",
                "t2",
                {"receipt_id": claimed.receipt_id, "verification": "verified"},
                minute=1,
            ),
            event("route", "session-2", "t3", {"kind": "off_scope", "source": "model"}, minute=2),
        ],
    )

    signals = load_signals(audit, ledger)

    assert [item.turn_id for item in signals] == ["t1", "t2"]
    first = signals[0]
    assert first.text == "silenzia l allarme"
    assert first.language == "it"
    assert first.route_source == "fast_path"
    assert first.chosen_runbook == "runbook.alert_silence"
    assert first.score == pytest.approx(0.42)
    assert first.outcomes == ["alert.silence"]
    assert first.verified_write is True
    assert first.grounded is True
    assert signals[1].verified_write is False
    assert load_signals(audit)[1].verified_write is True


def test_a_turn_that_reached_nothing_is_not_grounded(tmp_path: Path) -> None:
    audit = write_audit(
        tmp_path / "audit.jsonl",
        [
            event("turn", "session-3", "t9", {"turn": 1, "text": "scale the checkout service"}),
            event("route", "session-3", "t9", {"kind": "off_scope", "source": "model"}),
            event("discovery", "session-3", "t9", {"ranked": [], "chosen": None, "tie": False}),
        ],
    )

    signals = load_signals(audit)

    assert len(signals) == 1
    assert signals[0].chosen_runbook is None
    assert signals[0].grounded is False
    assert unmatched(signals, 0.2) == signals


# --- level 1: learned phrasings -------------------------------------------


def test_learned_phrases_are_deduplicated_redacted_and_capped(
    tmp_path: Path, registry: SkillRegistry, evaluation_dir: Path
) -> None:
    signals = [
        signal("riavvia il servizio", "runbook.service_restart", language="it"),
        signal("please restart checkout, it stopped answering", "runbook.service_restart"),
        signal("please restart checkout, it stopped responding", "runbook.service_restart"),
        signal("checkout fell over after ticket ABC-1234 on 10.0.0.5", "runbook.service_restart"),
        signal("bring the payments worker back up", "runbook.service_restart"),
        signal("no procedure was reached here", None),
    ]

    learned = learn(
        signals,
        registry,
        improvement_config(max_phrases_per_skill=3),
        redactions_path=evaluation_dir / "redactions.json",
    )

    kept = [phrase.text for phrase in learned["runbook.service_restart"]]
    assert "riavvia il servizio" not in kept
    assert len(kept) == 3
    assert sum(1 for text in kept if text.startswith("please restart checkout")) == 1
    redacted = next(text for text in kept if text.startswith("checkout fell over"))
    assert "abc-1234" not in redacted
    assert "10.0.0.5" not in redacted
    assert "reference" in redacted and "address" in redacted
    assert list(learned) == ["runbook.service_restart"]


def test_declared_patterns_are_what_gets_redacted(evaluation_dir: Path) -> None:
    redactions = load_redactions(evaluation_dir / "redactions.json")

    text = redact("mail ops@example.com about CHG-12 at https://example.com/a", redactions)

    assert "ops@example.com" not in text
    assert "CHG-12" not in text
    assert "https://example.com/a" not in text


def test_learned_files_are_written_and_reverted(tmp_path: Path) -> None:
    root = tmp_path / "learned"
    phrases = [LearnedPhrase(text="bounce the worker", language="en", session_id="session-1")]

    path = write_learned(root, "runbook.service_restart", phrases, at=MOMENT)

    assert path == learned_path(root, "runbook.service_restart")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["learned_at"] == MOMENT.isoformat()
    assert document["phrases"][0]["text"] == "bounce the worker"
    assert read_learned(root, "runbook.service_restart") == phrases
    assert revert(root, "runbook.service_restart") is True
    assert not path.exists()
    assert read_learned(root, "runbook.service_restart") == []
    assert revert(root, "runbook.service_restart") is False


LEARNED_REQUEST = "our order flow is frozen, unstick it"


def test_what_is_learned_is_ranked_and_reverting_gives_the_ranking_back(
    tmp_path: Path, registry: SkillRegistry, repo_root: Path
) -> None:
    """Level 1 changes what the runtime does, and deleting one file changes it back."""
    root = tmp_path / "state" / "learned"
    discovery = Discovery(
        registry,
        KeywordIndex(),
        load_runtime_config(repo_root / "configs" / "runtime.json").discovery,
        None,
        LearnedPhrases(root),
    )
    before = discovery.select(LEARNED_REQUEST)

    write_learned(
        root,
        "runbook.service_restart",
        [LearnedPhrase(text=LEARNED_REQUEST, language="en", session_id="session-1")],
    )
    learned = discovery.select(LEARNED_REQUEST)
    reverted_file = revert(root, "runbook.service_restart")
    after = discovery.select(LEARNED_REQUEST)

    assert before.chosen is None
    assert learned.chosen == "runbook.service_restart"
    assert learned.ranked[0].matched_on == f"learned en: {LEARNED_REQUEST}"
    assert reverted_file is True
    assert after.chosen is None
    assert after.model_dump() == before.model_dump()


def test_a_learned_phrase_is_written_under_the_state_directory_and_never_in_the_catalog(
    tmp_path: Path, registry: SkillRegistry
) -> None:
    root = tmp_path / "state" / "learned"
    skill = registry.require("runbook.service_restart")
    before = _snapshot(Path(skill.path).parent)

    path = write_learned(
        root,
        skill.id,
        [LearnedPhrase(text=LEARNED_REQUEST, language="en", session_id="session-1")],
    )

    assert path.is_relative_to(root)
    assert not path.is_relative_to(Path(skill.path).parent)
    assert _snapshot(Path(skill.path).parent) == before


# --- level 1: tuning ------------------------------------------------------


def tuning_cases() -> tuple[list[RoutingCase], list[list[RankedSkill]]]:
    """Two cases: one decided from far above the floor, one only from below 0.2."""
    cases = [
        RoutingCase(text="clearly the first", expected="runbook.first"),
        RoutingCase(text="faintly the second", expected="runbook.second"),
    ]
    ranked = [
        [
            RankedSkill(skill_id="runbook.first", score=0.8),
            RankedSkill(skill_id="runbook.other", score=0.1),
        ],
        [
            RankedSkill(skill_id="runbook.second", score=0.2),
            RankedSkill(skill_id="runbook.other", score=0.05),
        ],
    ]
    return cases, ranked


def test_tuning_takes_a_better_pair_inside_the_bounds() -> None:
    cases, ranked = tuning_cases()

    tuning = tune(
        cases,
        ranked,
        DiscoveryConfig(min_score=0.3, tie_margin=0.05),
        {"min_score": (0.1, 0.5), "tie_margin": (0.02, 0.15)},
    )

    assert tuning.before == {"min_score": 0.3, "tie_margin": 0.05}
    assert tuning.after["min_score"] <= 0.2
    assert tuning.moved is True
    assert tuning.precision == 1.0
    assert tuning.decisive_rate == 1.0
    assert tuning.refused == []


def test_tuning_refuses_an_optimum_outside_the_bounds() -> None:
    cases, ranked = tuning_cases()

    tuning = tune(
        cases,
        ranked,
        DiscoveryConfig(min_score=0.3, tie_margin=0.05),
        {"min_score": (0.3, 0.5), "tie_margin": (0.02, 0.15)},
    )

    assert tuning.after["min_score"] >= 0.3
    assert any("min_score" in reason for reason in tuning.refused)
    assert tuning.decisive_rate == 0.5


def test_a_tunable_without_bounds_is_left_alone() -> None:
    cases, ranked = tuning_cases()

    tuning = tune(cases, ranked, DiscoveryConfig(), {"min_score": (0.1, 0.5)})

    assert tuning.after["tie_margin"] == tuning.before["tie_margin"]
    assert any("tie_margin" in reason for reason in tuning.refused)


def test_tuned_thresholds_are_written_beside_the_state_and_not_into_the_configuration(
    tmp_path: Path, repo_root: Path
) -> None:
    cases, ranked = tuning_cases()
    tuning = tune(cases, ranked, DiscoveryConfig(), {"min_score": (0.1, 0.5)})
    before = (repo_root / "configs" / "runtime.json").read_text(encoding="utf-8")

    path = write_overrides(tmp_path / "state", tuning, at=MOMENT)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert path.parent == tmp_path / "state"
    assert document["discovery"] == tuning.after
    assert document["replaces"] == tuning.before
    assert (repo_root / "configs" / "runtime.json").read_text(encoding="utf-8") == before


# --- level 1: evaluation --------------------------------------------------


def discovery_over(registry: SkillRegistry, repo_root: Path) -> Discovery:
    """Discovery as the runtime configures it, ranking without a provider."""
    runtime = load_runtime_config(repo_root / "configs" / "runtime.json")
    return Discovery(registry, KeywordIndex(), runtime.discovery)


def test_the_seeded_set_is_written_in_two_languages_and_holds_ambiguous_cases(
    evaluation_dir: Path, registry: SkillRegistry
) -> None:
    cases = load_cases(evaluation_dir / "routing.json")
    declared = {
        trigger.strip().lower()
        for skill in registry.published()
        for trigger in skill.frontmatter.triggers
    }

    assert len(cases) >= 14
    assert {case.language for case in cases} == {"en", "it"}
    assert sum(1 for case in cases if case.expected == TIE) == 2
    assert not any(case.text.strip().lower() in declared for case in cases)


def test_the_evaluation_reports_the_case_that_is_labeled_wrong(
    evaluation_dir: Path, registry: SkillRegistry, repo_root: Path
) -> None:
    discovery = discovery_over(registry, repo_root)
    cases = load_cases(evaluation_dir / "routing.json")

    clean = run_evaluation(cases, discovery)
    spoiled = run_evaluation(
        [*cases, RoutingCase(text="restart payments now", expected="runbook.log_search")],
        discovery,
    )

    assert clean.precision == 1.0
    assert clean.failures == []
    assert clean.decisive_rate == pytest.approx(14 / 16)
    assert clean.model_pick_rate == pytest.approx(2 / 16)
    assert clean.per_language == {"en": 1.0, "it": 1.0}
    assert spoiled.precision < clean.precision
    assert [failure.actual for failure in spoiled.failures] == ["runbook.service_restart"]


def test_the_search_ranks_the_seeded_set_once_and_stays_inside_the_declared_bounds(
    evaluation_dir: Path, registry: SkillRegistry, repo_root: Path
) -> None:
    runtime = load_runtime_config(repo_root / "configs" / "runtime.json")
    discovery = discovery_over(registry, repo_root)
    cases = load_cases(evaluation_dir / "routing.json")

    ranked = rank_cases(cases, discovery.candidates(), KeywordIndex(), runtime.discovery.limit)
    tuning = tune(cases, ranked, runtime.discovery, runtime.improvement.bounds)

    assert len(ranked) == len(cases)
    assert tuning.precision == 1.0
    assert tuning.refused == []
    for name, (low, high) in runtime.improvement.bounds.items():
        assert low <= tuning.after[name] <= high


# --- level 2: gaps and drafts ---------------------------------------------


def test_gaps_are_clustered_and_described_from_the_ontology(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    signals = [
        signal("scale the checkout service", None, score=0.05),
        signal("scale up the checkout service", None, score=0.0),
        signal("please scale the checkout service", None, score=0.1),
        signal("order a laptop", None, score=0.0),
        signal("is the api healthy", "runbook.service_health", score=0.6),
    ]

    gaps = find_gaps(
        signals,
        graph,
        improvement_config(cluster_threshold=0.6, cluster_min_size=3),
        min_score=0.2,
    )

    assert len(gaps) == 1
    assert gaps[0].size == 3
    assert "entity.service" in gaps[0].suggested_entities
    assert gaps[0].suggested_operations
    assert all(not name.startswith("runbook.") for name in gaps[0].suggested_operations)
    assert len(gaps[0].sample_phrases) <= 3


def test_the_job_writes_a_report_and_leaves_its_candidates_where_they_are(
    example_settings: Settings, evaluation_dir: Path
) -> None:
    runtime = load_runtime_config(settings=example_settings)
    state_dir = Path(example_settings.state_dir)
    write_audit(
        state_dir / "audit.jsonl",
        [
            event("turn", "session-1", "t1", {"turn": 1, "text": "bounce the payments worker"}),
            event("route", "session-1", "t1", {"source": "fast_path", "language": "en"}),
            event(
                "discovery",
                "session-1",
                "t1",
                {
                    "ranked": [{"skill_id": "runbook.service_restart", "score": 0.5}],
                    "chosen": "runbook.service_restart",
                    "tie": False,
                },
            ),
            event(
                "execute",
                "session-1",
                "t1",
                {"operation": "service.restart", "stage": "call", "ok": True, "write": True},
            ),
            *[
                line
                for index, text in enumerate(
                    (
                        "scale the checkout service",
                        "scale the checkout service up",
                        "scale the checkout service now",
                    ),
                    start=2,
                )
                for line in (
                    event("turn", "session-2", f"t{index}", {"text": text}, minute=index),
                    event("route", "session-2", f"t{index}", {"source": "model"}, minute=index),
                )
            ],
        ],
    )

    report = run_job(
        example_settings,
        cases_path=evaluation_dir / "routing.json",
        redactions_path=evaluation_dir / "redactions.json",
    )

    assert report.signals == 4
    assert report.grounded == 1
    assert report.learned == {"runbook.service_restart": 1}
    assert read_learned(state_dir / "learned", "runbook.service_restart")
    assert report.evaluation.precision == 1.0
    assert report.previous_precision is None
    assert report.tuning is not None
    assert report.tuning_applied is False
    assert report.overrides_path is None
    assert report.gaps and report.gaps[0].size == 3
    assert report.proposals
    assert {proposal.state for proposal in report.proposals} == {ProposalState.draft.value}

    held = ProposalStore(state_dir / "proposals").list()
    assert [proposal.state for proposal in held] == [ProposalState.draft] * len(held)

    written = read_report(state_dir / "improvement.json")
    assert written is not None
    assert written.model_dump(mode="json") == report.model_dump(mode="json")
    assert "level 3" in render_text(report)
    assert json.loads(render_json(report))["gaps"]
    assert runtime.improvement.cluster_min_size == 3


def test_a_second_run_carries_the_precision_of_the_first(
    example_settings: Settings, evaluation_dir: Path
) -> None:
    write_audit(Path(example_settings.state_dir) / "audit.jsonl", [])
    first = run_job(
        example_settings,
        cases_path=evaluation_dir / "routing.json",
        redactions_path=evaluation_dir / "redactions.json",
    )

    second = run_job(
        example_settings,
        cases_path=evaluation_dir / "routing.json",
        redactions_path=evaluation_dir / "redactions.json",
    )

    assert first.previous_precision is None
    assert second.previous_precision == first.evaluation.precision


def test_applying_the_tuning_writes_the_overrides_file(
    example_settings: Settings, evaluation_dir: Path
) -> None:
    write_audit(Path(example_settings.state_dir) / "audit.jsonl", [])

    report = run_job(
        example_settings,
        apply_tuning=True,
        cases_path=evaluation_dir / "routing.json",
        redactions_path=evaluation_dir / "redactions.json",
    )

    assert report.tuning_applied is True
    assert report.overrides_path is not None
    assert Path(report.overrides_path).is_file()


# --- level 3: the ceiling -------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under a directory, by size and modification time."""
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class DraftsOnly(ProposalStore):
    """A store that refuses to hold anything the job did not leave in `draft`.

    The ceiling is not that the job avoids calling a particular function; it is
    that nothing it produces is ever in another state. Asking the store rather
    than the syntax catches every way of getting there, an assignment to
    `state` followed by a save included.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.saved: list[str] = []

    def save(self, proposal: SkillProposal) -> Path:
        if proposal.state is not ProposalState.draft:
            raise AssertionError(
                f"the improvement job saved a proposal in {proposal.state.value}"
            )
        self.saved.append(proposal.proposal_id)
        return super().save(proposal)


@contextmanager
def writes_confined_to(
    root: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Raise on any write outside `root`, whichever way it is attempted.

    `scratch` is the temporary directory the quality gate validates a candidate
    in and throws away; it is pinned here so that the one write the job makes
    outside the state directory is a named, empty place rather than anywhere.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    allowed = (root.resolve(), scratch.resolve())
    write_text, write_bytes = Path.write_text, Path.write_bytes
    path_open, mkdir, builtin_open = Path.open, Path.mkdir, builtins.open

    def refuse_outside(target: object, what: str) -> None:
        if isinstance(target, int):
            return
        where = Path(str(target)).resolve()
        if not any(where.is_relative_to(place) for place in allowed):
            raise AssertionError(f"the improvement job {what} outside the state: {where}")

    def writing(mode: str) -> bool:
        return any(letter in mode for letter in "wax+")

    def guarded_write_text(self: Path, *args: object, **kwargs: object) -> int:
        refuse_outside(self, "wrote")
        return write_text(self, *args, **kwargs)

    def guarded_write_bytes(self: Path, *args: object, **kwargs: object) -> int:
        refuse_outside(self, "wrote")
        return write_bytes(self, *args, **kwargs)

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if writing(mode):
            refuse_outside(self, "opened for writing")
        return path_open(self, mode, *args, **kwargs)

    def guarded_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        refuse_outside(self, "created a directory")
        return mkdir(self, *args, **kwargs)

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        if writing(mode):
            refuse_outside(file, "opened for writing")
        return builtin_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        yield
    finally:
        monkeypatch.undo()


def test_the_job_runs_against_a_store_that_refuses_anything_but_a_draft(
    example_settings: Settings,
    evaluation_dir: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling, proved by running under it rather than by reading the source.

    The store raises on any proposal that is not a draft, every write outside
    the state directory raises, and the catalog, the configuration and the
    labeled set are compared before and after.
    """
    state_dir = Path(example_settings.state_dir)
    write_audit(
        state_dir / "audit.jsonl",
        [
            line
            for index, text in enumerate(
                (
                    "scale the checkout service",
                    "scale the checkout service up",
                    "scale the checkout service now",
                ),
                start=1,
            )
            for line in (
                event("turn", "session-1", f"t{index}", {"text": text}, minute=index),
                event("route", "session-1", f"t{index}", {"source": "model"}, minute=index),
            )
        ],
    )
    store = DraftsOnly(state_dir / "proposals")
    untouched = {
        name: _snapshot(repo_root / name)
        for name in ("examples", "configs", "schemas", "spec")
    }

    scratch = state_dir.parent / "scratch"

    with writes_confined_to(state_dir, scratch, monkeypatch):
        report = run_job(
            example_settings,
            cases_path=evaluation_dir / "routing.json",
            redactions_path=evaluation_dir / "redactions.json",
            proposals=store,
        )

    assert report.proposals
    assert store.saved
    assert {proposal.state for proposal in report.proposals} == {ProposalState.draft.value}
    assert [proposal.state for proposal in store.list()] == [ProposalState.draft] * len(
        store.saved
    )
    assert untouched == {
        name: _snapshot(repo_root / name)
        for name in ("examples", "configs", "schemas", "spec")
    }
    assert not any(scratch.iterdir())


# The scan below is the cheap second line: it reads the package rather than
# running it, so it says nothing about a path taken at runtime, and the proof
# of the ceiling is the test above.


def package_sources() -> list[Path]:
    """Every module the improvement job is made of, the command surface included."""
    return sorted(IMPROVEMENT.rglob("*.py")) + [COMMANDS]


def calls_of(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def constants_in(node: ast.Call) -> list[str]:
    """Every string constant this call passes, inside formatted strings as well."""
    found: list[str] = []
    for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
        for inner in ast.walk(argument):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                found.append(inner.value)
    return found


def test_no_module_of_the_package_moves_a_proposal_past_draft() -> None:
    offending: list[str] = []
    for path in package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in calls_of(tree):
            if call_name(node) != "transition":
                continue
            targets = [
                value for value in constants_in(node) if value != ProposalState.draft.value
            ]
            if targets:
                offending.append(f"{path.name}:{node.lineno} moves a proposal to {targets}")
    assert offending == []


def test_no_call_of_the_package_names_a_state_beyond_draft() -> None:
    offending = [
        f"{path.name}:{node.lineno} passes {value!r}"
        for path in package_sources()
        for node in calls_of(ast.parse(path.read_text(encoding="utf-8")))
        for value in constants_in(node)
        if value in BEYOND_DRAFT
    ]
    assert offending == []


def test_the_package_imports_neither_the_lifecycle_nor_a_policy_or_connector_writer() -> None:
    forbidden = (
        "oska.learning.lifecycle",
        "oska.connectors",
        "oska.governance.policy_engine",
    )
    imported: list[str] = []
    for path in package_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
    assert [name for name in imported if name.startswith(forbidden)] == []


# --- the command surface --------------------------------------------------


@pytest.fixture
def cli_state(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> Path:
    """Point the commands at the example domain, with state under the test."""
    state = tmp_path / "state"
    for name, value in (
        ("SKILLS_ROOTS", repo_root / "examples" / "ops" / "skills"),
        ("RUNTIME_CONFIG", repo_root / "configs" / "runtime.json"),
        ("MODEL_CONFIG", repo_root / "configs" / "model.json"),
        ("SCHEMAS_DIR", repo_root / "schemas"),
        ("STATE_DIR", state),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    monkeypatch.setenv("OSKA_PROVIDER", "none")
    state.mkdir(parents=True, exist_ok=True)
    return state


def test_the_commands_run_the_job_show_it_and_revert_one_procedure(
    cli_state: Path, evaluation_dir: Path
) -> None:
    runner = CliRunner()
    write_audit(
        cli_state / "audit.jsonl",
        [
            event("turn", "session-1", "t1", {"text": "bounce the payments worker"}),
            event("route", "session-1", "t1", {"source": "fast_path", "language": "en"}),
            event(
                "discovery",
                "session-1",
                "t1",
                {"ranked": [], "chosen": "runbook.service_restart", "tie": False},
            ),
            event(
                "execute",
                "session-1",
                "t1",
                {"operation": "service.restart", "stage": "call", "ok": True},
            ),
        ],
    )
    cases = ["--cases", str(evaluation_dir / "routing.json")]

    ran = runner.invoke(app, ["run", "--json", *cases])
    shown = runner.invoke(app, ["show"])
    reverted = runner.invoke(app, ["revert", "runbook.service_restart"])
    again = runner.invoke(app, ["revert", "runbook.service_restart"])

    assert ran.exit_code == 0
    assert json.loads(ran.stdout)["learned"] == {"runbook.service_restart": 1}
    assert shown.exit_code == 0
    assert "level 3" in shown.stdout
    assert reverted.exit_code == 0
    assert again.exit_code == 1
    assert not learned_path(cli_state / "learned", "runbook.service_restart").exists()


def test_evaluating_changes_nothing_and_a_drop_leaves_a_non_zero_status(
    cli_state: Path, evaluation_dir: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    write_audit(cli_state / "audit.jsonl", [])
    spoiled = tmp_path / "spoiled.json"
    cases = load_cases(evaluation_dir / "routing.json")
    spoiled.write_text(
        json.dumps(
            {
                "cases": [
                    *(case.model_dump() for case in cases),
                    {
                        "text": "restart payments now",
                        "expected": "runbook.log_search",
                        "language": "en",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    first = runner.invoke(app, ["run", "--cases", str(evaluation_dir / "routing.json")])
    clean = runner.invoke(app, ["evaluate", "--cases", str(evaluation_dir / "routing.json")])
    dropped = runner.invoke(app, ["evaluate", "--cases", str(spoiled), "--json"])
    held = read_report(cli_state / "improvement.json")

    assert first.exit_code == 0
    assert clean.exit_code == 0
    assert "precision 1.0" in clean.stdout
    assert dropped.exit_code == 1
    assert json.loads(dropped.stdout)["precision"] < 1.0
    assert held is not None and held.evaluation.precision == 1.0


# --- level 0: a judgment on an answer -------------------------------------


def test_a_judgment_is_recorded_against_a_turn_and_changes_nothing_else(
    tmp_path: Path,
) -> None:
    """One line, one file, and no other state touched."""
    root = tmp_path / "state" / "feedback"
    store = FeedbackStore(root)

    store.record("session-1", 1, "up")
    store.record("session-1", 2, "down", "too long")
    store.record("session-2", 1, "down")

    held = store.for_session("session-1")
    assert [(judgment.turn, judgment.score) for judgment in held] == [(1, "up"), (2, "down")]
    assert held[1].comment == "too long"
    assert len(store.all()) == 3
    assert scores(store.all()) == {"up": 1, "down": 2}
    assert sorted(path.name for path in tmp_path.rglob("*.jsonl")) == [
        "session-1.jsonl",
        "session-2.jsonl",
    ]


def test_the_last_judgment_of_a_turn_is_the_one_that_counts(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback")

    store.record("session-1", 1, "down", "wrong")
    store.record("session-1", 1, "up", "better now")

    assert latest(store.all())[("session-1", 1)].score == "up"
    assert len(store.for_session("session-1")) == 2


def test_a_score_that_is_not_up_or_down_is_refused(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback")

    with pytest.raises(ValueError, match="a score is"):
        store.record("session-1", 1, "sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="counted from 1"):
        store.record("session-1", 0, "up")


def test_a_signal_carries_the_judgment_recorded_against_its_turn(tmp_path: Path) -> None:
    """The turn a person judged is the turn the signal names."""
    audit = write_audit(
        tmp_path / "audit.jsonl",
        [
            event("turn", "session-1", "t1", {"turn": 1, "text": "restart the search service"}),
            event("route", "session-1", "t1", {"source": "model", "language": "en"}),
            event("turn", "session-1", "t2", {"turn": 2, "text": "and the logs"}, minute=1),
            event("route", "session-1", "t2", {"source": "model", "language": "en"}, minute=1),
        ],
    )
    store = FeedbackStore(tmp_path / "feedback")
    store.record("session-1", 2, "down", "far too long")

    signals = load_signals(audit, None, store.all())

    assert [signal.turn for signal in signals] == [1, 2]
    assert signals[0].feedback is None
    assert signals[1].feedback is not None
    assert signals[1].feedback.comment == "far too long"


# --- level 1: a preference about how answers read --------------------------


class _Classifier:
    """A provider that answers the classification, and counts what it was asked."""

    name = "classifier"

    def __init__(self, answers: list[dict]) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def complete_json(
        self,
        system: str,
        messages: list[str],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self.asked.append(messages[-1].content)
        return self._answers.pop(0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_bag_vector(text) for text in texts]


def _judgment(comment: str, session_id: str = "session-1") -> TurnFeedback:
    return TurnFeedback(session_id=session_id, turn=1, score="down", comment=comment, at=MOMENT)


def test_a_comment_about_presentation_becomes_a_preference(tmp_path: Path) -> None:
    root = tmp_path / "preferences"
    provider = _Classifier([{"kind": "presentation", "preference": "answer in two sentences"}])

    recorded = learn_preferences([_judgment("too long, be brief")], root, provider=provider)

    assert [preference.text for preference in recorded["session-1"]] == [
        "answer in two sentences"
    ]
    assert read_preferences(root, "session-1")[0].text == "answer in two sentences"
    assert provider.asked == ["too long, be brief"]


def test_a_comment_asking_for_something_to_be_done_is_discarded(tmp_path: Path) -> None:
    """A stored one would be a standing instruction to act that nobody approved."""
    root = tmp_path / "preferences"
    provider = _Classifier([{"kind": "action", "preference": "restart the payments service"}])

    judgments = [_judgment("just restart payments next time")]
    recorded = learn_preferences(judgments, root, provider=provider)

    assert recorded == {}
    assert read_preferences(root, "session-1") == []
    assert not root.exists()


def test_without_a_provider_nothing_is_classified(tmp_path: Path) -> None:
    root = tmp_path / "preferences"

    assert learn_preferences([_judgment("too long")], root) == {}
    assert classify_comment("", _Classifier([])) is None


def test_an_up_and_a_comment_free_judgment_are_never_classified(tmp_path: Path) -> None:
    provider = _Classifier([])
    judgments = [
        TurnFeedback(session_id="s", turn=1, score="up", comment="loved it", at=MOMENT),
        TurnFeedback(session_id="s", turn=2, score="down", comment="", at=MOMENT),
    ]

    assert learn_preferences(judgments, tmp_path / "preferences", provider=provider) == {}
    assert provider.asked == []


def test_a_preference_is_a_file_and_reverting_it_is_deleting_it(tmp_path: Path) -> None:
    root = tmp_path / "preferences"
    write_preferences(root, "session-1", [Preference(text="no lists", session_id="session-1")])

    assert read_preferences(root, "session-1")[0].text == "no lists"
    assert revert_preferences(root, "session-1") is True
    assert read_preferences(root, "session-1") == []
    assert revert_preferences(root, "session-1") is False


def test_a_run_of_the_job_reads_the_judgments_and_records_the_preferences(
    example_settings: Settings, tmp_path: Path
) -> None:
    """The judgment is a level 0 signal and the preference is a level 1 change."""
    state = Path(example_settings.state_dir)
    write_audit(
        state / "audit.jsonl",
        [event("turn", "session-1", "t1", {"turn": 1, "text": "how is search doing"})],
    )
    store = FeedbackStore(feedback_root(state))
    store.record("session-1", 1, "down", "too long and no lists please")
    provider = _Classifier([{"kind": "presentation", "preference": "no lists, keep it short"}])

    report = run_job(example_settings, provider=provider, proposals=ProposalStore(tmp_path / "p"))

    assert report.judged == 1
    assert report.judgments == {"up": 0, "down": 1}
    assert report.preferences == {"session-1": 1}
    assert read_preferences(preferences_root(state), "session-1")[0].text == (
        "no lists, keep it short"
    )
    assert "presentation preferences: 1" in render_text(report)


def test_the_command_records_a_judgment_and_reverts_a_preference(cli_state: Path) -> None:
    runner = CliRunner()
    write_preferences(
        preferences_root(cli_state), "session-1", [Preference(text="no lists")]
    )

    recorded = runner.invoke(
        app,
        ["feedback", "session-1", "--turn", "2", "--score", "down", "--comment", "too long"],
    )
    refused = runner.invoke(app, ["feedback", "session-1", "--turn", "2", "--score", "meh"])
    reverted = runner.invoke(app, ["revert", "session-1"])
    again = runner.invoke(app, ["revert", "session-1"])

    assert recorded.exit_code == 0
    assert "nothing else changed" in recorded.stdout
    assert refused.exit_code == 1
    assert FeedbackStore(feedback_root(cli_state)).for_session("session-1")[0].comment == (
        "too long"
    )
    assert reverted.exit_code == 0
    assert "recorded preferences were deleted" in reverted.stdout
    assert again.exit_code == 1
