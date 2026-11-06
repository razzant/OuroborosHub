---
name: email-presence
description: Multi-account Gmail API, Exchange EWS and IMAP/SMTP mail tools with optional Presence intake and durable delivery.
version: 0.10.9
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, route, tool, widget, companion_process, presence]
env_from_settings: [EMAIL_IMAP_HOST, EMAIL_IMAP_PORT, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD, EMAIL_DEFAULT_FOLDER, EMAIL_AUTH_MODE, EMAIL_OAUTH_ACCESS_TOKEN, EMAIL_OAUTH_REFRESH_TOKEN, EMAIL_OAUTH_CLIENT_ID, EMAIL_OAUTH_CLIENT_SECRET, EMAIL_IMAP_SECURITY, EMAIL_SMTP_SECURITY, EMAIL_TLS_CA, EMAIL_TLS_FINGERPRINT, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET]
dependencies: [httpx, starlette, requests, requests-ntlm, defusedxml]
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
    conversation: conversation_id
    thread: thread_id
    description: Queue a proactive email or threaded reply for durable delivery.
  - name: email_search
    effect: read
    target: []
    description: Search old and new mailbox messages using provider queries or IMAP criteria.
  - name: email_read
    effect: read
    target: []
    description: Read an account-bound message reference or legacy UID and UIDVALIDITY without changing flags.
  - name: email_mark_read
    effect: write
    target: [account_id,remove_labels,folder_names]
    description: Mark one message read by its account-bound reference, or a legacy IMAP UID and UIDVALIDITY.
  - name: email_mailbox
    effect: write
    target: [account_id,destination,add_labels,remove_labels,label_id,label_names,folder_names]
    description: List/create folders, copy/move messages and update flags.
  - name: email_draft
    effect: write
    target: [account_id,to,cc,bcc,folder,folder_names]
    conversation: conversation_id
    thread: thread_id
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
    target: [account_id,forward_to,add_labels,remove_labels,label_names]
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

This transport connects Gmail API, on-premises Exchange EWS and IMAP/SMTP mailboxes. Accounts
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

Supply canonical `account_id` for new sends, drafts, marking read, mailbox and Gmail
settings writes, even with a sole connected mailbox. Reads and folder listing may still
select that sole mailbox implicitly. A write with an implicit account returns
the canonical account and its resolved targets in one `retry_arguments`. Read/search results also include canonical `conversation_id`
and `thread_id` when the RFC source is known; pass both for a threaded reply.
An omitted account or reply identity returns `needs_arguments` with exact
`retry_arguments` before enqueueing or saving anything. Repeat that call so the
existing Host guard sees the same account and conversation the mail will use.
This is argument preparation, not another human approval. `References` and a
Place are not mandatory. A new unthreaded mail with an explicit account still
queues in one call, and a queued request's receipt remains replayable without
another effect. Supplied conversation fields must match the resolved RFC source,
never a Gmail thread id. Draft attribution remains a write, not a speaking turn.

## Setup

1. Open the Email accounts widget. Add an IMAP/SMTP account using its app password
   (Yandex) or local Proton Bridge credentials. Ports and implicit TLS/STARTTLS
   are explicit. Optional CA trust or an exact SHA-256 certificate pin supports
   private certificates; the pin is checked before authentication. For Bridge,
   use the certificate/fingerprint obtained from Bridge, never your Proton login
   password. One mailbox may have many sender addresses; these are not separate
   account identities. IMAP and SMTP usernames/passwords may differ.
   For on-premises Exchange (2013 SP1 or later), choose **Add Exchange**.
   Enter the mailbox email address, its HTTPS EWS service URL (not the OWA webmail
   address), and the NTLM login/password accepted by that server. The login may
   differ from the mailbox address. Obtain the EWS URL from the provider or its
   Autodiscover settings; this form does not discover hosts or import browser
   sessions. Exchange verifies TLS with the CA bundle available to Requests in
   this installation; the form has no per-account CA or certificate-pin override.
   Test connection reads the configured Inbox without sending mail.
   The ordinary mail tools and optional intake work with the browser closed.
   After Exchange rejects a login (HTTP 401), that background connection stops
   presenting the rejected credential. Edit and save the connection to resume
   with fresh credentials. Testing it explicitly makes a fresh connection;
   a refused operation such as sending as a particular address does not disable
   the whole mailbox.
