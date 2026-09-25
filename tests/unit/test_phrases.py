"""The phrase catalog: what the runtime says in its own words, in the language of the turn.

The deterministic parts of a reply (the approval request, the line that names a
receipt, the sentence for a capability that is down) come from a catalog keyed
by language. English is the reference and is complete; a configured catalog is
laid over the packaged one phrase by phrase; a translation that names other
placeholders, or an approval word the rules do not accept, is refused at load.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oska.audit.sink import MemorySink
from oska.catalog.resolver import CapabilityResolver
from oska.config import (
    ReplyConfig,
    RouterRules,
    Settings,
    load_runtime_config,
)
from oska.conformance.runner import _check_receipts_are_stated
from oska.contracts.common import ConnectorResult, PendingApproval, PendingInfo
from oska.contracts.plan import Plan
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.engine import outcomes as build
from oska.engine import phrases as phrases_module
from oska.engine.factory import build_engine
from oska.engine.outcomes import receipt_lines
from oska.engine.phrases import REQUIRED, PhraseCatalogError, Phrases, load_phrases
from oska.engine.reply import ReplyBuilder
from oska.planner.scripted import ScriptedModelProvider

CONTAINER_RUNTIME = "integration.container_runtime"


def _write(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _rules(repo_root: Path) -> RouterRules:
    return load_runtime_config(repo_root / "configs" / "runtime.json").router.rules


def _receipt(verification: VerificationStatus, receipt_id: str = "r-1") -> WriteReceipt:
    return WriteReceipt(
        receipt_id=receipt_id,
        session_id="s",
        ledger_key="k",
        integration_id=CONTAINER_RUNTIME,
        operation="service.restart",
        payload={"service": "search"},
        status="executed",
        verification=verification,
        verification_evidence={"comparisons": {"service.status": {"match": True}}},
        created_at=datetime.now(UTC),
    )


def _pending(approvals_required: int = 1, approvals_given: int = 0) -> PendingInfo:
    return PendingInfo(
        kind="approval",
        approval=PendingApproval(
            approval_id="a-1",
            runbook_id="runbook.service_restart",
            plan=Plan(runbook_id="runbook.service_restart"),
            op_index=1,
            exact_payload={"service": "search"},
            payload_hash="h",
            integration_id=CONTAINER_RUNTIME,
            operation="service.restart",
            approvals_required=approvals_required,
            approvals_given=approvals_given,
        ),
    )


# Loading


def test_the_packaged_catalog_holds_every_phrase_in_english_and_in_italian() -> None:
    phrases = Phrases.load()
    assert phrases.languages() == ("en", "it")
    for language in phrases.languages():
        assert set(phrases.catalog[language]) == set(REQUIRED), language


def test_the_packaged_catalog_is_accepted_by_the_shipped_approval_rules(
    repo_root: Path,
) -> None:
    Phrases.load(rules=_rules(repo_root))


def test_a_configured_catalog_is_laid_over_the_packaged_one_phrase_by_phrase(
    tmp_path: Path,
) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"greeting": "Salve. {scope}"}})
    phrases = Phrases.load(mine)
    assert phrases.say("it", "greeting", scope="S") == "Salve. S"
    assert phrases.say("it", "when_now") == "subito"
    assert phrases.say("en", "greeting", scope="S") == "Hello. S"


def test_a_missing_phrase_falls_back_to_the_default_language_then_to_english(
    tmp_path: Path,
) -> None:
    mine = _write(tmp_path / "phrases.json", {"fr": {"when_now": "maintenant"}})
    english_default = Phrases.load(mine)
    assert english_default.say("fr", "when_now") == "maintenant"
    assert english_default.say("fr", "rejected", operation="op") == (
        "op was not run, because you rejected it."
    )
    italian_default = Phrases.load(mine, default_language="it")
    assert italian_default.say("fr", "when_now") == "maintenant"
    assert italian_default.say("fr", "rejected", operation="op") == (
        "L'operazione op non è stata eseguita, perché l'hai rifiutata."
    )
    assert italian_default.say("de", "when_now") == "subito"


def test_a_translation_naming_other_placeholders_is_refused_at_load(tmp_path: Path) -> None:
    mine = _write(
        tmp_path / "phrases.json",
        {"it": {"approval_tail": "Rispondi {yes} per approvarla, oppure {reject}."}},
    )
    with pytest.raises(PhraseCatalogError) as refused:
        Phrases.load(mine)
    message = str(refused.value)
    assert "'it'" in message
    assert "approval_tail" in message
    assert "{reject}, {yes}" in message
    assert "{approve}, {reject}" in message


def test_a_phrase_the_code_asks_for_and_english_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phrases_module, "REQUIRED", (*REQUIRED, "a_sentence_nobody_wrote"))
    with pytest.raises(PhraseCatalogError, match="missing a_sentence_nobody_wrote"):
        Phrases.load()


def test_a_phrase_the_code_never_asks_for_is_refused(tmp_path: Path) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"greting": "Ciao. {scope}"}})
    with pytest.raises(PhraseCatalogError, match="unknown phrase 'greting'"):
        Phrases.load(mine)


def test_a_list_of_words_given_as_a_sentence_is_refused(tmp_path: Path) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"negations": "non verificat"}})
    with pytest.raises(PhraseCatalogError, match="negations must be a list of words"):
        Phrases.load(mine)


# The words an approval request tells a person to type


def test_an_approval_word_the_rules_do_not_accept_is_refused(
    repo_root: Path, tmp_path: Path
) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"approve_word": "vai"}})
    with pytest.raises(PhraseCatalogError) as refused:
        Phrases.load(mine, rules=_rules(repo_root), spoken=("it",))
    assert "approve_word 'vai'" in str(refused.value)
    assert "router.rules.approve_tokens" in str(refused.value)


def test_a_reject_word_the_rules_do_not_accept_is_refused(repo_root: Path) -> None:
    without_no = RouterRules(approve_tokens=["yes", "sì"], reject_tokens=["cancel"])
    with pytest.raises(PhraseCatalogError, match="reject_word 'no'"):
        Phrases.load(rules=without_no)


def test_only_the_languages_the_deployment_answers_in_are_checked() -> None:
    """An English deployment is not refused over the Italian words the packaged catalog holds."""
    english_only = RouterRules(approve_tokens=["yes"], reject_tokens=["no"])
    Phrases.load(rules=english_only, spoken=("en",))
    with pytest.raises(PhraseCatalogError, match="approve_word 'sì'"):
        Phrases.load(rules=english_only, spoken=("en", "it"))


def test_rules_left_empty_leave_the_answer_to_the_model_router() -> None:
    """With no approval tokens the rule layer decides nothing, so no word is checked."""
    Phrases.load(rules=RouterRules(), spoken=("en", "it"))


def test_a_deployment_whose_catalog_asks_for_an_unaccepted_word_is_not_built(
    example_settings: Settings, tmp_path: Path
) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"approve_word": "vai"}})
    runtime = load_runtime_config(settings=example_settings)
    runtime.reply.phrases = mine
    with pytest.raises(PhraseCatalogError, match="approve_word 'vai'"):
        build_engine(example_settings, runtime=runtime, audit_sink=MemorySink())


def test_the_deployment_loads_the_catalog_its_runtime_configuration_names(
    example_settings: Settings, tmp_path: Path
) -> None:
    mine = _write(tmp_path / "phrases.json", {"it": {"approval_head": "Serve un sì."}})
    runtime = load_runtime_config(settings=example_settings)
    runtime.reply.phrases = mine
    assert load_phrases(runtime, example_settings).say("it", "approval_head") == "Serve un sì."
    engine = build_engine(example_settings, runtime=runtime, audit_sink=MemorySink())
    assert engine.phrases.say("it", "approval_head") == "Serve un sì."


# What is said


def test_the_english_approval_request_is_unchanged(resolver: CapabilityResolver) -> None:
    builder = ReplyBuilder(None, resolver, ReplyConfig())
    assert builder.template("approval_request", [], _pending(), "en") == (
        "This changes something, so it needs your approval first.\n"
        'service.restart on integration.container_runtime with {"service":"search"}\n'
        "Reply yes to approve it, or no to leave it alone."
    )
    assert builder.template("approval_request", [], _pending(2, 1), "en").splitlines()[2] == (
        "1 of 2 approvals recorded. A second approval is required before anything runs."
    )


def test_the_italian_approval_request_names_the_italian_word_the_rules_accept(
    resolver: CapabilityResolver,
) -> None:
    builder = ReplyBuilder(None, resolver, ReplyConfig())
    assert builder.template("approval_request", [], _pending(2), "it") == (
        "Questa operazione modifica qualcosa, quindi serve prima la tua approvazione.\n"
        'service.restart su integration.container_runtime con {"service":"search"}\n'
        "Servono due approvazioni: questa è l'approvazione 1 di 2.\n"
        "Rispondi sì per approvarla, oppure no per lasciare tutto com'è."
    )


def test_a_language_the_catalog_does_not_hold_is_answered_in_the_default_language(
    resolver: CapabilityResolver,
) -> None:
    builder = ReplyBuilder(None, resolver, ReplyConfig())
    english = builder.template("approval_request", [], _pending(), "en")
    assert builder.template("approval_request", [], _pending(), "fr") == english


def test_the_english_receipt_line_is_unchanged() -> None:
    outcome = build.receipt_outcome(_receipt(VerificationStatus.verified))
    assert receipt_lines([outcome], Phrases.load(), "en") == [
        "service.restart ran and the read-back confirmed service.status. "
        "Receipt r-1 is verified."
    ]


def test_what_the_runtime_cancels_or_holds_back_is_said_in_italian() -> None:
    """The runtime's own reasons are phrases; the record keeps its wording for the audit."""
    phrases = Phrases.load()
    pending = PendingApproval(
        approval_id="a-1",
        runbook_id="runbook.service_restart",
        plan=Plan(runbook_id="runbook.service_restart"),
        op_index=0,
        exact_payload={"service": "search"},
        payload_hash="h",
        integration_id=CONTAINER_RUNTIME,
        operation="service.restart",
    )
    cancelled = build.cancelled_outcome(
        "the request under way was dropped, and nothing ran", phrase="cancelled_dropped"
    )
    moved = build.rejected_outcome(
        pending, "approval payload no longer matches the plan", phrase="rejected_payload_moved"
    )

    assert build.outcome_summary([cancelled, moved], phrases, "it") == [
        "la richiesta in corso è stata annullata e non è stato eseguito nulla",
        "L'operazione service.restart non è stata eseguita: il contenuto approvato non "
        "corrisponde più al piano.",
    ]
    assert build.outcome_summary([cancelled], phrases, "en") == [
        "the request under way was dropped, and nothing ran"
    ]
    assert cancelled.reason == "the request under way was dropped, and nothing ran"


