# Cassettes

A cassette is one recorded session: every completion with the request that
produced it and the answer that came back, and every embedding call with its
texts and vectors. Record one by running a session against a live endpoint with
`oska run --provider openai_compatible --record examples/ops/cassettes/<name>.json`,
then turn it into a scenario script with `oska cassette to-script`.

`restart-in-italian.json` was recorded against a hosted model: a restart asked
for in Italian, approved with "sì", then asked for again. It stops for approval
with the exact payload, the approval sends one verified write, and the repeat is
recognized from the ledger instead of being sent twice.
`tests/unit/test_live_cassette.py` replays it offline. Its reply expectations
end before the receipt identifier, which is new on every run — cut there
automatically, not by hand; see "Recording a real session" in
`docs/conformance.md`.
