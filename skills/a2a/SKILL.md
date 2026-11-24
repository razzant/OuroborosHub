---
name: a2a
description: Agent-to-Agent protocol bridge for Ouroboros. Provides a local A2A-compatible server plus client tools that send durable letters to other A2A agents over owner-managed links and read their answers later.
version: 2.0.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, tool, route, widget, companion_process, presence]
env_from_settings: []
when_to_use: User asks to communicate with another A2A-compatible agent, discover an agent card, send an A2A message, check A2A task status, or expose this Ouroboros instance as an A2A peer.
timeout_sec: 120
install_specs:
  - kind: pip
    package: "protobuf<6"
  - kind: pip
    package: "a2a-sdk[http-server]>=1.0.0,<2.0.0"
companion_processes:
  - name: a2a_server
    command: [python3, scripts/a2a_daemon.py]
    runtime: python3
    restart_policy: on_failure
  - name: a2a_letters
    command: [python3, scripts/a2a_watch.py]
    runtime: python3
    restart_policy: on_failure
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: A2A
    destination: vendor:A2A
    scope: account
    icon: ui/icon.svg
    wakes: [direct_message, from_bot]
    source: {route: status, refresh_s: 60}
    settings: a2a
tools:
  - name: discover
    effect: read
    target: url
  - name: send
    effect: message
    target: url
    conversation: context_id
    thread: task_id
    text: message
    late_result: true
  - name: status
    effect: read
    target: url
    conversation: task_id
---

# A2A skill

Note for reviewers: two companions are declared in this manifest's
frontmatter under `companion_processes`, both runtime `python3` with
restart policy `on_failure`: `a2a_server` (`[python3, scripts/a2a_daemon.py]`,
the receiving server) and `a2a_letters` (`[python3, scripts/a2a_watch.py]`,
the late-answer watch, which imports neither the server nor its SDK). Both
are registered at load via `api.register_companion_process(...)`. The host's
extension loader resolves the descriptors from the frontmatter; review-prompt
manifest summaries may omit the `companion_processes` field, so consult
the frontmatter above for the authoritative declaration.

The `widget` permission is for the Settings section that `plugin.py`
registers with `api.register_settings_section(...)`: the host requires
`widget` for Settings sections as well as for UI tabs. The skill registers
no UI tab.

`send` declares `target: url` on purpose. The host treats a message tool as
the place's in-conversation door only when every target lies inside the
conversation identity; a peer URL does not, so the A2A place stays
question-and-answer and its turn's final text is the answer the peer
receives. Addressing peers by conversation ids alone would make every A2A
answer silent. `text: message` names the words the host's checks read, and
`late_result: true` asks a host that brings late answers back for its
reference to each `send` call (only `send` declares it).

This skill moves Ouroboros's Agent-to-Agent protocol support out of the
core runtime. Its three tools work without the server companion, which is
only the door through which other agents reach this install:

- `discover` — fetch another agent's Agent Card; with no url, list the saved links.
- `send` — send a letter and return its handle.
- `status` — read a letter, list the letters this task or chat asked, or read a peer's task as it is now.

## Links

A link is one agent this install talks to, saved in Settings → A2A (or in
`links.json` in the skill's state folder on an install without settings
pages). It has an id (the name tools use for it), a name shown as the
author of its messages, and either side or both:

- **its address** and the user name and password this install sends there.
  The address is used exactly as written, its path included (a trailing
  slash too; none means `/`), so save the one the peer's Agent Card gives.
  `send` and `status` use the password only at that exact endpoint (scheme,
  host, port, path). For `discover`, explicitly name the saved link: the only
  credentialed requests are GETs to its `/.well-known/agent-card.json` and
  legacy `/.well-known/agent.json`, under the endpoint's path. Discovery by
  a bare URL is anonymous. A redirect or an address advertised in a card
  never receives the password. Editing the saved address drops the password
  and CA file until they are typed again;
- **its password for writing here**, typed by the owner (at least 16
  characters) and given to that peer; it is stored only as an HMAC-SHA256
  under one random key of this install and never shown back. Every link's
  password differs from the others' and from the shared server password (both
  what is saved and what the companion read at startup, remembered by a
  private keyed digest). Editing a label or description does not reset other
  fields. No Settings read shows a password or its digest.

A link's identity is a random id made with it and kept across renames and
password changes; its tasks and its room belong to that identity. A link
removed and made again under the same id is a new agent: it sees none of
the removed one's tasks.

Tools accept a link id or an http(s) address in `url`. Links are read when
used, so a saved, changed or removed link applies to the next call without
a restart, and no setting is forwarded into any process environment.

