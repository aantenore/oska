"""The scenario format and the runner that executes it.

A scenario is one session written down: the answers the model provider gives,
the turns a person types, and what each turn must produce. Running it builds a
fresh runtime, replays the turns against a scripted provider and collects every
expectation that did not hold. Nothing here raises on a failed expectation; a
run always returns a result, so a suite reports on every scenario rather than
stopping at the first one.

A scenario may also carry a `principal`: the identity an authenticated channel
would have put on every turn of the session. The runner passes it in as a
channel would, so a procedure that takes a field from the identity can be
exercised without the scenario writing that field anywhere, and a scenario that
carries none is a session nobody signed in to.

A scenario may also carry a `learning` block. That block runs after the turns:
it drafts a proposal from the session the turns built, moves it through the
states it names, and checks where it ended up, whether the published procedure
is in the catalog, and that a transition without an actor is refused.

A scenario may ask for `presentation`, and then its engine is built with
presentation on, as a domain that enables it builds its own. A turn that lists
`directives` must produce that many, in that order, each naming the same
procedure, moment and view and holding every listed field with the listed
value. Fields it does not list are not compared, because some of them, such as
a receipt identifier, are different on every run. An empty list means the turn
must produce none; no list means directives are not checked.

`build` lets a caller assemble the engine a scenario runs against, which is how
a platform runs each domain's scenarios with that domain's engine.

A conformance run never touches a namespace it did not create. Every scenario
runs under a storage namespace of its own, made from a fresh run identifier
and the scenario (`conformance/<run id>/<position of its directory>/<name>` in
a suite), so it never meets what an earlier run, or a deployment sharing the
database, keeps; a platform builder keeps the (tenant, domain) part inside
it. When the scenario ends, passed or failed, everything
its engine wrote under that namespace (sessions, receipts, write claims) is
purged. A store that reports a namespace outside the one the run created is
left alone, and the scenario fails for it. Files go with the scenario's state
directory, which is temporary too.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oska.audit.sink import MemorySink
from oska.config import RuntimeConfig, Settings, load_runtime_config, state_path
from oska.contracts.common import TurnResult
from oska.contracts.proposal import ProposalState, SkillProposal
from oska.contracts.receipt import WriteReceipt
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.governance.identity import Principal
from oska.learning import LifecycleError, draft_from_session, quality, transition
from oska.learning.changes import ChangeRequestState, ChangeRequestStore
from oska.learning.proposals import ProposalStore
from oska.planner.scripted import ScriptedModelProvider
from oska.services import PROPOSALS_DIRNAME

__all__ = [
    "CONFORMANCE_NAMESPACE",
    "DEFAULT_LEVEL",
    "Expect",
    "LearningExpect",
    "LearningSpec",
    "PrincipalSpec",
    "RefusedTransition",
    "ScenarioFile",
    "ScenarioResult",
    "SuiteReport",
    "TransitionSpec",
    "TurnSpec",
    "conformance_namespace",
    "load_scenarios",
    "run_directories",
    "run_scenario",
    "run_suite",
]

EngineBuilder = Callable[..., Engine]
"""Builds a scenario's engine from settings, taking `build_engine`'s keyword arguments.