2. For Gmail, configure and grant the installation's GOOGLE_OAUTH_CLIENT_ID and
   GOOGLE_OAUTH_CLIENT_SECRET where required. The existing EMAIL_OAUTH_CLIENT_*
   keys are accepted as fallback. The widget prepares a PKCE sign-in link; Google
   returns through the host's /onboarding/static/provider-return.html page and
   posts the code/state to this skill's oauth/complete route. A remote installation
   needs its reachable public address and a Web client with that exact redirect;
   a local Desktop client uses loopback. An older host without the return page
   needs an update. Calendar has a separate consent and credential owner.
3. Gmail requests openid/email and gmail.modify, plus gmail.settings.basic for filters,
   forwarding, vacation replies and sender addresses, unless the owner clears that choice
   at sign-in. A Workspace administrator may approve only mail access, and Google then
   refuses a larger request as a whole (admin_policy_enforced). Without it, reading,
   drafting, sending and organizing work, and settings writes answer
   `settings_permission_missing`. Full mail scope, including deletion that
   skips Trash, is optional and named at consent.
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
Exchange keeps the latest sync state per intake folder in that same account store.
The native-event index prevents duplicate capture when an unfinished sync is replayed. With
160-byte folder handles and 144-byte event keys its measured SQLite cost is
about 34 KiB per 100 captured messages, in addition to ordinary retained mail.
To give a conversation its arrivals oldest first, a running sync temporarily keeps
eligible native IDs and timestamps until it has seen the last change page (about
29 KiB per 100 pending 150-character IDs). That working set contains no mail
bodies and is released when drained or when the account client stops.
The retained rows, index and cursors follow the existing mailbox lifetime;
disconnect preserves them, removing the skill's local data removes them.

Settings changes reload account workers cooperatively; changing environment-granted
legacy secrets may require restarting the skill. A disconnected account's receipts
remain readable by explicit account_id.

## Mailbox tools and transport behavior

- All twelve tools return JSON text. email_accounts lists canonical account ids
  and sender addresses, existing queue counts and the latest 20 delivery receipts,
  including disconnected accounts. Select account_id explicitly when more than one account
  is connected; unknown ids never fall back to a neighbor. email_receipt returns
  JSON null for a missing request, with request_id scoped to the chosen account.
- email_search uses Gmail or Exchange query/page_token, or IMAP criteria tokens. It retrieves
  headers/metadata, not every matching body. Results carry an account-bound
  message_ref; email_read accepts it, or legacy uid/uidvalidity/folder arguments.
  IMAP Unicode operands use UTF-8 literals and folder names use modified UTF-7.
  An Exchange message_ref contains a case-sensitive native item ID. Use the
  new reference returned by a move or copy when available; otherwise search
  its destination again. A local intake row number is never an EWS item ID.
  Exchange search may expose only a sender name; read the message for SMTP address details.
  For Exchange and Gmail, a Place event carries the same public message_ref as the tools; its
  native source_event_id and local queue anchor remain separate.
  Gmail search is not identical to its website's alias-expanded/thread-wide search.
- email_read only peeks and preserves flags. A local reply-header cache and exact
  raw source artifacts retain explicitly read historical mail without submitting
  it to Presence. email_mark_read is the separate write for Seen/UNREAD.
  Incoming address headers keep a readable `headers` view and retained MIME source.
  If Exchange specifically cannot generate MIME, GetItem may instead supply the
  same item's native body, RFC headers and recipient/attachment metadata. The
  result identifies this fallback, marks missing raw MIME and attachment bytes,
  and reports a missing or server-truncated body. An unrelated provider error is
  not converted into a successful read; if the alternate read fails, intake
  keeps its committed position and reports the failure.
  The view unfolds header continuations and recovers raw UTF-8 names; an undecodable
  encoded word stays visible, and invalid unlabelled bytes display replacement
  characters while the original MIME bytes remain unchanged. These reads stay
  capturable and searchable. Parsed address fields are projections, not permission
  to send to malformed text: outgoing validation and selected-output custody still
  apply. A damaged Message-ID does not stop intake. Mail keeps the original
  MIME when available and uses a usable provider or MIME identity, or a stable
  local fallback rather than claiming that the sender supplied a valid ID.
  An IMAP full read keeps the SHA-256 identity of the complete source;
  Gmail can use its native message ID for a damaged header, and Exchange uses
  its native item ID when neither source supplies a usable InternetMessageId.
  A headers-only view cannot hash the original message. When the ID is absent
  from that view, use its account-bound message_ref to read it; the optional
  message_id_note explains the missing identity. No full body is fetched just
  for search. A synthetic ID does not recreate the sender's original thread.
  After an IMAP UIDVALIDITY change, a previously tolerated damaged ID is only
  recognized as an old capture when the same folder's inbox row records the
  identical full-source digest. Without that evidence the message is captured.
  This recovery does not cover every malformed MIME structure or
  unsupported charset.