Migration from 1.x: manual launchers that already set both
`A2A_CLIENT_PEER_URL` and `A2A_CLIENT_PASSWORD` can keep them for `send` and
`status` at that exact HTTP(S) endpoint, including its path. The 1.5.3 client
posted with a trailing slash: if you used a subpath, put the actual endpoint
(for example, `/a2a/`) in both `A2A_CLIENT_PEER_URL` and the call. There is no
origin-wide matching, no discovery with the environment password, and no
import into Settings or fallback to another path. A saved link takes precedence
for new letters. A legacy letter continues through its own pair when available to the tool
or watch process, never through a different link or as an anonymous read.
The standalone 1.5.3 receiver can still return a completed task with a
receipt for that exact letter. It may wait despite `blocking: false`; if
the call ends with neither a reply nor a task id, its outcome is unknown.
There is nothing to poll and the letter is not sent again.

## Letters

`send` saves the letter (its message id, destination, text and who asked)
and records the attempt as uncertain before posting A2A 0.3 `message/send`
with `blocking: false`. A saved letter, an uncertain send, and a confirmed
peer task are different facts. Only an explicit connection failure or a
refusal says the attempt did not send or was refused. `wait: false` makes
one bounded request, returns a matched answer if already present and releases
the watch promptly; otherwise `tasks/get` also stops at the call's budget.
The handle gives the message, task and context ids, the answer if known,
the send and peer states, how the link was secured
(`https`, `loopback`, `plain http` when a password crossed an unencrypted
network link, `http`), and the exact `status(url=..., message_id=...)`
call that reads it later.

An answer slower than the call is kept with its letter. Once the peer has
confirmed a task, the `a2a_letters` companion keeps reading it after the
call returns: `tasks/get` only, with no model and without the receiving
server or its SDK. Each pass (about every 30 seconds, plus time spent
reading) checks due letters; the delay grows up to one hour between checks until the
letter settles or a task read finds no task or a final outcome it cannot tie
to this particular letter, a reason the letter keeps and its
handle shows. No deadline drops a waiting letter, and it reads only
the small index of letters still owed something, never the archive. A
host that brings late answers back (`late_result_version` in `/identity`)
gives each `send` call a reference (`ctx.call_ref`); text the peer tied
to that letter, its answer or once its `input-required` question (never
acted on here), is offered back to the host with the peer's display name.
The host can confirm the original tool result (`tool_result`) or accept
the return into the original room (`handed_off`). Neither proves a model
read the text. A retry repeats only that notice, not the peer request.
If the host declines the notice, including when text exceeds its limit,
the complete answer stays readable by message id; it is not truncated
or automatically delivered. On an older host, or for a letter written
without that reference, nothing is brought back or posted anywhere: the
answer stays on the letter, and the handle says to read it by number. A
read from another work never counts as the asking work's. The watch reads
only as the identity the letter was sent by: a renamed link or its new
password keeps it, a removed or re-made link ends the wait, and a letter
sent to a bare address is read with no password even if a link takes
that address later.

`status` with the message id alone reads the local letter; with its url
as well it asks the peer for news through the same original link or bare
address used by `send` and the watch. `status()` with no arguments lists the
latest 50 letters asked from the current task or chat (a call with neither id
sees none). This explicit list reads the local archive; a read by message id
does not scan it. `status(url, task_id)` returns the peer's current JSON-RPC
task result or error. Read a reused task's reply to a particular letter by its
message id. A status read never sends a message, and a final answer, once
recorded, never changes. A recorded clarification stays on its own letter;
answering it sends a new letter on the same task and context.

Nothing repeats a letter by itself: recovery only reads. Retry a saved or
definitely unaccepted letter with the same `message_id`, `url` and exact
`message`. If any send might have landed, it is not repeated. A direct reply
or a task's separate receipt for this message can supply the answer. With a
plain A2A peer the corresponding latest question must be visible in the
history. Task-wide artifacts are used only on an unreused task begun by that
letter; a continuation needs its own status reply, either attached to this
input in history or linked there to the peer's previous recorded clarification.
If a final snapshot does not establish that link, automatic checks end without
claiming an answer.
The letter and the task snapshot are still separately readable via `status`.

A 1.0-only peer without 0.3 compatibility stays unreachable.

Letters and the receiver's records are small JSON files in the skill's
state folder, and nothing removes them yet (as before 2.0 for `tasks/` and
`presence/`). Measured with equal question and answer lengths, one exchange
between two installs keeps seven files: on the sender the letter and its
empty lock file plus the remote task's order index and its lock (one per
remote task), on the receiver the arrival, the Presence receipt and the
task record; together about 4.7 KB for 20-character messages and about
41 KB for 4 KB messages, before file-system overhead. While a letter is
owed a read or a late answer, one more small mark names it in
`letters/watching/`; the companion removes it when nothing more is owed.

## The A2A place and what a peer is told

Connect provider `a2a`, account `server` in Circles: one place for the
whole account. Direct peer requests carry `direct_message` and `from_bot`.
A named link writes as `link:<its identity>` under its name, marked as a
program (`origin: machine`, which can only lower its authority), and its
contexts are threads of its own conversation, its room in that place. A
link has no place of its own in this release: the host's outbound check
could not attribute a send to a narrower place, so pausing one link would
have held sends to every peer. Each link sees only its own tasks. The
shared password and an anonymous request to a local-only loopback bind
are one unnamed caller, shown as `A2A peer` with `origin: machine`.
The server's shared password is checked first; a wrong password is refused
on every bind. Inbound message text keeps its spaces and newlines.