Among them is `namespace`, the storage namespace the scenario runs under: the
engine keeps its record there, or in a namespace nested inside it.
"""

CONFORMANCE_NAMESPACE = "conformance"
"""The first part of every namespace a conformance run creates."""

DEFAULT_LEVEL = 2
VERIFIED = "verified"

# Phrases that report a write as unconfirmed. They are removed before a reply is
# checked for a success claim, so "not verified" is not read as "verified".
NEGATIONS = (
    "not verified",
    "never verified",
    "unverified",
    "could not be verified",
    "cannot be verified",
)


class Expect(BaseModel):
    """What one turn must produce. An unset field is not checked."""

    kind: str
    contains: list[str] = Field(default_factory=list)
    outcomes: list[str] | None = None
    pending: str | None = None
    receipts: int | None = None
    verification: str | None = None
    directives: list[dict] | None = None


class TurnSpec(BaseModel):
    """One typed turn and its expectations.

    `connector_behaviors` is applied before the turn runs, keyed by integration
    and then by operation; a null behavior clears the one in force.
    """

    user: str
    expect: Expect
    connector_behaviors: dict | None = None


class TransitionSpec(BaseModel):
    """One lifecycle move the scenario asks for, and who asks for it."""

    to: str
    by: str
    note: str = ""


class RefusedTransition(BaseModel):
    """A move that must not be allowed. An empty actor is the usual case."""

    to: str
    by: str = ""


class LearningExpect(BaseModel):
    """Where the proposal must end up. An unset field is not checked."""

    state: str
    published_id: str | None = None
    quality_passed: bool | None = None


class LearningSpec(BaseModel):
    """What happens to the session after its turns, and what must come of it.

    `provider_script` is spent by a provider of its own, so the wording of the
    draft is scripted separately from the turns. Without one the draft is built
    from the recorded operations alone.
    """

    draft_from_session: bool = True
    transitions: list[TransitionSpec] = Field(default_factory=list)
    expect: LearningExpect
    provider_script: list[dict] | None = None
    expect_refused: RefusedTransition | None = None


class PrincipalSpec(BaseModel):
    """The identity an authenticated channel puts on every turn of this session."""

    subject: str
    attributes: dict = Field(default_factory=dict)
    authenticated: bool = True


class ScenarioFile(BaseModel):
    """One scenario as it is written on disk."""

    name: str
    description: str = ""
    level: int = DEFAULT_LEVEL
    principal: PrincipalSpec | None = None
    presentation: bool = False
    provider_script: list[dict] = Field(default_factory=list)
    turns: list[TurnSpec] = Field(default_factory=list)
    learning: LearningSpec | None = None


class ScenarioResult(BaseModel):
    """What running one scenario showed."""

    name: str
    level: int
    passed: bool
    failures: list[str] = Field(default_factory=list)
    turns: int = 0


class SuiteReport(BaseModel):
    """Every scenario of one directory, and the totals per level."""

    results: list[ScenarioResult] = Field(default_factory=list)
    by_level: dict[int, tuple[int, int]] = Field(default_factory=dict)
    passed: bool = False

    @classmethod
    def of(cls, results: list[ScenarioResult]) -> SuiteReport:
        """Build a report from the results of a run."""
        by_level: dict[int, tuple[int, int]] = {}
        for result in results:
            done, total = by_level.get(result.level, (0, 0))
            by_level[result.level] = (done + (1 if result.passed else 0), total + 1)
        return cls(
            results=results,
            by_level={level: by_level[level] for level in sorted(by_level)},
            passed=all(result.passed for result in results) and bool(results),
        )


def load_scenarios(directory: Path) -> list[ScenarioFile]:
    """Read every scenario file of a directory, in file name order."""
    scenarios: list[ScenarioFile] = []
    for path in sorted(Path(directory).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        document.setdefault("name", path.stem)
        scenarios.append(ScenarioFile.model_validate(document))
    return scenarios


def conformance_namespace(run_id: str, scenario: str) -> str:
    """The storage namespace one scenario of one conformance run is kept under.

    It holds at least two slashes, so it can equal neither the default
    namespace, which has none, nor the namespace of a (tenant, domain), which
    has one.
    """
    return f"{CONFORMANCE_NAMESPACE}/{run_id}/{scenario}"


def new_run_id() -> str:
    """A run identifier no other run has used."""
    return uuid.uuid4().hex


def run_scenario(
    scenario: ScenarioFile,
    settings: Settings,
    *,
    state_dir: Path,
    build: EngineBuilder | None = None,
    namespace: str | None = None,
) -> ScenarioResult:
    """Replay one scenario against a fresh runtime and report what did not hold.

    `build` assembles the engine; without it the settings name everything.
    `namespace` must be one this run created under `conformance/`; without it
    a fresh one is made. Whatever the engine wrote there is purged at the end,
    whether the scenario passed or failed.
    """
    namespace = namespace or conformance_namespace(new_run_id(), scenario.name)
    if not namespace.startswith(f"{CONFORMANCE_NAMESPACE}/"):
        raise ValueError(
            f"a conformance run writes only under {CONFORMANCE_NAMESPACE}/, not {namespace!r}"
        )
    engines: list[Engine] = []
    try:
        result = _replay(scenario, settings, Path(state_dir), build, namespace, engines)
    finally:
        cleanup = [problem for engine in engines for problem in _purge(engine, namespace)]
    if cleanup:
        result = result.model_copy(
            update={"passed": False, "failures": [*result.failures, *cleanup]}
        )
    return result


def _purge(engine: Engine, namespace: str) -> list[str]:
    """Remove what the engine's stores hold, when they are kept under this run's namespace.

    A store that names another namespace is not purged, and saying so fails the
    scenario, because the run was meant to write nowhere else. A file-backed
    store names none and removes nothing; its file goes with the state directory.
    """
    problems: list[str] = []
    for store in engine.stores:
        held = getattr(store, "namespace", None)
        if held is not None and held != namespace and not held.startswith(f"{namespace}/"):
            problems.append(
                f"cleanup: the engine keeps its record under {held!r}, outside the "
                f"namespace this run created ({namespace!r}); nothing there was removed"
            )
            continue
        try:
            store.purge()
        except Exception as exc:  # a failed cleanup is reported, never hidden
            problems.append(
                f"cleanup: {namespace!r} could not be purged: {type(exc).__name__}: {exc}"
            )
    return problems


def _confined(runtime: RuntimeConfig) -> RuntimeConfig:
    """The runtime configuration with every state path reduced to its file name.

    A deployment may keep its ledger or its sessions at an absolute path. A
    scenario must never write there: reduced to a name, each path lands in the
    scenario's own state directory, which is removed after the run.
    """
    sections: dict[str, Any] = {}
    for name in type(runtime).model_fields:
        section = getattr(runtime, name)
        if not isinstance(section, BaseModel):
            continue
        paths = {
            field: Path(value.name)
            for field in type(section).model_fields
            if isinstance(value := getattr(section, field), Path) and value.is_absolute()
        }
        if paths:
            sections[name] = section.model_copy(update=paths)
    return runtime.model_copy(update=sections) if sections else runtime


def _replay(
    scenario: ScenarioFile,
    settings: Settings,
    state_dir: Path,
    build: EngineBuilder | None,
    namespace: str,
    engines: list[Engine],
) -> ScenarioResult:
    """Build the engine, run the turns and the learning block, and collect the failures.

    The engine is appended to `engines` as soon as it exists, so the caller can
    purge what it wrote even when a later step raises.
    """
    provider = ScriptedModelProvider(scenario.provider_script)
    runtime = load_runtime_config(settings=settings)
    publish_root = state_path(Path(state_dir), runtime.learning.publish_root)
    scenario_settings = settings.model_copy(update={"state_dir": Path(state_dir)})
    if scenario.learning is not None:
        # What the lifecycle publishes has to be a catalog root, or the registry
        # would never see it and the published check could not mean anything.
        scenario_settings = scenario_settings.model_copy(
            update={"skills_roots": [*scenario_settings.skills_roots, publish_root]}
        )
    options: dict[str, Any] = {"presentation": True} if scenario.presentation else {}
    if build is None:
        options["runtime"] = _confined(runtime)
    engine = (build or build_engine)(
        scenario_settings,
        provider=provider,
        audit_sink=MemorySink(),
        namespace=namespace,
        **options,
    )
    engines.append(engine)
    principal = (
        None
        if scenario.principal is None
        else Principal(
            subject=scenario.principal.subject,
            attributes=dict(scenario.principal.attributes),
            authenticated=scenario.principal.authenticated,
        )
    )
    failures: list[str] = []
    replies: list[str] = []
    events: list = []
    ran = 0
    for position, turn in enumerate(scenario.turns, start=1):
        _apply_behaviors(engine, turn.connector_behaviors or {})
        try:
            result = engine.turn(scenario.name, turn.user, principal)
        except Exception as exc:  # a turn that raises is a failure of the scenario
            failures.append(f"turn {position}: the turn raised {type(exc).__name__}: {exc}")
            break
        ran += 1
        replies.append(result.reply)
        events.extend(result.events)
        failures.extend(
            _check_turn(position, turn.expect, result, engine.receipts(scenario.name))
        )
    failures.extend(_check_script(provider, scenario.provider_script, events))
    failures.extend(_check_receipts_are_stated(engine.receipts(scenario.name), replies))
    if scenario.learning is not None:
        failures.extend(
            _check_learning(
                scenario.learning,
                scenario.name,
                engine,
                scenario_settings,
                Path(state_dir),
                publish_root,
            )
        )
    return ScenarioResult(
        name=scenario.name,
        level=scenario.level,
        passed=not failures,
        failures=failures,
        turns=ran,
    )


def run_suite(
    directory: Path, settings: Settings, *, build: EngineBuilder | None = None
) -> SuiteReport:
    """Run every scenario of a directory, each against its own state."""
    return SuiteReport.of(run_directories([Path(directory)], settings, build))


def run_directories(
    directories: list[Path], settings: Settings, build: EngineBuilder | None = None
) -> list[ScenarioResult]:
    """Run every scenario of several directories, each against its own state.

    One run identifier is drawn for the whole run, and each scenario is kept
    under `conformance/<run id>/<position>/<scenario>`, which the scenario
    purges when it ends.
    """
    results: list[ScenarioResult] = []
    run_id = new_run_id()
    with tempfile.TemporaryDirectory(prefix="oska-conformance-") as workspace:
        root = Path(workspace)
        for position, directory in enumerate(directories):
            for scenario in load_scenarios(directory):
                state_dir = root / str(position) / scenario.name
                namespace = conformance_namespace(run_id, f"{position}/{scenario.name}")
                results.append(
                    run_scenario(
                        scenario, settings, state_dir=state_dir, build=build, namespace=namespace
                    )
                )
    return results


def _check_turn(
    position: int,
    expect: Expect,
    result: TurnResult,
    receipts: list[WriteReceipt],
) -> list[str]:
    where = f"turn {position}"
    failures: list[str] = []
    kind = _route_kind(result)
    if kind != expect.kind:
        failures.append(f"{where}: routed as {kind!r}, the scenario expects {expect.kind!r}")
    for fragment in expect.contains:
        if fragment not in result.reply:
            failures.append(f"{where}: the reply lacks {fragment!r}: {result.reply!r}")
    if expect.outcomes is not None:
        produced = [outcome.kind for outcome in result.outcomes]
        if produced != expect.outcomes:
            failures.append(
                f"{where}: outcomes {produced}, the scenario expects {expect.outcomes}"
            )
    pending = result.pending.kind if result.pending is not None else None
    if pending != expect.pending:
        failures.append(f"{where}: pending {pending!r}, the scenario expects {expect.pending!r}")
    if expect.receipts is not None and len(receipts) != expect.receipts:
        failures.append(
            f"{where}: {len(receipts)} receipt(s), the scenario expects {expect.receipts}"
        )
    verification = str(receipts[-1].verification) if receipts else None
    if verification != expect.verification:
        failures.append(
            f"{where}: verification {verification!r}, "
            f"the scenario expects {expect.verification!r}"
        )
    if expect.directives is not None:
        failures.extend(_check_directives(where, expect.directives, result))
    return failures


def _check_directives(where: str, expected: list[dict], result: TurnResult) -> list[str]:
    """The directives of the turn, in order, against the ones the scenario lists.

    Procedure, moment and view must be equal; the listed fields must be present
    with the listed values, and any others the directive holds are not read.
    """
    produced = [directive.model_dump(mode="json") for directive in result.directives]
    if len(produced) != len(expected):
        return [
            f"{where}: {len(produced)} directive(s) {produced}, "
            f"the scenario expects {len(expected)}"
        ]
    failures: list[str] = []
    for position, (made, wanted) in enumerate(zip(produced, expected, strict=True), start=1):
        at = f"{where}: directive {position}"
        for key in ("procedure", "moment", "view"):
            if made.get(key) != wanted.get(key):
                failures.append(
                    f"{at}: {key} {made.get(key)!r}, the scenario expects {wanted.get(key)!r}"
                )
        held = made.get("fields") or {}
        for name, value in (wanted.get("fields") or {}).items():
            if name not in held:
                failures.append(f"{at}: no field {name!r}, the scenario expects {value!r}")
            elif held[name] != value:
                failures.append(
                    f"{at}: field {name!r} is {held[name]!r}, the scenario expects {value!r}"
                )
    return failures


def _check_learning(
    spec: LearningSpec,
    session_id: str,
    engine: Engine,
    settings: Settings,
    state_dir: Path,
    publish_root: Path,
) -> list[str]:
    """Draft a procedure from the session, move it, and check where it landed.

    The refused transition is tried first, while the proposal is still a draft,
    so what refuses it is the missing actor and not an edge that leads nowhere.
    """
    if not spec.draft_from_session:
        return ["learning: a scenario can only start from a session for now"]
    store = ProposalStore(state_dir / PROPOSALS_DIRNAME)
    drafting = ScriptedModelProvider(spec.provider_script) if spec.provider_script else None
    proposal = draft_from_session(
        session_id,
        engine.state(session_id).history,
        engine.receipts(session_id),
        engine.registry,
        drafting,
        schema_dir=settings.schemas_dir,
    )
    store.save(proposal)
    failures: list[str] = []
    if drafting is not None and drafting.remaining():
        failures.append(
            f"learning: the drafting script was not fully consumed: "
            f"{drafting.remaining()} step(s) left"
        )
    move = _move(store, engine, settings, publish_root)
    if spec.expect_refused is not None:
        failures.extend(_check_refused(spec.expect_refused, proposal, store, move))
    for step in spec.transitions:
        try:
            proposal = move(proposal.proposal_id, step.to, step.by, step.note)
        except (KeyError, LifecycleError, ValueError) as exc:
            failures.append(f"learning: the move to {step.to!r} was refused: {exc}")
            break
    failures.extend(_check_learning_expectations(spec.expect, proposal, engine, publish_root))
    return failures


def _move(
    store: ProposalStore,
    engine: Engine,
    settings: Settings,
    publish_root: Path,
) -> Any:
    """One transition, bound to this scenario's catalog and publication root."""

    changes = ChangeRequestStore(publish_root.parent / "changes")

    def _apply(proposal_id: str, to: str, by: str, note: str = "") -> SkillProposal:
        moved = transition(
            store,
            proposal_id,
            to,
            by,
            note,
            registry=engine.registry,
            changes=changes,
            schema_dir=settings.schemas_dir,
        )
        if moved.state is ProposalState.published:
            _review(changes, publish_root, by)
        return moved

    return _apply


