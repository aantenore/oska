"""Removing the record of a conversation from every place it is kept.

The account of a session is a row in the ledger, a run of lines in the audit
trail and a document in the session store. It is also the judgments a person
left on its answers, the preferences learned from those judgments and, where
tracing is on, the spans naming it. These tests hold all of them together: a
removal that reaches some of the stores has not removed anything, and a removal
that reaches a session nobody asked about has removed too much.

The last three belong to layers above retention, so they are handed in by the
caller. A deployment that keeps none of them hands in none, and the counts for
them are zero.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from oska.audit.events import new_event
from oska.audit.sink import JsonlSink, rewrite_jsonl
from oska.audit.trace import JsonlTraceSink
from oska.cli.commands_forget import app as forget_app
from oska.config import Settings, load_runtime_config, state_path
from oska.contracts.events import AuditEvent
from oska.contracts.proposal import (
    ProposalState,
    ProposalTransition,
    QualityReport,
    SkillProposal,
)
from oska.engine.factory import build_engine
from oska.engine.session_store import SessionStore
from oska.engine.state import SessionState
from oska.governance.ledger import WriteLedger
from oska.governance.retention import expire, forget
from oska.improvement.feedback import FeedbackStore, feedback_root
from oska.improvement.preferences import (
    Preference,
    PreferenceStore,
    preferences_path,
    preferences_root,
    write_preferences,
)
from oska.learning.changes import (
    ChangeRequestState,
    ChangeRequestStore,
    new_request,
)
from oska.learning.forgetting import FORGOTTEN, ChangeRequestRecords, ProposalRecords
from oska.learning.proposals import ProposalStore
from oska.platform import Platform
from oska.services import PROPOSALS_DIRNAME
from oska.storage import open_sessions

GONE = "session-to-forget"
KEPT = "session-to-keep"
SECRET = "the sentence somebody typed"
LEAKY = "session-that-typed-a-credential"
CREDENTIAL = "sk-live-4f2b9c8d7e6a5b4c3d2e1f0a"
REDACTED = "[redacted]"


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """An audit trail holding two turns of each of two sessions."""
    path = tmp_path / "audit.jsonl"
    sink = JsonlSink(path)
    for session_id in (GONE, KEPT):
        sink.emit(new_event("turn", session_id, "t1", {"turn": 1, "text": SECRET}))
        sink.emit(new_event("reply", session_id, "t1", {"kind": "outcomes"}))
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> WriteLedger:
    """A ledger holding two receipts for one session and one for the other."""
    held = WriteLedger(tmp_path / "ledger.sqlite")
    for session_id, count in ((GONE, 2), (KEPT, 1)):
        for index in range(count):
            held.record(
                held.new_receipt(
                    session_id,
                    f"key-{session_id}-{index}",
                    "integration.access_control",
                    "membership.grant",
                    {"username": "tnovak", "group": f"group-{index}"},
                )
            )
    return held


@pytest.fixture
def sessions(tmp_path: Path) -> SessionStore:
    """A session store holding the state of both sessions."""
    store = SessionStore(tmp_path / "sessions.sqlite")
    for session_id in (GONE, KEPT):
        store.save(SessionState(session_id=session_id))
    return store


@pytest.fixture
def feedback(tmp_path: Path) -> FeedbackStore:
    """Judgments left on two turns of one session and one turn of the other."""
    store = FeedbackStore(feedback_root(tmp_path))
    store.record(GONE, 1, "down", "keep it shorter")
    store.record(GONE, 2, "up")
    store.record(KEPT, 1, "up")
    return store


@pytest.fixture
def preferences(tmp_path: Path) -> PreferenceStore:
    """A preference file for each of the two sessions."""
    root = preferences_root(tmp_path)
    for session_id in (GONE, KEPT):
        held = [Preference(text="Keep it short.", session_id=session_id)]
        write_preferences(root, session_id, held)
    return PreferenceStore(root)


@pytest.fixture
def traces(tmp_path: Path) -> JsonlTraceSink:
    """A trace file holding one turn of each session, each with a nested span."""
    sink = JsonlTraceSink(tmp_path / "traces.jsonl")
    for session_id in (GONE, KEPT):
        with sink.span("turn", session=session_id, turn=1):
            with sink.span("route"):
                pass
    return sink


def _events(path: Path) -> list[AuditEvent]:
    return [
        AuditEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_forget_removes_one_session_from_all_three_stores(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    report = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.sessions == [GONE]
    assert (report.receipts, report.events, report.session_states) == (2, 2, 1)
    assert ledger.for_session(GONE) == []
    assert len(ledger.for_session(KEPT)) == 1
    assert sessions.sessions() == [KEPT]
    kinds = [(event.session_id, event.kind) for event in _events(audit_path)]
    assert (KEPT, "turn") in kinds
    assert (GONE, "turn") not in kinds


def test_forget_leaves_no_trace_of_what_was_said(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    trail = audit_path.read_text(encoding="utf-8")
    assert trail.count(SECRET) == 1  # the one the kept session wrote
    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert len(recorded) == 1
    assert recorded[0].session_id == GONE
    assert recorded[0].data == {
        "operation": "forget",
        "receipts": 2,
        "events": 2,
        "session_states": 1,
        "feedback": 0,
        "preferences": 0,
        "trace_spans": 0,
        "proposals": 0,
        "change_requests": 0,
    }


def test_the_record_of_a_removal_survives_the_next_removal(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    again = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert (again.receipts, again.events, again.session_states) == (0, 0, 0)
    kinds = [event.kind for event in _events(audit_path) if event.session_id == GONE]
    assert kinds == ["retention", "retention"]


def test_forgetting_a_session_nobody_recorded_removes_nothing(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    before = audit_path.read_text(encoding="utf-8")
    report = forget("never-happened", ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.empty
    assert len(ledger.for_session(GONE)) == 2
    assert before in audit_path.read_text(encoding="utf-8")


def test_expire_removes_what_is_past_the_window_and_nothing_else(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=40))
    report = expire(30, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.sessions == [GONE]
    assert report.receipts == 2
    assert sessions.sessions() == [KEPT]
    assert len(ledger.for_session(KEPT)) == 1
    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert [event.data["operation"] for event in recorded] == ["expire"]


def test_a_window_of_zero_days_keeps_everything(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=400))
    report = expire(0, ledger=ledger, audit_path=audit_path, sessions=sessions)
    assert report.empty and report.sessions == []
    assert sorted(sessions.sessions()) == sorted([GONE, KEPT])
    assert [event.kind for event in _events(audit_path) if event.kind == "retention"] == []
    # Deletion on request is not the window, and still works.
    assert forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions).receipts == 2


def test_a_trail_is_replaced_whole_and_keeps_what_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('not json\n{"session_id": "a"}\n{"session_id": "b"}\n', encoding="utf-8")
    removed = rewrite_jsonl(path, lambda entry: entry.get("session_id") != "a")
    assert removed == 1
    assert path.read_text(encoding="utf-8") == 'not json\n{"session_id": "b"}\n'
    assert [item.name for item in tmp_path.iterdir()] == ["audit.jsonl"]


def test_a_trail_that_was_never_written_is_not_an_error(
    ledger: WriteLedger,
    sessions: SessionStore,
    tmp_path: Path,
) -> None:
    path = tmp_path / "nothing" / "audit.jsonl"
    report = forget(GONE, ledger=ledger, audit_path=path, sessions=sessions)
    assert report.events == 0
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "retention"


# --- the stores a caller hands in --------------------------------------------


def test_forget_reaches_the_judgments_the_preferences_and_the_traces(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
    tmp_path: Path,
) -> None:
    """A session leaves six trails, and a request to be forgotten answers all six."""
    report = forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    assert (report.receipts, report.events, report.session_states) == (2, 2, 1)
    assert (report.feedback, report.preferences, report.trace_spans) == (2, 1, 2)
    assert feedback.for_session(GONE) == []
    assert len(feedback.for_session(KEPT)) == 1
    assert not preferences_path(preferences_root(tmp_path), GONE).is_file()
    assert preferences_path(preferences_root(tmp_path), KEPT).is_file()
    held = _spans(traces.path)
    assert [span["name"] for span in held] == ["route", "turn"]
    assert held[1]["fields"]["session"] == KEPT


def test_the_event_names_the_counts_of_every_store_and_no_content(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
) -> None:
    """What was removed is counted; what it said is not in the event or anywhere else."""
    forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert len(recorded) == 1
    assert recorded[0].data == {
        "operation": "forget",
        "receipts": 2,
        "events": 2,
        "session_states": 1,
        "feedback": 2,
        "preferences": 1,
        "trace_spans": 2,
        "proposals": 0,
        "change_requests": 0,
    }
    assert "keep it shorter" not in audit_path.read_text(encoding="utf-8")
    assert "Keep it short." not in audit_path.read_text(encoding="utf-8")


def test_a_deployment_that_keeps_none_of_them_hands_in_none(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
) -> None:
    """The three stores are optional, and what is not kept is counted zero."""
    report = forget(GONE, ledger=ledger, audit_path=audit_path, sessions=sessions)

    assert (report.feedback, report.preferences, report.trace_spans) == (0, 0, 0)
    assert report.receipts == 2


def test_the_window_reaches_the_same_stores_as_a_request(
    ledger: WriteLedger,
    sessions: SessionStore,
    audit_path: Path,
    feedback: FeedbackStore,
    preferences: PreferenceStore,
    traces: JsonlTraceSink,
) -> None:
    """Expiry is the same removal on a schedule, so it removes the same things."""
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=40))

    report = expire(
        30,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        feedback=feedback,
        preferences=preferences,
        traces=traces,
    )

    assert report.sessions == [GONE]
    assert (report.feedback, report.preferences, report.trace_spans) == (2, 1, 2)
    assert feedback.for_session(GONE) == []
    assert len(feedback.for_session(KEPT)) == 1


def test_a_trace_of_a_session_goes_with_the_spans_opened_inside_it(
    tmp_path: Path,
) -> None:
    """A nested span names no session and still belongs to one, by descent."""
    sink = JsonlTraceSink(tmp_path / "traces.jsonl")
    with sink.span("turn", session=GONE, turn=1):
        with sink.span("plan"):
            with sink.span("execute"):
                pass
    with sink.span("turn", session=KEPT, turn=1):
        pass

    removed = sink.delete(GONE)

    assert removed == 3
    assert [span["name"] for span in _spans(sink.path)] == ["turn"]
    assert sink.delete("never-traced") == 0
    assert sink.delete(GONE) == 0


def _spans(path: Path) -> list[dict]:
    """The spans a trace file holds, in the order they were recorded."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- what is never written ---------------------------------------------------