- email_folders is a declared read and works in Silent/Advises. email_mailbox
  retains list/create/copy/move/flags, including the old list action. IMAP move
  requires UID MOVE, never a mailbox-wide expunge. Gmail adds label management,
  archive, trash/untrash, delete_draft and explicitly granted permanent deletion.
  For Gmail label creation,
  properties.name keeps its existing precedence; a different or omitted top-level
  destination returns exact retry_arguments naming that actual label without
  dropping any properties. Gmail exposes all added and removed label IDs, including
  those implied by a move, archive or flag. A label name may need a read-only
  lookup before the tool returns canonical arguments. Updating or deleting a label
  exposes its actual ID; a rename exposes the new name as destination as well.
  Gmail checks current custom label names alongside IDs, including rename's old
  and new names. The move source is a removed label, trash/untrash expose TRASH,
  and Gmail mark-read exposes its fixed removal of UNREAD. Follow returned
  retry_arguments; label_names describes checked targets and never changes a
  label. A matching canonical call remains one call.
- For IMAP and Exchange, folder_names identifies the actual folders changed.
  A move changes source and destination; a copy writes only the destination.
  Marking a message read or changing its flags writes its actual source folder.
  Before IMAP CREATE, Mail checks the server's hierarchy delimiter. For a nested
  path it declares any missing intermediate folders and the nearest existing
  parent that would gain a child. Mail checks the spelling you selected and the
  spelling returned by the server when they differ. An existing active parent stays writable when
  a more distant ancestor is paused. A flat namespace treats the name literally.
  Exchange trash moves to Deleted Items. Exchange writes check the affected folder
  identities and require read access to their metadata; ordinary message reads still work
  under the existing account and item permissions.
  Gmail delete_draft checks both DRAFT and Drafts before deleting the selected draft.
  Permanent deletion by message reference checks the message's current labels too,
  including both names for Drafts and the current names of custom labels. Follow
  retry_arguments if the labels change. The usual full-mail grant is still required.
  Gmail and IMAP read the current targets before writing; another client may
  change the mailbox between that last read and the provider command.
- For Reply All, follow retry_arguments: Mail expands the original visible
  recipients before sending or saving. You may edit the explicit To/Cc/Bcc;
  leaving reply_all false keeps that choice. A permitted group thread remains
  permitted if a separate conversation with one recipient is paused. Bcc from
  the original message is never copied into a reply. Keep the opaque reply_all_fingerprint
  returned by a prepared send. An exact repeat works even after the source disappears;
  if you edit the recipient list, replay the explicit accepted arguments. For an older
  send without a fingerprint, use its explicit arguments or inspect email_receipt.
- email_draft saves or updates a Gmail draft by draft_id, or appends an IMAP
  draft. The selected draft folder is an explicit Host target. Omit the folder
  to receive retry_arguments with the provider default (Gmail DRAFT, IMAP Drafts,
  Exchange drafts). Gmail checks both DRAFT and Drafts. Exchange prepares the existing account-bound folder handle
  and its current server names in folder_names; both are checked, and the same
  native folder ID receives the draft. Use the stable handle for durable folder
  rules across renames and localization. The Host checks these independently of any RFC
  Place: a Silent/Paused folder holds the draft, a genuinely unconnected folder
  is still writable, and a failed policy lookup does not save anything. Search Gmail folder DRAFT to obtain stable draft ids. Sending captured
  draft content with email_send does not silently delete the original draft;
  deleting it is a separate explicit mailbox operation.