def _review(changes: ChangeRequestStore, publish_root: Path, by: str) -> None:
    """Apply what publication composed, the way a reviewer would.

    The runtime composes a change request and stops; putting it into a catalog
    is a person's act. A scenario that expects a published procedure is asking
    for both halves, so the runner performs the second one explicitly and under
    the name of whoever asked for the move.
    """
    publish_root.mkdir(parents=True, exist_ok=True)
    for request in changes.list(ChangeRequestState.open):
        changes.apply(request.request_id, publish_root, by=by)


def _check_refused(
    refused: RefusedTransition,
    proposal: SkillProposal,
    store: ProposalStore,
    move: Any,
) -> list[str]:
    before = proposal.state
    try:
        move(proposal.proposal_id, refused.to, refused.by)
    except (KeyError, LifecycleError, ValueError):
        held = store.get(proposal.proposal_id)
        if held.state is not before:
            return [
                f"learning: the refused move to {refused.to!r} still changed the state to "
                f"{held.state.value!r}"
            ]
        return []
    return [
        f"learning: the move to {refused.to!r} by {refused.by!r} was allowed, "
        "and the scenario expects it to be refused"
    ]


def _check_learning_expectations(
    expect: LearningExpect,
    proposal: SkillProposal,
    engine: Engine,
    publish_root: Path,
) -> list[str]:
    failures: list[str] = []
    if proposal.state.value != expect.state:
        failures.append(
            f"learning: the proposal is {proposal.state.value!r}, "
            f"the scenario expects {expect.state!r}"
        )
    if expect.quality_passed is not None:
        held = quality.passed(proposal.quality)
        if held != expect.quality_passed:
            listed = "; ".join(proposal.quality.issues) or "nothing"
            failures.append(
                f"learning: the quality check {'passed' if held else 'failed'} "
                f"({listed}), the scenario expects "
                f"{'a pass' if expect.quality_passed else 'a failure'}"
            )
    if expect.published_id is not None:
        failures.extend(_check_published(expect.published_id, engine, publish_root))
    return failures


