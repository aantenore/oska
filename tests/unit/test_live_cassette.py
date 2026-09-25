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

import json
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


def test_the_same_cassette_replays_with_its_expectations_uncut(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    """The cut is applied when a cassette is loaded, so an older, uncut recording still replays.

    This restores, in a temporary copy, the receipt id the committed cassette
    was hand-cut to drop before this cut existed — what a recording made before
    the automatic cut would have written. `ReplayProvider` must cut it again on
    load rather than depend on it having been cut when it was written.
    """
    cassette = repo_root / "examples" / "ops" / "cassettes" / "restart-in-italian.json"
    steps = json.loads(cassette.read_text(encoding="utf-8"))
    for step in steps:
        if "expect_contains" not in step:
            continue
        last_user = next(
            message["content"]
            for message in reversed(step["request"]["messages"])
            if message["role"] == "user"
        )
        step["expect_contains"] = last_user.strip()[:60]
    uncut = tmp_path / "uncut.json"
    uncut.write_text(json.dumps(steps), encoding="utf-8")

    provider = ReplayProvider(uncut)
    engine = build_engine(example_settings, provider=provider, audit_sink=MemorySink())

    asked = engine.turn("live", REQUEST)
    assert asked.pending is not None and asked.pending.approval is not None

    approved = engine.turn("live", "sì")
    receipts = [outcome for outcome in approved.outcomes if outcome.kind == "receipt"]
    assert len(receipts) == 1

    again = engine.turn("live", REQUEST)
    assert again.outcomes[0].receipt_id == receipts[0].receipt_id
    assert provider.remaining() == 0