def test_what_a_turn_must_not_keep_is_never_written_to_the_record(
    example_settings: Settings,
) -> None:
    """The other half of retention: the record never holds it, so nothing removes it.

    The input guardrail chain runs before the turn is recorded. What it redacts
    is what reaches the audit trail, the session history and the reply; the
    wording it refused reaches none of the three, and there is no later pass
    that takes it out again.
    """
    engine = build_engine(example_settings)
    runtime = load_runtime_config(settings=example_settings)
    audit_path = state_path(example_settings.state_dir, runtime.audit.path)
    sessions_path = state_path(example_settings.state_dir, runtime.sessions.path)

    result = engine.turn(LEAKY, f"restart the payments service, the api key is {CREDENTIAL}")

    assert [outcome.kind for outcome in result.outcomes] == ["denied"]
    assert CREDENTIAL not in result.reply

    trail = audit_path.read_text(encoding="utf-8")
    assert CREDENTIAL not in trail
    recorded = [event for event in _events(audit_path) if event.kind == "turn"]
    assert len(recorded) == 1
    assert REDACTED in str(recorded[0].data["text"])
    assert [event.kind for event in _events(audit_path) if event.kind == "retention"] == []

    held = engine.state(LEAKY)
    assert [message.content for message in held.history if CREDENTIAL in message.content] == []
    assert REDACTED in held.history[0].content
    assert CREDENTIAL.encode("utf-8") not in sessions_path.read_bytes()


