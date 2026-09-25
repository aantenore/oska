"""A session recorded against a real model, replayed offline.

`examples/ops/cassettes/restart-in-italian.json` holds what a hosted model
answered when a person asked, in Italian, to restart a stuck service, approved
the restart, and then asked for it again. Replaying it runs the runtime exactly
as it ran then, with no network: the plan the model wrote must still hold up
against the catalog, the write must still stop for its exact payload, the
read-back must still confirm it, and the repeated request must still be
recognized instead of being sent twice.
"""

from __future__ import annotations

from pathlib import Path

from oska.audit.sink import MemorySink
from oska.config import Settings
from oska.engine.factory import build_engine
from oska.planner.recording import ReplayProvider

REQUEST = "Il servizio search in staging è bloccato, riavvialo per favore"


def test_a_session_recorded_against_a_real_model_replays_offline(
    example_settings: Settings, repo_root: Path
) -> None:
    cassette = repo_root / "examples" / "ops" / "cassettes" / "restart-in-italian.json"
    provider = ReplayProvider(cassette)
    engine = build_engine(example_settings, provider=provider, audit_sink=MemorySink())

    asked = engine.turn("live", REQUEST)
    assert [outcome.kind for outcome in asked.outcomes] == ["read", "approval_pending"]
    assert asked.pending is not None and asked.pending.approval is not None
    assert asked.pending.approval.exact_payload == {"service": "search"}

    approved = engine.turn("live", "sì")
    receipts = [outcome for outcome in approved.outcomes if outcome.kind == "receipt"]
    assert len(receipts) == 1
    assert receipts[0].data["verification"] == "verified"
    assert engine.state("live").language == "it"

    again = engine.turn("live", REQUEST)
    assert [outcome.kind for outcome in again.outcomes] == ["ledger_hit"]
    assert again.outcomes[0].receipt_id == receipts[0].receipt_id
    assert len(engine.receipts("live")) == 1
    assert provider.remaining() == 0
