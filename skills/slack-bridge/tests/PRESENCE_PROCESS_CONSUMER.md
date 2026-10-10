# Separate-process Presence consumer

Run this explicit opt-in fixture with the candidate core checkout and a Python
environment containing that core's test requirements. The launcher must start
before pytest imports any core code. The test path must be absolute because the
launcher changes working directory to its own core checkout.

```sh
python3 -I -S /path/to/core/scripts/safe_test.py --temp-parent /tmp -- \
  /usr/bin/env OUROBOROS_PRESENCE_CORE_ROOT=/path/to/core \
  /path/to/test-python -m pytest -q -s \
  /absolute/bridge/skills/slack-bridge/tests/test_presence_process_consumer.py
```

An unset core root produces SKIP. The tests are marked `serial`. Temporary roots
and diagnostic files are retained, including on failure; this is configuration
isolation, not an OS sandbox. No operator settings, credentials, Slack account,
live model, live review launcher or installed skill is used.

The bridge consumer runs in a bounded driver process. Its supervised child runs
the real Host ASGI application over a loopback TCP socket under uvicorn. Synthetic
transport and behavior manifests, reviewed/enabled states, grants, token and
binding are seeded only in the fixture data directory. Token authentication,
binding admission, Presence gate with active cap 1, Host in-flight reservations,
execution registry, continuation and replay all execute production code.

`LoopAgent` is an explicit test lifecycle wrapper, not the complete
`OuroborosAgent` bootstrap. It carries the admitted capability ceiling and inline
round limit into the production tool registry, `run_llm_loop`, model-wait scope,
acceptance coordinator, review custody and terminal pipeline. The only replaced
cognition callables are `loop.call_llm_with_retry` and
`review_substrate._review_route_executor`; both send HTTP requests to the local
deterministic provider. This does not exercise the production model HTTP client,
provider retries, a configured real reviewer roster, memory bootstrap or paid
post-task synthesis. The provider records actual model request messages and tool
schemas and holds the review response until the consumer releases it.

The child scenarios extend that lifecycle wrapper with a synthetic confirmed
supervisor handoff (`_swarm_handoff_attempt`) and a binding-scoped child task row,
then complete that child through the normal task-result writer. They exercise no
child scheduler or real child model. The refusal scenario supplies a canonical
`resource_refusal_no_resend` parent terminal; production `run_presence_turn` and
the Host route produce the HTTP 409 and its admitted `work_ref`.

The production Socket Mode envelope handler, store, inbound/outbound workers,
Slack client and loopback Host adapter run unchanged. Synthetic Slack Web API
responses use `httpx.MockTransport`; no Socket Mode gateway connection or physical
Slack send is claimed. The reporting-0 cases explicitly opt out of delivery
receipts after capability discovery while retaining continuation-v1 negotiation.

The nine cases establish:

| Scenario | Observed consumer behavior |
| --- | --- |
| Blocking, reporting 0 and 1 | Held output; next same-thread event completes while review is pending; same author resumes with event identity and words; output delivered once. |
| Early Advisory FAIL, reporting 0 and 1 | Early output before review; next event completes; same author receives FAIL, accepts criticism and sends one correction. Reporting 1 also carries the confirmed early-delivery receipt into reentry. |
| Six parked authors | Six same-skill turns obtain HTTP 200 and park simultaneously despite the normal five-request in-flight budget; all later finish, with six distinct selection identities for identical text. |
| Full queued facts | Twelve still-unsubmitted events, each over 1,000 characters, reach the author through the production queue-refresh POST and its frozen `get_task_result` source reader. The model retrieves five pages, checks the complete SHA-256, and sees every exact text before finalizing within its original round limit. |
| Late promoted child, live parent | Initial continuation has no child; a first Blocking FAIL returns the author to revise, and its new handoff is retained at the second real review park. A reopened bridge discovers the child from the pending author poll and delivers its result while the parent remains parked. |
| Late promoted child, dead parent | After the second park and child completion, the actual isolated Host process is killed and restarted with its retained data. Core's persisted controller witness projects the old author as interrupted; the bridge still discovers and delivers the child and retains the unresolved parent failure. |
| Admitted child in HTTP 409 | The real Host refuses the original event with `presence_resources_unavailable`, `disposition=retry` and an admitted child ref. The bridge durably polls that child across restart, sends its result once and retains the original refusal without resubmitting the event. |

Each child case reopens the bridge store, adapter, workers and transport objects
twice, discarding every in-memory bridge cache. This is a bridge state restart
inside the same driver process, not a second bridge OS process. The dead-parent
case separately restarts the actual Host OS process. Duplicate inbound delivery
and later polls must leave exactly one child outbox row and one original-event
submission.

Primary cases also test wrong-token/wrong-binding refusal, durable initial replay
before and after completion with no extra model request, duplicate Slack event
handling, completed-v1 output references, no direct-send tool in model schemas,
and survival of the newer conversation pointer after the older author's terminal.
Evidence includes `consumer-facts.json`, full model/review requests, wire requests and responses,
outbox and task records, process exit, and before/after hashes of every imported
core and bridge source file. Every normal Host exit must be 0; only the explicit
dead-parent scenario kills its first Host. Driver timeout
first raises its SIGTERM handler through cleanup; only an unresponsive cleanup
reaches the bounded kill fallback. Failed-run logs remain available for diagnosis.

These nine cases do not establish physical Slack exactly-once delivery or unknown
send recovery, disconnect replay, real promoted-child dispatch, proactive initiation,
Stop/Panic, bridge OS crash recovery, manual continuation, or platform-specific native
behavior. Those need the separate bridge/core seam matrix and independent receipts;
passing this fixture must not be represented as their execution.
