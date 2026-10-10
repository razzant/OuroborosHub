# Inbound continuation lease boundary

An inbox claim has a 2,100-second budget. A newly submitted continuing turn
persists its Host reference, then refreshes that budget with the same row/token
check used for inbox checkpoints. This occurs once, before the finite
queue-observation/poll phase. A retry already obtains a fresh claim and does not
renew again. Initial output still enters the outbox before either HTTP poll;
staged files, source identity, Host references and output identities are unchanged.

The regression used 297 logical seconds of file staging, 1,790 of submit, and
5 of GET. The legacy deferred consumer released its row at 2,092 seconds. The
continuing consumer's additional 9-second queue report pushed it to 2,101,
allowing another worker to claim the row before its next checkpoint. Refreshing
the owned lease at the durable reference boundary gives that finite phase its
own budget without a timer, extra queue or a change to author lifetime.

Inbox checkpoint, terminal and retry token mismatches raise `InboxLeaseLost`.
The inbound attempt contains only this typed condition, including when it arises
inside the existing failure or retry handlers. A stale checkpoint does not enter
the generic retry handler. The obsolete attempt ends without another write using
its token; the newer owner's row and companion workers remain active. SQLite or
programming errors are not converted to lease loss. Existing handling of ordinary
Host/transport failures is retained, and unrelated errors raised by the failure
or retry handlers still propagate. Outbound lease handling is unchanged.

`test_inbound_lease.py` checks current/stale ownership, mutation preservation,
handler boundaries and genuine-error propagation. `test_inbound_lease_runtime.py`
uses the actual store, adapter, Slack client and four inbound worker loops with
synthetic HTTP transports and a logical store clock. It checks the finite-overhead
case, immediate output and continued processing after a permitted takeover.
Existing continuation tests cover independent author/child polls, replay,
corrections, reporting modes and delivery identity. The separate Host fixture is
described in [PRESENCE_PROCESS_CONSUMER.md](PRESENCE_PROCESS_CONSUMER.md).

The poll is finite: one author and at most two distinct child GETs per iterator,
plus the queue-observation POST. A pending result releases the inbox for a later
fresh claim. The author's total lifetime is not the lifetime of one inbox lease.
HTTP timeouts are inactivity/phase settings, not an aggregate wall-clock bound.
The synthetic elapsed times do not represent a physical network endurance test.

This change does not prevent an active owner from being displaced after long
staging, a suspended process, a clock step or a sufficiently long finite request.
It neither repairs staging's lack of an aggregate deadline nor provides a
live-owner guarantee. Those broader inbound ownership questions remain with
Hub105; outbound ownership (Hub123), unknown-send recovery, malformed Settings
POST and widget metrics are separate from this repair. No automatic author
restart, resend of an unknown provider effect, parked-author cap, or mandatory
poll/delivery ordering is introduced.