def _backdate(store: SessionStore, session_id: str, moment: datetime) -> None:
    """Move when a session was last written, the way the passage of time would."""
    _backdate_path(store.path, session_id, moment)


def _backdate_path(store_path: Path, session_id: str, moment: datetime) -> None:
    """The same move, by the path of the sessions file rather than a store object."""
    connection = sqlite3.connect(store_path)
    with connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (moment.isoformat(), session_id),
        )
    connection.close()


# --- forget and expire from the terminal, reaching a platform -----------------
#
# `oska forget session` and `oska forget expired` open one deployment's stores
# by default. Given `--platform` and no `--domain` or `--tenant`, they reach
# every (tenant, domain) the platform serves, because a request to be
# forgotten is about a person, not about one domain: these tests hold a
# platform of two domains and two tenants and check that a removal touches the
# (tenant, domain) pairs it should and none of the others.

runner = CliRunner()


def _platform_manifest(examples: Path, tenants: list[str]) -> dict:
    """A platform of two domains, each serving the given tenants."""
    return {
        "name": "trial",
        "packs": {
            "ops": {"path": str(examples / "ops")},
            "access": {"path": str(examples / "access")},
        },
        "domains": [
            {
                "name": "operations",
                "packs": ["ops"],
                "host": {"name": "the operations desk"},
                "tenants": tenants,
            },
            {
                "name": "access",
                "packs": ["access"],
                "host": {"name": "the access desk"},
                "tenants": tenants,
            },
        ],
    }