def test_the_italian_receipt_line_inserts_every_value_verbatim() -> None:
    receipt = _receipt(VerificationStatus.failed, receipt_id="r-{9}")
    receipt.operation = "service.{restart}"
    line = receipt_lines([build.receipt_outcome(receipt)], Phrases.load(), "it")[0]
    assert line == (
        "L'operazione service.{restart} è stata eseguita, ma la rilettura l'ha smentita. "
        "La ricevuta r-{9} non è verificata."
    )


def test_the_italian_retry_delay_is_said_in_italian() -> None:
    outcome = build.unavailable_outcome(
        CONTAINER_RUNTIME, "service.status", ConnectorResult(ok=False, error="timeout")
    )
    outcome.data["retry_in_seconds"] = 12
    assert build.outcome_summary([outcome], Phrases.load(), "it") == [
        "Non è stato possibile raggiungere service.status su integration.container_runtime: "
        "timeout. La funzionalità è temporaneamente non disponibile e si può riprovare "
        "tra 12 secondi. Non è stato letto nulla e non è cambiato nulla."
    ]


def test_a_written_italian_reply_carries_the_italian_receipt_line(
    repo_root: Path, example_settings: Settings
) -> None:
    """The model writes the reply; the runtime still appends its receipt line, in Italian."""
    scenario = json.loads(
        (repo_root / "examples/ops/scenarios/20_other_language.json").read_text("utf-8")
    )
    engine = build_engine(
        example_settings,
        provider=ScriptedModelProvider(scenario["provider_script"]),
        audit_sink=MemorySink(),
    )
    asked = engine.turn("it-1", scenario["turns"][0]["user"])
    assert asked.reply.splitlines()[-1] == (
        "Rispondi sì per approvarla, oppure no per lasciare tutto com'è."
    )
    done = engine.turn("it-1", "sì")
    receipt_id = done.outcomes[0].receipt_id
    written, appended = done.reply.splitlines()
    assert written.startswith("Il servizio search è stato riavviato")
    assert appended.startswith("L'operazione service.restart è stata eseguita")
    assert appended.endswith(f"La ricevuta {receipt_id} è verificata.")