- email_send accepts sender, To/Cc/Bcc, MIME attachments and reply-all. Read a
  historical source first, or use reply_folder when it is outside the intake
  folder. Reply-all excludes configured own sender addresses from its automatic additions. The same
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
- Exchange saves new drafts (`draft_id` replacement is not implemented), sends with
  an explicit To/Cc/Bcc envelope, and uses its native sync cursor for optional
  inbox intake. The first pass skips historical messages and keeps eligible mail
  arriving during that pass. It discovers eligible IDs through the final change
  page, captures them oldest first and commits the new sync state only after the
  captures are durable. A restart replays the unfinished range; already captured
  native events are not downloaded again. If Exchange invalidates the cursor,
  recovery starts at the original activation boundary;
  items already removed from the server may no longer be available. Automatic
  capture retains Mail's per-folder RFC Message-ID deduplication; ordinary tools
  can read physical copies separately by their native refs. Exchange acceptance
  is a successful response to SendAndSaveCopy, which need not return an item ID.
  The receipt's submitted_message_id is the locally authored RFC identifier;
  it is not a provider-assigned identifier or proof of recipient delivery.
  A lost or ambiguous response is recorded as uncertain, not retried automatically.
- Gmail intake stores native message identity once across labels, follows history
  pagination, and explicitly reports history_reset when an expired history cursor
  needs an activation-range rescan. IMAP UIDVALIDITY changes rescan from the saved
  activation date and deduplicate Message-ID. Captured data is stored before cursor
  advancement, while automatic intake, output observation and delivery remain
  independent workers of this one companion.
- email_settings_read inspects supported Gmail settings; email_settings changes
  supported filters, vacation, language, IMAP/POP and existing send-as properties.
  A settings write exposes its selected account, even when it is the only mailbox.
  Before creating a filter, Mail compares the effective values.action.forward,
  addLabelIds and removeLabelIds and current custom label names with the declared
  forward_to, add_labels, remove_labels and label_names. Missing or stale targets
  return exact retry_arguments without writing. A canonical call writes once.
  The label fields describe the original Gmail action; they never add an effect.
  forward_to alone fills the nested forwarding field while preserving the criteria
  and other actions. Gmail runs an installed filter until it is deleted; a later
  local Place change does not suspend an existing provider rule. Deleting a filter
  acts on the selected account and filter identifier, without executing the rule.
  Administrative forwarding or additional send-as management requiring domain
  delegation is reported as website_required for personal accounts. Other
  providers' account settings are not exposed by this adapter; use the provider website.

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
Retained work carries `reply_anchor` on both released and terminal selected
outputs, just as a continuing author does. The existing captured-message and
same-thread checks select the recipient; absent or invalid anchors keep the
legacy fallback. Recovering an anchor never changes an already queued receipt's
recipient or repeats its send.
An invalid mailbox destination cannot turn a selected answer into a delivery.
The transport records `invalid_recipient`, keeps observing an active author and
still queues later outputs with valid reply anchors. Once the Host work is
terminal, the inbox reports failure if a selected reply could not be queued;
the original Host reference and its canonical selected text remain recoverable.
This does not regenerate the answer or treat other Host/transport errors as a
permanent address failure.
`rejected` ends an event-specific refusal. A Host advertising Place binding may
return `blocked` with `code: place_not_connected` before admission: retain that
receipt and settle the inbox item without resubmitting it. Connecting a Place
later does not replay those settled inbox items; ordinary account tools remain usable.
Provider cursor catch-up is separate: if this account already has a cursor,
polling without a Place leaves it unchanged, so mail arriving after that cursor
can enter intake when a Place is connected again. A fresh account establishes
its first baseline without backfilling earlier mail. No cursor reset or new
automatic replay of settled inbox rows is performed.
Other before-admission `retry` and `blocked` results retain retry custody, including
after account or binding repair. Missing or unknown typed dispositions stay
retryable, even on HTTP 403/404. Full queue recovery requires
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
The same rule applies to an admitted deferred work reference: a blocked read
does not fail the inbox or resubmit the original turn. Canonical terminal work
status still settles the reference and retains any selected speech.
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
addresses, Date, and decoded named From/To/Cc/Reply-To headers. A Date with a time zone
is also `message.sent_at`, an ISO time in the sender's offset (`-0000`, UTC from an
unnamed zone, as `+00:00`); a Date without a zone or an unreadable Date leaves the send
time unknown. Reply-To stays
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
original To/Cc recipients, excludes configured sender identities from automatic additions and de-duplicates
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
or an unavailable lookup holds the same queue item; Active resumes it without
spending the retry budget on policy holds, and an unavailable lookup clears once
Ouroboros answers. When Ouroboros refuses the skill itself (its grant,
enablement, review or token), the hold lasts until its access is fixed in Skills.

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
