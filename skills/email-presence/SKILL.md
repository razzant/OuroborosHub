---
name: email-presence
description: Multi-account Gmail API and IMAP/SMTP mail tools with optional Presence intake and durable delivery.
version: 0.9.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, route, tool, widget, companion_process, presence]
env_from_settings: [EMAIL_IMAP_HOST, EMAIL_IMAP_PORT, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD, EMAIL_DEFAULT_FOLDER, EMAIL_AUTH_MODE, EMAIL_OAUTH_ACCESS_TOKEN, EMAIL_OAUTH_REFRESH_TOKEN, EMAIL_OAUTH_CLIENT_ID, EMAIL_OAUTH_CLIENT_SECRET, EMAIL_IMAP_SECURITY, EMAIL_SMTP_SECURITY, EMAIL_TLS_CA, EMAIL_TLS_FINGERPRINT, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET]
dependencies: [httpx, starlette]
when_to_use: User wants to read, compose, organize or configure mailboxes, or receive and reply to incoming mail through a Place.
timeout_sec: 120
companion_processes:
  - name: email_poller
    command: [python3, scripts/email_daemon.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 10
tools:
  - name: email_send
    effect: message
    target: [account_id,to,cc,bcc]
    conversation: reply_to_message_id
    description: Queue a proactive email or threaded reply for durable delivery.
  - name: email_search
    effect: read
    target: []
    description: Search old and new mailbox messages using Gmail queries or IMAP criteria.
  - name: email_read
    effect: read
    target: []
    description: Read an account-bound message reference or legacy UID and UIDVALIDITY without changing flags.
  - name: email_mark_read
    effect: write
    target: [account_id]
    description: Mark one message read by stable UID and UIDVALIDITY.
  - name: email_mailbox
    effect: write
    target: [account_id]
    description: List/create folders, copy/move messages and update flags.
  - name: email_draft
    effect: write
    target: [account_id,to,cc,bcc]
    conversation: reply_to_message_id
    description: Save a text draft without sending.
  - name: email_receipt
    effect: read
    target: []
    description: Read durable delivery state and Message-ID.
  - name: email_test_connection
    effect: read
    target: []
    description: Check TLS and authentication without sending mail.
  - name: email_accounts
    effect: read
    target: []
    description: List configured accounts, actual sender addresses and granted scopes without credentials.
  - name: email_folders
    effect: read
    target: [account_id]
    description: Read folders or labels without changing mailbox state.
  - name: email_settings_read
    effect: read
    target: [account_id]
    description: Read supported provider settings, filters and sender configuration.
  - name: email_settings
    effect: write
    target: [account_id]
    description: Change supported provider settings; website-only operations are reported explicitly.
ui_tab:
  tab_id: email_presence
  title: Email accounts
  render:
    kind: module
    entry: ui/accounts.js
    entry_type: module
    assets: [ui/accounts.css]
    appearance: host
    start: auto
    height: 620
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: Email
    destination: vendor:Email
    scope: account
    source: {route: status, refresh_s: 15}
    icon: ui/icon.svg
    settings: email_presence
    facts:
      - any_message
      - direct_message
      - from_bot
      - {id: signal.words, label: "Words you chose appear (connector setting)"}
    wakes: []
---

# Email Presence

This transport connects multiple Gmail API or IMAP/SMTP mailboxes. Accounts
are ordinary tools; a Place is optional for automatic intake and presence.
A configured account without a Place remains usable by tasks and consciousness
when the current mode and instructions permit it. This is not an owner-direct-only
mode. Silent and Paused still constrain their applicable account/place scope,
including every configured sender address of that mailbox.

One supervised companion manages independent account workers, with separate
inboxes, outboxes and cursors. Account identity is distinct from the chosen From
address. Host conversation identity remains RFC Message-ID/References; native
Gmail message/thread ids remain explicit provider facts, and replies pass the
native threadId to Gmail. No signature, footer or Auto-Submitted marker is added.

## Setup

1. Open the Email accounts widget. Add an IMAP/SMTP account using its app password
   (Yandex) or local Proton Bridge credentials. Ports and implicit TLS/STARTTLS
   are explicit. Optional CA trust or an exact SHA-256 certificate pin supports
   private certificates; the pin is checked before authentication. For Bridge,
   use the certificate/fingerprint obtained from Bridge, never your Proton login
   password. One mailbox may have many sender addresses; these are not separate
   account identities. IMAP and SMTP usernames/passwords may differ.
2. For Gmail, configure and grant the installation's GOOGLE_OAUTH_CLIENT_ID and
   GOOGLE_OAUTH_CLIENT_SECRET where required. The existing EMAIL_OAUTH_CLIENT_*
   keys are accepted as fallback. The widget prepares a PKCE sign-in link; Google
   returns through the host's /onboarding/static/provider-return.html page and
   posts the code/state to this skill's oauth/complete route. A remote installation
   needs its reachable public address and a Web client with that exact redirect;
   a local Desktop client uses loopback. An older host without the return page
   needs an update. Calendar has a separate consent and credential owner.
3. Gmail requests openid/email, gmail.modify and gmail.settings.basic. Full mail
   scope, including deletion that skips Trash, is optional and named at consent.
   Granted scopes are shown as known or unknown; provider refusals are not
   reported as successes. Local disconnect removes this skill's credentials and
   preserves history. Google project-wide revocation is different and may affect
   Calendar or other clients in the same project.
4. Test the configured connection. Then optionally connect its canonical account
   id as an Email Place. New Places start Silent and activation remains the
   owner's existing choice. Sending off holds every send, including direct
   commands, while reading, drafts and organization remain available.
5. Automatic intake starts only for applicable Places. A known account without
   a Place does not create an endless Presence retry queue. Silent/Paused Places
   still reach the Host's intake decision. The first successful intake records
   its baseline; older mail remains available through the ordinary tools.

Existing EMAIL_* configurations keep their original account id and state directory;
no mail or receipt is moved. New account stores live under hashed account directories.
The private accounts.sqlite3 registry (mode0600) contains configuration and tokens;
it is not an encrypted backup. The skill's normal source package contains no credentials.
Settings changes reload account workers cooperatively; changing environment-granted
legacy secrets may require restarting the skill. A disconnected account's receipts
remain readable by explicit account_id.

## Mailbox tools and transport behavior

- All twelve tools return JSON text. email_accounts lists canonical account ids
  and sender addresses. Select account_id explicitly when more than one account
  is connected; unknown ids never fall back to a neighbor. email_receipt returns
  JSON null for a missing request, with request_id scoped to the chosen account.
- email_search uses Gmail query/page_token or IMAP criteria tokens. It retrieves
  headers/metadata, not every matching body. Results carry an account-bound
  message_ref; email_read accepts it, or legacy uid/uidvalidity/folder arguments.
  IMAP Unicode operands use UTF-8 literals and folder names use modified UTF-7.
  Gmail search is not identical to its website's alias-expanded/thread-wide search.
- email_read only peeks and preserves flags. A local reply-header cache and exact
  raw source artifacts retain explicitly read historical mail without submitting
  it to Presence. email_mark_read is the separate write for Seen/UNREAD.
- email_folders is a declared read and works in Silent/Advises. email_mailbox
  retains list/create/copy/move/flags, including the old list action. IMAP move
  requires UID MOVE, never a mailbox-wide expunge. Gmail adds label management,
  archive, trash/untrash, delete_draft and explicitly granted permanent deletion.
- email_draft saves or updates a Gmail draft by draft_id, or appends an IMAP
  draft. Search Gmail folder DRAFT to obtain stable draft ids. Sending captured
  draft content with email_send does not silently delete the original draft;
  deleting it is a separate explicit mailbox operation.
- email_send accepts sender, To/Cc/Bcc, MIME attachments and reply-all. Read a
  historical source first, or use reply_folder when it is outside the intake
  folder. Reply-all removes all configured own sender addresses. The same
  account_id/request_id returns its original receipt; changed sender/content
  conflicts. The same request_id in another explicitly selected account is a
  different operation.
- Queued is not sent. Results/receipts expose pending, held, completed, failed
  or uncertain state, actual account/sender and native provider receipt when
  available. An ambiguous SMTP/API acceptance is never blindly replayed.
- New Hosts advertise presence_place_binding_version=1. The tool captures the
  applicable target binding before enqueue; delivery checks that retained binding
  against current scope. A newly unbound send is distinct from a disconnected
  bound/automatic send. A404, a missing legacy field, or origin.kind=tool alone
  never grants unbound delivery. Older Hosts keep their legacy check; receipts
  disclose unsupported binding capture, and a no-Place legacy send remains held.
- Gmail intake stores native message identity once across labels, follows history
  pagination, and explicitly reports history_reset when an expired history cursor
  needs an activation-range rescan. IMAP UIDVALIDITY changes rescan from the saved
  activation date and deduplicate Message-ID. Captured data is stored before cursor
  advancement, while automatic intake, output observation and delivery remain
  independent workers of this one companion.
- email_settings_read inspects supported Gmail settings; email_settings changes
  supported filters, vacation, language, IMAP/POP and existing send-as properties.
  Administrative forwarding or additional send-as management requiring domain
  delegation is reported as website_required for personal accounts. Other
  providers' account settings use their own websites, not invented IMAP features.

## Retained input and late results

A typed Host `disposition: retained` means the original event is saved but has
no canonical final result to deliver. The transport stores the complete refusal receipt
with its source event, releases that conversation's input queue, and observes
the original `turn_ref` and any real child `work_ref` independently. It never
resubmits that retained event. A child finishing does not erase the original
uncertainty or stop observation of its late selected output.

A work view answers `pending` while the turn's executor can still change it and
is terminal once nothing can. A failed task may still carry a selected answer;
lifecycle status alone does not suppress speech. A turn that ended without a
canonical answer reads `failed` (`cancelled` when recorded so) with
`outcome: "silent"`: nothing further to deliver from that reference. Its reason
facts (`code`, `recorded_status`, `uncertainty`) prove neither delivery nor the
absence of external effects, and observation starts no new generation. A real
child keeps its own reference in `child_work_ref`, which any view may name, and
is observed on its own. Older Hosts may still answer `unresolved`, which
stays observable. Canonical selected outputs keep their source and delivery
identity across repeated polls and restarts; a retained receipt itself is never
sent as speech. This does not claim exactly-once provider delivery after
ambiguous network acceptance.
`rejected` ends an event-specific refusal; before-admission `retry` and `blocked`
retain retry custody, including after account or binding repair. Missing or unknown typed
dispositions stay retryable, even on HTTP 403/404. Full queue recovery requires
both an updated Host and this transport; older Hosts may still return a
retry-only refusal. Existing completed delivery receipts remain unchanged.

The existing inbox Host-reference envelope retains the original receipt and
observed references; late mail keeps the original RFC 5322 reply threading.
Upgrade through the normal payload review/enable lifecycle.

## Timely intake (Presence continuation version 2)

When `/identity` lists 2 in `presence_continuation_versions`, each new turn
request adds `continuation_version: 2`. The request is otherwise unchanged. A
Host that does not list it keeps the plain turn: the request waits for the
turn's end, and that blocks the companion's one inbox worker meanwhile. This
transport never used version 1. The status route shows the negotiated version
as `delivery_support.presence_continuation_version`.

- `status: "continuing"` (with `outcome: "deferred"`, empty text and a
  `continuation_ref`) is custody, not a reply. It arrives once the author's
  intake is open. The reference is stored, which releases the thread, and the
  claim ends without polling.
- The companion's existing inbox lifecycle runs two loops in the same process.
  The input loop only claims mail without a Host reference; once a reference is
  durable it queues any speech the envelope already carries and releases the
  mail. The observation loop only claims mail with a stored reference and polls
  it once per claim, about every 5 seconds while pending. A slow or blocked
  poll therefore never holds new mail, of its thread or any other, and a
  continuous inflow of new mail never holds observation. No daemon, scheduler
  or queue is added.
- `status: "completed"` with `outcome: "silent"` means the mail was heard or
  delivered as context to the thread's live author. It is finished, and any
  reply arrives through that author's poll.
- HTTP 429 `presence_capacity_full` (`disposition: "retry"`, field
  `conversation` or `capacity`) means nothing ran. The same mail stays first in
  its thread and is resubmitted unchanged after max(hint, backoff). The hint is
  the Host's numeric `retry_after` and is never shortened. The backoff is this
  transport's own: 1 second, doubling per refusal, capped at 30 seconds. It is
  used alone when the hint is missing or malformed. There is no attempt cap.
  Mail in other threads continues meanwhile. Nothing is dropped, merged or
  debounced.
- An author's initial answer, released output or terminal may carry
  `reply_anchor: {source_event_id, message}`, which names the last mail the
  author covered. The reply then
  answers that stored message in the same thread: its Reply-To (or From) is the
  recipient, and its Message-ID and References thread the reply. Without an
  anchor (promoted children, retained views, older Hosts), or when the anchor
  is not bound, the reply answers the polled mail as before. Bound means the
  message's folder, UIDVALIDITY and IMAP UID select a captured mail whose
  `source_event_id` and Message-ID are the anchor's, in the polled mail's
  thread.
- Each `output_ref` is queued once under the polled mail's request id, across
  polls, the terminal, children and restarts. If a repeated output would change
  its headers, the first queued copy stays authoritative.
- A stopped author's terminal is `cancelled` with `outcome: "silent"`. Nothing
  new is sent, the mail ends `failed`, and it is never resubmitted.

A promoted child named by a later poll is stored with the receipt before it is
polled. An unreadable poll, including HTTP 403/404, keeps custody and backs off.
References stored as version 2 cannot be read by an older transport that is
rolled back. They keep their custody until this version runs again.

## Delivery history

Hosts advertising `presence_delivery_version=1` receive `/presence/delivery`
observations from the existing outbox. SMTP success is recorded as `accepted`,
meaning server acceptance, never recipient inbox delivery or reading. A partial
recipient refusal retains the accepted subset and the actual refusal evidence;
an ambiguous SMTP disconnect or a process lost while sending remains uncertain
and is never automatically resent. Reports retain the transmitted text, subject,
recipients, Message-ID, reply headers and producer origin, including sends made
after the model turn ends.

Provider state and immutable report payload commit together before the history
callback. ACK/backoff stays in the same outbox, and its worker owns a concurrent
report attempt so unavailable history does not delay later mail. Restart and lost
ACK reuse the original report without another SMTP send. Automatic immediate and
deferred replies preserve the Host's echoed reporting mode. Old Hosts and migrated
rows continue normal sending with mode 0; terminal legacy rows are not imported.
`email_receipt` and `status` expose pending reports and reporting availability;
queued mail is never logged as accepted speech. Existing delivery reporting grants remain unchanged; account setup is described above.

## Incoming context and replies

Incoming Presence events retain the From address and display name, all To and Cc
addresses, Date, and decoded named From/To/Cc/Reply-To headers. Reply-To stays
separate from the From actor. Automatic responses go to Reply-To when supplied,
or From otherwise; To and Cc are context, not an automatic reply-all list. The
configured mailbox account identifies the receiving transport, even when that
mailbox is absent from To because delivery used Bcc or a mailing-list alias.

Attachment descriptors expose filenames and MIME types. Successfully staged
parts include immutable state paths and `content_available: true`; parts that
exceed an inbound limit or use an unsupported MIME shape retain
`content_available: false` plus an error descriptor. The full raw RFC822 source
is staged as an artifact so a clipped part remains recoverable. These message
facts are retained with the inbox across restarts. Existing inbox rows keep
their original content and receipts, with new context unavailable.

Text, reply threading and ordinary MIME attachments are supported. Incoming
attachments are copied into immutable, skill-owned staged files before a
Presence turn; the Host receives their metadata and staged paths separately.
Outbound sends and drafts accept staged file attachments, To/Cc/Bcc envelopes,
and optional multipart/alternative HTML. `reply_all` derives Reply-To/From plus
original To/Cc recipients, removes the sending mailbox and de-duplicates
addresses. SMTP sends keep Bcc envelope-only; drafts retain a Bcc header for
later manual sending. Partial SMTP
recipient acceptance is retained in the receipt alongside accepted/refused
recipients, while ambiguous acceptance remains `uncertain` and is never
automatically resent. HTML-only inbound mail remains available as plain text
with explicit body truncation disclosure.
The separate public `email` utility remains available; this package neither
replaces nor removes its tools.

References: [Gmail XOAUTH2](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol),
[Google refresh tokens](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

## Place protocol

Hosts advertising `places_version >= 1` receive every captured email with
observed `facts`, without a binding ID. Older hosts receive the saved
legacy binding and event shape. Existing Message-IDs, UIDVALIDITY epochs,
staged attachments, work references and delivery reports are preserved.
The SMTP worker re-reads the place immediately before sending. Silent/Paused
or an unavailable lookup holds the same queue item with a visible reason;
Active resumes it without spending the retry budget on policy holds.

`email_read` and `email_folders` are declared read, so a Silent or Advises place can read mail;
`email_mark_read` and the mixed `email_mailbox` (it can mutate) are declared
write. Sending is message; search, receipts and connection checks are declared
read. To/Cc/Bcc are all destination arguments.

## Mailbox identity, settings and facts

On hosts advertising `presence_identity: 1`, the status route and every event
declare the selected default sender under `identity.sends_as` and the mailbox
addresses under `identity.reads_as`, as `{id: address, label: address, kind: user}`.
The Place account id remains the canonical account record id, never a From alias. The host's owner-account
confirmation determines whether that mailbox is the human's own account.
Every such event declares `actor.authenticated: false`: a sender-controlled
`From:` header is not proof. A follow-up will verify the sender using the
receiving server's trusted `Authentication-Results` (DKIM/SPF/DMARC) before
reporting `true`. Older hosts receive neither new key.

In the connector settings, `sending` is `on` by default. `off` makes
`email_send` refuse with `sending_off`: "Sending is off for this mailbox in its
connector settings; reading, drafts and tidying keep working". Reads,
`email_draft`, `email_mark_read` and `email_mailbox` still work. The SMTP worker
also holds queued and automatic replies until sending is on; the status route
shows this switch under `readiness`. This setting applies immediately.

How I decide each event fact:

- `any_message`: every captured arrival, regardless of sender, text or place state.
- `direct_message` (Mail addressed to you): the mailbox address is in To or Cc,
  case-insensitively, and there is no `List-Id`, `List-Unsubscribe` or
  `Precedence: bulk|list|junk`. Bcc-only delivery does not imply this fact.
- `from_bot`: `Auto-Submitted` is present and is not `no`, `Precedence` is
  bulk/list/junk, or the sender local part is `noreply` / `donotreply`, optionally
  separated with hyphens, underscores or dots and optionally followed by a `+` tag.
- `signal.words`: at least one configured plain string in `words` matches a whole
  word or phrase in subject or decoded body, case-insensitively, with non-word
  boundaries on each side. Punctuation is literal, never a pattern. The full
  retained body is used when the displayed excerpt is clipped. The settings form
  takes one entry per line and stores a list; an empty list reports no word signal.
  Account preference changes reload the relevant worker cooperatively. This signal is sent only when the
  host advertises `presence_wake_signals: 1`.

The manifest declares the plain core kind `direct_message`: older hosts reject
a label on a core kind in the manifest and drop the whole place declaration.
The status route the owner's browser fetches for readiness always includes
`labels: {"direct_message": "Mail addressed to you"}`. This needs no capability
gate; older browsers ignore unknown status keys.
No signature, footer, delegate line or `Auto-Submitted` header is appended: each
reply is a judged, paid turn under the allowance, with no machine marker to break loops.