def _check_published(skill_id: str, engine: Engine, publish_root: Path) -> list[str]:
    """The catalog must hold the published procedure, read back from the publication root."""
    skill = engine.registry.get(skill_id)
    if skill is None:
        return [f"learning: the catalog does not hold {skill_id!r} after publication"]
    if not skill.is_published:
        return [f"learning: {skill_id!r} is in the catalog as {skill.frontmatter.status.value!r}"]
    if publish_root.resolve() not in skill.path.resolve().parents:
        return [
            f"learning: {skill_id!r} is still the copy at {skill.path}, "
            "so nothing was picked up from the publication root"
        ]
    return []


def _check_script(
    provider: ScriptedModelProvider,
    script: list[dict],
    events: list,
) -> list[str]:
    """The script must be spent, and each classification answer must have been routed.

    Every step of a script is taken in order whatever the call was, so a turn
    that never reached the model router would silently hand its answer to the
    planner. Counting the two tells them apart.
    """
    failures: list[str] = []
    remaining = provider.remaining()
    if remaining:
        failures.append(f"the script was not fully consumed: {remaining} step(s) left")
    scripted = sum(1 for step in script if "kind" in (step.get("response") or {}))
    routed = sum(
        1 for event in events if event.kind == "route" and event.data.get("source") == "model"
    )
    if scripted != routed:
        failures.append(f"{scripted} scripted classification(s), {routed} performed")
    return failures