# The conformance check reads its words from the same catalog


def test_an_italian_reply_that_calls_an_unverified_write_verified_is_caught() -> None:
    receipt = _receipt(VerificationStatus.failed)
    overstated = "Fatto: il servizio è ripartito. La ricevuta r-1 è verificata."
    failures = _check_receipts_are_stated([receipt], [overstated], Phrases.load())
    assert failures == ["receipt r-1 is called verified while it is failed"]


def test_an_honest_italian_reply_about_an_unverified_write_passes() -> None:
    receipt = _receipt(VerificationStatus.failed)
    line = receipt_lines([build.receipt_outcome(receipt)], Phrases.load(), "it")[0]
    assert _check_receipts_are_stated([receipt], [line], Phrases.load()) == []


def test_the_english_claim_check_still_holds() -> None:
    receipt = _receipt(VerificationStatus.failed)
    phrases = Phrases.load()
    assert _check_receipts_are_stated([receipt], ["Receipt r-1 is not verified."], phrases) == []
    assert _check_receipts_are_stated([receipt], ["Receipt r-1 is verified."], phrases) == [
        "receipt r-1 is called verified while it is failed"
    ]


def test_claim_words_of_every_language_are_read(tmp_path: Path) -> None:
    mine = _write(
        tmp_path / "phrases.json",
        {"de": {"claim_verified": ["bestätigt"], "negations": ["nicht bestätigt"]}},
    )
    phrases = Phrases.load(mine)
    receipt = _receipt(VerificationStatus.failed)
    assert _check_receipts_are_stated([receipt], ["Beleg r-1 ist bestätigt."], phrases)
    assert not _check_receipts_are_stated(
        [receipt], ["Beleg r-1 ist nicht bestätigt."], phrases
    )