Requests go through `/presence/turn` using the `presence` permission. Peer
text, including slash prefixes, stays conversation data. Each message's
arrival is stored under the caller and its message id, with the exact content,
conversation and delivery window, before its task binding is published;
a repeat, a concurrent first copy and a copy after a restart join it, and
changed content under the same id is refused, as is a new message naming a
task with a context that is not that task's. Blocking or streaming repeats
of a known letter read that letter's operation, not the task's current stream.
When the host cannot say
which place takes a message, it is asked again and nothing is offered
until it can. Every offer counts as of unknown effect until the host
answers, so a crash never turns it into a refusal. When no host answer
came back, a task status read can offer its current event again under the
same source and place, only within its delivery window and before a final
refusal. A host operation with a known reference can still be read after
the window. With no known work reference after that window, an uncertain
offer is reported as `unknown` once no request is still getting its receipt.
An old event with no work reference or follow-on outcome is readable as
`unknown` and not offered again. Each letter has its own window. The read
returns saved results without waiting on other letters; each late
answer is filed under its own message id. Nothing is sent to the peer by
a read. A message the server had no free slot for
is answered `try_later` (`failed`); repeating it returns that answer under
the same task, so send a new message later.

What the peer learns is one short state, in the task's status message and
in `metadata.ouroboros.delivery`, never an internal reason (capacity, a
place's or allowance's state, a restart, running work), a reset time or an
error text: an answer; `saved` (kept: `submitted` while it may still be
taken up, `rejected` once no response will start); `passed_on` (taken over
by work that answers elsewhere, nothing more here); `no_answer` (taken up,
nothing said); `try_later` (not taken up or not completed: send it again
later); `unknown` (the host does not say whether it was read); `working`;
`canceled`. Only the host's own allowance answer (HTTP 429 with its code
and time) is offered again, until the delivery window
`A2A_STREAM_DEADLINE_SEC` closes. Each message's outcome is also kept
under its message id in `metadata.ouroboros.answers`, in the same words.
`pending_message_ids` distinguishes letters still being observed when the
task already has another letter's outcome.
A host that advertises `presence_attention_version` explains silence
(`heard` is saved, `handed_off` passed on, `completed_silent` no answer;
a focus that only started stays `unknown`); an older one leaves it
`unknown`. Stored tasks are read in the same words:
the status message of an earlier release's record is replaced by its
state's sentence, and the history keeps only the peers' own messages. A
failure inside this server answers one fixed sentence; the detail stays in
the companion's log.

`A2A_RESPONSE_TIMEOUT_SEC` bounds the initial host wait. The Presence
protocol has no cancellation endpoint: `tasks/cancel` returns
`TaskNotCancelableError` and work continues. Task records of earlier
releases stay readable under their saved bindings, and a repeat of such a
message reads its saved turn instead of offering it again.

## HTTPS and the Agent Card

A network bind or a configured `A2A_PUBLIC_URL` needs an inbound credential
(a link that accepts messages, or the shared password), even when a proxy
forwards to loopback. A loopback bind without a public URL still works
without credentials. Without `A2A_TLS_CERT_FILE` and
`A2A_TLS_KEY_FILE` it serves plain HTTP, which sends passwords and
messages unencrypted: Settings says so, the companion logs it at start,
and `send` reports `plain http`. Use it only over a private link (a VPN,
an SSH forward) or behind a TLS proxy. An incomplete or unusable
certificate pair stops the server before it listens. A link's CA file
verifies a self-signed peer.

If the saved server settings are unreadable, back up `settings.json` in
the skill's state folder and repair it or restore a known-good JSON object
before saving or restarting. A failed read is not an empty form. Leave the
separate link settings and key in place.

The card advertises `A2A_PUBLIC_URL` (a proxy or tunnel address, with the path
exactly as configured) or the bind address, which a `0.0.0.0` bind cannot be dialled
at. An invalid nonempty public address is refused on Save and at server
start, also when supplied by the environment. It declares HTTP Basic in
`securitySchemes`, and requires it for a
network bind or public URL. Settings reports the configured address,
not live listeners. The card describes what this Ouroboros instance does,
capability-first: the name is `Ouroboros` unless `A2A_AGENT_NAME` is set;
the description leads with a curated capability summary and appends the
host `/identity` first line (`A2A_AGENT_DESCRIPTION` overrides it); the
skill list starts with five curated categories, followed by live per-tool
entries from `GET /tools/schemas`. A discovery GET answers in seconds from
the last known-good tool list while a background refresher keeps it warm.

The receiving server needs its declared SDK: without it the companion
stops at start and says so; the tools keep working. For a manual launch,
install the manifest's SDK dependencies and `uvicorn` in the launcher's
Python environment. There is no separate receiver implementation without
the SDK.