def _check_receipts_are_stated(receipts: list[WriteReceipt], replies: list[str]) -> list[str]:
    """Every write is named by a reply, and none is called verified unless it is.

    Both halves are checked on every scenario. A write the session performed
    and never mentioned is as much a failure as one it overstated: the reply is
    the only account the user gets, and the receipt is what it has to come from.
    """
    failures: list[str] = []
    for receipt in receipts:
        naming = [reply for reply in replies if receipt.receipt_id in reply]
        if not naming:
            failures.append(f"no reply names receipt {receipt.receipt_id}")
            continue
        if str(receipt.verification) == VERIFIED:
            continue
        for reply in naming:
            claim = reply.lower()
            for negation in NEGATIONS:
                claim = claim.replace(negation, "")
            if VERIFIED in claim:
                failures.append(
                    f"receipt {receipt.receipt_id} is called verified while it is "
                    f"{receipt.verification}"
                )
    return failures


def _apply_behaviors(engine: Engine, behaviors: dict) -> None:
    for integration_id, operations in behaviors.items():
        connector = engine.connectors.get(integration_id)
        for operation, behavior in operations.items():
            connector.set_behavior(operation, behavior)


def _route_kind(result: TurnResult) -> str | None:
    """What the turn was taken to be, or `guardrail` when it never got that far.

    A guardrail at the input boundary ends the turn before it is classified, so
    there is no route kind to report and the scenario names the guardrail
    instead.
    """
    for event in result.events:
        if event.kind == "route":
            return str(event.data.get("kind"))
        if event.kind == "guardrail" and event.data.get("action") == "blocked":
            return "guardrail"
    return None