def _write_platform(directory: Path, document: dict) -> Path:
    path = directory / "platform.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> Settings:
    """Settings of a platform process, with state under `tmp_path` for the CLI too.

    `oska forget` reads its settings from the environment through
    `load_settings`, so the state directory it opens has to be the one these
    tests open directly, or a test could not tell whether the command reached
    the right files.
    """
    state = tmp_path / "state"
    for name, value in (
        ("RUNTIME_CONFIG", repo_root / "configs" / "runtime.json"),
        ("MODEL_CONFIG", repo_root / "configs" / "model.json"),
        ("SCHEMAS_DIR", repo_root / "schemas"),
        ("STATE_DIR", state),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    monkeypatch.setenv("OSKA_PROVIDER", "none")
    return Settings(
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state,
        provider="none",
    )


@pytest.fixture
def two_domains_two_tenants(
    examples: Path, tmp_path: Path, platform_env: Settings
) -> tuple[Path, Platform]:
    """A platform of two domains (`operations`, `access`) and two tenants (`default`, `acme`)."""
    path = _write_platform(tmp_path, _platform_manifest(examples, ["default", "acme"]))
    return path, Platform.from_file(path, platform_env)


def _sessions_path(held: Platform, tenant: str, domain: str) -> Path:
    runtime = held.runtime_for(tenant)
    settings = held.settings_for(domain, tenant)
    return state_path(settings.state_dir, runtime.sessions.path)


def _exists(held: Platform, tenant: str, domain: str, session_id: str) -> bool:
    """Whether one (tenant, domain)'s own session store still holds this session."""
    store = open_sessions(held.runtime_for(tenant), held.settings_for(domain, tenant))
    return store.exists(session_id)


def _seed(held: Platform, domain: str, tenant: str, session_id: str) -> None:
    """One turn of this session in one (tenant, domain), through that pair's own engine.

    A platform binds a session to the tenant of its first turn, so the same id
    under two tenants cannot arrive through `Platform.turn`. Each pair's store
    can still hold it independently, and that is what a forget request across a
    platform has to reach.
    """
    held.engine(domain, tenant).turn(session_id, "hello")


def test_forget_session_on_a_platform_with_no_domain_reaches_every_pair(
    two_domains_two_tenants: tuple[Path, Platform],
) -> None:
    path, held = two_domains_two_tenants
    _seed(held, "operations", "acme", "s1")
    _seed(held, "access", "default", "s1")

    result = runner.invoke(forget_app, ["session", "s1", "--platform", str(path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["session_states"] == 2
    assert payload["reached"] == ["acme/operations", "default/access"]
    assert _exists(held, "acme", "operations", "s1") is False
    assert _exists(held, "default", "access", "s1") is False


def test_forget_session_with_a_named_domain_and_tenant_reaches_only_that_pair(
    two_domains_two_tenants: tuple[Path, Platform],
) -> None:
    path, held = two_domains_two_tenants
    _seed(held, "operations", "acme", "s1")
    _seed(held, "operations", "default", "s1")

    result = runner.invoke(
        forget_app,
        [
            "session",
            "s1",
            "--platform",
            str(path),
            "--domain",
            "operations",
            "--tenant",
            "acme",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["session_states"] == 1
    assert payload["reached"] == ["acme/operations"]
    assert _exists(held, "acme", "operations", "s1") is False
    assert _exists(held, "default", "operations", "s1") is True


def test_forget_session_with_a_named_domain_reaches_both_its_tenants(
    two_domains_two_tenants: tuple[Path, Platform],
) -> None:
    path, held = two_domains_two_tenants
    _seed(held, "operations", "acme", "s1")
    _seed(held, "operations", "default", "s1")
    _seed(held, "access", "default", "unrelated")

    result = runner.invoke(
        forget_app,
        ["session", "s1", "--platform", str(path), "--domain", "operations", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["session_states"] == 2
    assert payload["reached"] == ["acme/operations", "default/operations"]
    assert _exists(held, "default", "access", "unrelated") is True


def test_forget_expired_on_a_platform_reaches_every_pair(
    two_domains_two_tenants: tuple[Path, Platform],
) -> None:
    path, held = two_domains_two_tenants
    _seed(held, "operations", "acme", "old-in-ops")
    _seed(held, "access", "default", "old-in-access")
    long_ago = datetime.now(UTC) - timedelta(days=400)
    _backdate_path(_sessions_path(held, "acme", "operations"), "old-in-ops", long_ago)
    _backdate_path(_sessions_path(held, "default", "access"), "old-in-access", long_ago)

    result = runner.invoke(
        forget_app, ["expired", "--days", "30", "--platform", str(path), "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert sorted(payload["sessions"]) == ["old-in-access", "old-in-ops"]
    assert payload["reached"] == ["acme/operations", "default/access"]
    assert _exists(held, "acme", "operations", "old-in-ops") is False
    assert _exists(held, "default", "access", "old-in-access") is False


# --- what the evolution layer drafted from a session -------------------------

DRAFTED = f"## Purpose\n\nRepeats what was asked: {SECRET}.\n"


def _proposal(proposal_id: str, session_id: str) -> SkillProposal:
    return SkillProposal(
        proposal_id=proposal_id,
        state=ProposalState.published,
        source_session_id=session_id,
        skill_markdown=f"---\nid: runbook.{proposal_id}\n---\n{DRAFTED}",
        quality=QualityReport(issues=[f"triggers: {SECRET!r} already belongs to x"], score=0.9),
        history=[
            ProposalTransition(
                state=ProposalState.draft,
                at=datetime.now(UTC),
                by=session_id,
                note="drafted from the session",
            ),
            ProposalTransition(
                state=ProposalState.approved, at=datetime.now(UTC), by="reviewer"
            ),
        ],
    )


def _request(changes: ChangeRequestStore, proposal_id: str) -> str:
    request = new_request(proposal_id, f"runbook.{proposal_id}", 1, "operator", QualityReport())
    changes.save(request, skill_markdown=DRAFTED, policy={"note": SECRET})
    return request.request_id


def _drafted(state_dir: Path, changes_root: Path, catalog: Path) -> dict[str, str]:
    """Proposals and change requests drafted from two sessions, one change applied.

    For the session to forget: a draft with no request, one with an open
    request, one with a rejected request and one whose request was applied.
    For the session to keep: one with an open request.
    """
    proposals = ProposalStore(state_dir / PROPOSALS_DIRNAME)
    changes = ChangeRequestStore(changes_root)
    for proposal_id, session_id in (
        ("drafted", GONE),
        ("opened", GONE),
        ("refused", GONE),
        ("applied", GONE),
        ("other", KEPT),
    ):
        proposals.save(_proposal(proposal_id, session_id))
    ids = {name: _request(changes, name) for name in ("opened", "refused", "applied", "other")}
    changes.reject(ids["refused"], by="reviewer", note="not now")
    changes.apply(ids["applied"], catalog, by="reviewer")
    return ids


def test_forget_removes_what_was_drafted_and_keeps_an_applied_decision(
    ledger: WriteLedger, sessions: SessionStore, audit_path: Path, tmp_path: Path
) -> None:
    state_dir, changes_root = tmp_path / "state", tmp_path / "changes"
    ids = _drafted(state_dir, changes_root, tmp_path / "catalog")
    proposals = ProposalStore(state_dir / PROPOSALS_DIRNAME)
    changes = ChangeRequestStore(changes_root)

    report = forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        proposals=ProposalRecords(proposals, changes),
        change_requests=ChangeRequestRecords(proposals, changes),
    )

    assert (report.proposals, report.change_requests) == (4, 3)
    assert [proposal.proposal_id for proposal in proposals.list()] == ["applied", "other"]
    kept = proposals.get("applied")
    assert kept.source_session_id == FORGOTTEN
    assert kept.skill_markdown == ""
    assert kept.quality.issues == []
    assert [entry.by for entry in kept.history] == [FORGOTTEN, "reviewer"]
    assert proposals.get("other").source_session_id == KEPT

    held = {request.request_id: request for request in changes.list()}
    assert set(held) == {ids["applied"], ids["other"]}
    applied = held[ids["applied"]]
    assert applied.state is ChangeRequestState.applied
    assert (applied.decided_by, applied.applied_to) == ("reviewer", str(tmp_path / "catalog"))
    assert not (changes.directory(ids["applied"]) / applied.package).exists()
    assert (changes.directory(ids["other"]) / held[ids["other"]].package).is_dir()
    for path in [*(state_dir / PROPOSALS_DIRNAME).rglob("*"), *changes_root.rglob("*")]:
        if path.is_file() and ids["other"] not in str(path) and path.name != "other.json":
            assert SECRET not in path.read_text(encoding="utf-8"), path

    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert (recorded[0].data["proposals"], recorded[0].data["change_requests"]) == (4, 3)
    again = forget(
        GONE,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        proposals=ProposalRecords(proposals, changes),
        change_requests=ChangeRequestRecords(proposals, changes),
    )
    assert (again.proposals, again.change_requests) == (0, 0)


def test_forget_on_a_platform_reaches_what_each_pair_drafted(
    two_domains_two_tenants: tuple[Path, Platform], tmp_path: Path
) -> None:
    path, held = two_domains_two_tenants
    settings = held.settings_for("access", "acme")
    runtime = held.runtime_for("acme")
    _drafted(
        Path(settings.state_dir),
        state_path(settings.state_dir, runtime.changes.root),
        tmp_path / "catalog",
    )

    result = runner.invoke(forget_app, ["session", GONE, "--platform", str(path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["proposals"], payload["change_requests"]) == (4, 3)
    assert payload["reached"] == ["acme/access"]
    left = ProposalStore(Path(settings.state_dir) / PROPOSALS_DIRNAME).list()
    assert [proposal.proposal_id for proposal in left] == ["applied", "other"]


def test_expire_removes_what_was_drafted_and_keeps_an_applied_decision(
    ledger: WriteLedger, sessions: SessionStore, audit_path: Path, tmp_path: Path
) -> None:
    """The window reaches what was drafted from an expired session, as a request does."""
    state_dir, changes_root = tmp_path / "state", tmp_path / "changes"
    ids = _drafted(state_dir, changes_root, tmp_path / "catalog")
    proposals = ProposalStore(state_dir / PROPOSALS_DIRNAME)
    changes = ChangeRequestStore(changes_root)
    _backdate(sessions, GONE, datetime.now(UTC) - timedelta(days=40))

    report = expire(
        30,
        ledger=ledger,
        audit_path=audit_path,
        sessions=sessions,
        proposals=ProposalRecords(proposals, changes),
        change_requests=ChangeRequestRecords(proposals, changes),
    )

    assert report.sessions == [GONE]
    assert (report.proposals, report.change_requests) == (4, 3)
    assert [proposal.proposal_id for proposal in proposals.list()] == ["applied", "other"]
    kept = proposals.get("applied")
    assert (kept.source_session_id, kept.skill_markdown) == (FORGOTTEN, "")
    assert [entry.by for entry in kept.history] == [FORGOTTEN, "reviewer"]
    assert proposals.get("other").source_session_id == KEPT

    held = {request.request_id: request for request in changes.list()}
    assert set(held) == {ids["applied"], ids["other"]}
    applied = held[ids["applied"]]
    assert applied.state is ChangeRequestState.applied
    assert (applied.decided_by, applied.applied_to) == ("reviewer", str(tmp_path / "catalog"))
    assert not (changes.directory(ids["applied"]) / applied.package).exists()
    assert (changes.directory(ids["other"]) / held[ids["other"]].package).is_dir()
    for path in [*(state_dir / PROPOSALS_DIRNAME).rglob("*"), *changes_root.rglob("*")]:
        if path.is_file() and ids["other"] not in str(path) and path.name != "other.json":
            assert SECRET not in path.read_text(encoding="utf-8"), path

    recorded = [event for event in _events(audit_path) if event.kind == "retention"]
    assert [event.data["operation"] for event in recorded] == ["expire"]
    assert (recorded[0].data["proposals"], recorded[0].data["change_requests"]) == (4, 3)
