# Service-level targets

One target per metric `GET /metrics` exposes, named the way the exposition
names it. `alerts.yml` carries one alerting rule per target, against the same
name; `tests/unit/test_metrics.py` fails the build if a name is added to one
of the three and not the other two, so this file cannot drift from the numbers
it is describing or the rules that watch them.

A target here is a starting point for a deployment's own history, not a
universal constant: the two sentences under each one say what the number
means and why the figure was picked, so an operator with different traffic or
a different risk tolerance can move it with the same reasoning rather than
guessing at the original author's intent.

## oska_turns

**Target:** at least one turn every 30 minutes in a deployment expected to be
live.

The number is a gauge: the turns the audit trail holds over the report window,
recomputed on every scrape. It falls when a session is forgotten or expired, so
it is watched for standing still, not for a rate of increase. A count that has
not moved over a window that should be busy is silence, and silence has two very
different causes: nobody is using the deployment, or nothing reaches it any
more. The metric cannot tell those apart, which is exactly why an operator has
to be woken up to find out which one it is. A deployment with no audit trail
exports no count at all rather than a zero.

## oska_latency_p95_ms

**Target:** at or below 2000 ms.

The 95th percentile is the number a person waiting on a turn actually feels
on a bad day, and two seconds is roughly where a conversational reply stops
feeling immediate. A model call dominates a turn's time, so a rising p95
without a rising p50 usually points at one slow dependency rather than a
systemic problem.

## oska_latency_p50_ms

**Target:** at or below 800 ms.

The median is the typical turn, and it should be fast because most turns take
the deterministic path through discovery and planning rather than waiting on
a chain of model calls. A rising median, unlike a rising p95, usually means
the slowdown is systemic and not one noisy outlier.

## oska_fallback_rate

**Target:** at or below 0.05.

A deterministic template stands in for the model only when no model is
configured or the plan it returned did not hold up, and neither should be
common in a working deployment. A rate climbing above one in twenty turns
means the provider is failing, timing out, or producing plans the runtime
keeps rejecting, not that fallback is doing its job.

## oska_verification_failure_rate

**Target:** at or below 0.01.

A read-back that does not confirm the write it followed is a correctness
problem, not ordinary noise, so the floor here is the tightest of the six. A
sustained rate above one in a hundred read-backs means writes are landing
differently than the runtime believes, which is worth paging on well before it
compounds.

## oska_guardrail_block_rate

**Target:** at or below 0.10.

Guardrails exist to refuse the rare request that should not proceed, so a
block rate that stays in the low single digits is the deployment behaving as
declared. A rate climbing past one in ten turns is either a guardrail catalog
tuned too aggressively against legitimate requests or a sustained pattern of
requests that should not be arriving in the first place, and both are worth
knowing about quickly.

## oska_unsupported_rate

**Target:** at or below 0.15.

This is not an error rate: a request that reaches no procedure is the product
signal that the catalog is missing something, and some baseline of it is
expected from people asking for things the deployment was never meant to do.
The target exists to catch a *rising* trend rather than to demand zero -- a
sustained climb above it is a catalog gap worth prioritizing, not a bug to
patch in the runtime.
