---
id: runbook.alert_silence
type: runbook
version: 1
status: published
description: Silences one firing alert for a bounded number of minutes while the cause is being worked on.
triggers:
  - silence the alert for 30 minutes
  - mute the paging for checkout
  - stop this alert from firing while we fix it
  - snooze the alert
  - silenzia l allarme
  - metti in pausa la notifica
entities:
  - entity.alert
integrations:
  - integration.alerting
capabilities:
  reads:
    - alert.list
  writes:
    - alert.silence
runtime:
  action: silence an alert
  action_on_selection: silence it
  order: 5
policy: policy.json
---

## Purpose

Stop an alert paging the rotation while the work it is pointing at is already
in hand. Silencing buys quiet, nothing else: the condition the rule watches is
unchanged.

## Preconditions

- The alert is firing and has been read, so the rule and the service behind it
  are known.
- Someone is already working the cause, or the alert has been confirmed as
  noise.
- The silence covers the work in flight and no more. Long silences outlive the
  memory of who set them.

## Steps

1. Read `alert.list` for the service and identify the exact alert.
2. Decide the duration from the work in flight, not from convenience.
3. State the alert identifier and the number of minutes, and get approval.
4. Call `alert.silence` with that exact pair.
5. Read `alert.list` again for the same alert.

## Verification

The read-back must show the alert in state `silenced`. If it still shows
`firing`, the silence did not take: say so plainly rather than assuming the
call worked.

## Rollback

Silences expire on their own. To end one early, silence the alert again for one
minute, or clear it in the alerting system by hand.
