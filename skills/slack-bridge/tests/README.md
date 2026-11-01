# Slack bridge continuation tests

The normal bridge suite exercises production parser, adapter, SQLite queues,
workers and Slack client against deterministic HTTP fixtures:

```sh
python3 -I -S "$CORE_ROOT/scripts/safe_test.py" --temp-parent /tmp -- \
  "$TEST_PYTHON" -m pytest -q "$BRIDGE_ROOT/skills/slack-bridge/tests" \
  --ignore="$BRIDGE_ROOT/skills/slack-bridge/tests/test_presence_process_consumer.py"
```

Set these variables to an isolated core candidate, this bridge checkout and a
dependency-only Python environment. The launcher prints disposable state roots
and changes its working directory to core, so the bridge test path is absolute.
It is not an OS sandbox. Test state is retained for diagnosis.

The [separate-process consumer](PRESENCE_PROCESS_CONSUMER.md) documents the
opt-in command, its nine scenarios, retained evidence and exact production versus
synthetic boundaries. An unset core root reports SKIP, not consumer evidence.

The [inbound lease boundary](INBOUND_LEASE.md) documents the finite first-poll
budget regression, typed lease-loss containment, tests and remaining ownership
limits.

`test_presence_continuation.py` separately controls blocked author/child HTTP
polls, promoted child plus parent tail, equal-text selection identities,
replay/disconnect, legacy delivery, interrupted-author projection and observation
ACK loss. Those Host answers are scripted; they are not additional real-author
process scenarios. These cases use one worker without a role, which submits and
polls in a single claim.

`test_presence_v2_intake.py` runs the companion's split workers (input and
observation roles, plus `BridgeRuntime` loops for the flood case) against
scripted version 2 Host answers. It covers negotiation by
`presence_continuation_versions`. It covers the startup FIFO until the custody
ACK, and an input worker that ends at the ACK without polling. A slow or blocked
author GET must not hold input of its own or other threads, including a flood
of twelve arrivals in arrival order. A continuous-inflow witness on the
`BridgeRuntime` loops keeps new events arriving faster than they can be
submitted. The author's reply must still be posted while input is queued, and
every arrival must still reach Host once, in order. Typed 429
`presence_capacity_full` refusals keep the same head byte for byte, with exact
retry schedules, while other threads proceed. The Host's `retry_after` is a
floor that is never shortened; only the bridge's own backoff is capped (hint
45 s stays 45 s). Further cases cover `output_ref` deduplication across
polls, terminal, child and restart, a lost ACK replayed once, an unknown Slack
send never repeated, and Stop/retained cancellation without speech or
resubmission. The rest cover a late child, anchored outputs replying in the
event's own thread, version 1 fallback and version-echo mismatches. These are
consumer tests against a synthetic Host, not the Host's own v2 implementation.

Late promotion cases discover `child_work_ref` on pending and interrupted polls,
then reopen the store and adapter with the parent slow, unavailable or dead. The
child finishes independently, including cancellation before its first reply and
an uncertain Slack send that must not repeat. `test_host_refusals.py` covers typed
turn refusals with admitted work, retained refusal facts across restart, child
selection identity, same-thread progress and explicit retries without an attempt
cap. These tests run the actual adapter, SQLite store and inbound/outbound workers
against scripted Host HTTP responses.

`test_send_custody.py` uses production outbound code and a synthetic provider.
Its crash case exits a separate child after provider acceptance and before the
local receipt checkpoint. Recovery settles the marked row as uncertain without
resending. Other cases cover cancellation, migration, stale leases and known
no-effect retries. Local argument refusals and unreadable/empty upload input
exercise the real client and worker: zero provider requests, a failed receipt,
and no retry after restart or duplicate enqueue. OSError and lost-response
controls at generic dispatch and all three upload phases stay uncertain without
resending. This establishes the bridge's attempt boundary, not Slack
exactly-once delivery.

Proactive initiation, explicit manual author continuation, Stop/Panic and
cancellation during core reacquisition remain core test obligations. The child
fixture covers Host death/restart at its second parked-author boundary, with
synthetic child admission/completion. Full application bootstrap, configured
review qualification, platform/native behavior and live adoption require their
own evidence. Run logs and dated pass counts belong in the source-bound handoff,
not in this maintained test description.

`test_retained_custody.py` covers retained parent and real child observations,
FIFO release, restart and late-output deduplication with real adapter/store/workers.
It also checks typed blocked/retry/rejected and untyped HTTP failures. The Host
responses in this module are scripted; cross-repository Host composition is a
separate verification, not implied by these fixtures.

`test_reactions.py` runs reaction facts and writes through the actual parser, store,
workers, Slack client and adapter against scripted Slack and Host HTTP. It covers
exact target lookup, the one narrow read on a miss, capability gating, withheld
observations that are never replayed, applied, no-op, refused, uncertain and held
writes, frozen reports at the moment they would be sent, and the read-only
`slack_reactions` and `slack_thread` consumers. The per-message write lane is
covered too. A held reaction holds back only later writes to its own message.
The cases include rows an earlier version queued per channel (pending, held, leased,
started and expired), a lease taken before the move, a row an older process queues
after the store opened, a target recorded after enqueue, cancellation, and
concurrent claimers. `slack_api` calls of `reactions.add` and `reactions.remove`
are covered through the registered tool (POST and GET), and as older rows, live
or leased, that share the dedicated tools' message lane. When the importable `ouroboros` carries the reaction validators, each fact and
receipt also passes them; otherwise those checks are skipped. The repository's
`tests/test_reaction_core_consumer.py` is the opt-in run through a real core's routes.
