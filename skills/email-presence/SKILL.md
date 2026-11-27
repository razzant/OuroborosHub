---
name: email-presence
description: Multi-account Gmail API, Exchange EWS and IMAP/SMTP mail tools with optional Presence intake and durable delivery.
version: 0.11.0
type: extension
entry: plugin.py
plugin_api: "2.5"
runtime: python3
permissions: [net, read_settings, route, tool, widget, companion_process, presence, notify_owner]
env_from_settings: [EMAIL_IMAP_HOST, EMAIL_IMAP_PORT, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD, EMAIL_DEFAULT_FOLDER, EMAIL_AUTH_MODE, EMAIL_OAUTH_ACCESS_TOKEN, EMAIL_OAUTH_REFRESH_TOKEN, EMAIL_OAUTH_CLIENT_ID, EMAIL_OAUTH_CLIENT_SECRET, EMAIL_IMAP_SECURITY, EMAIL_SMTP_SECURITY, EMAIL_TLS_CA, EMAIL_TLS_FINGERPRINT, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET]
dependencies: [httpx, starlette, requests, requests-ntlm, defusedxml]
sign_in:
  - id: google
    label: Google
    issuer: https://accounts.google.com
    scopes: [openid, email, "https://www.googleapis.com/auth/gmail.modify"]
    scope_labels:
      openid: Confirm the Google account
      email: Identify its email address
      "https://www.googleapis.com/auth/gmail.modify": Read, send, draft and organize Gmail
    scope_spellings: {email: ["https://www.googleapis.com/auth/userinfo.email"]}
    optional_scopes:
      - {scope: "https://www.googleapis.com/auth/gmail.settings.basic", label: "Manage Gmail settings: filters, forwarding, vacation replies and senders", default: true}
      - {scope: "https://mail.google.com/", label: "Full Gmail access, including permanent deletion that skips Trash and Gmail settings", default: false}
    authorize_params: {access_type: offline, prompt: consent}
    accounts: many
    optional: true
    notes:
      client: >-
        Choose a client for this mailbox before opening Google. Use your own Desktop client for a local return, or an approved Web client with its exact registered return address. A Google Cloud project in Testing can end offline access after seven days.
      refused: >-
        If your organization approved only mail access, clear Manage Gmail settings and leave Full Gmail access off. For admin_policy_enforced or org_internal, check which client and permissions your Workspace administrator approved.
      refresh_rejected: >-
        Sign in again on this card with the client for this mailbox. If that Google Cloud project is in Testing, provider access can expire after seven days.
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
    touches:
      - {conversation: emitted_conversation_id, thread: emitted_conversation_id, effect: message}
      - {conversation: to, effect: write, match: exact_casefold}
      - {conversation: cc, effect: write, match: exact_casefold}
      - {conversation: bcc, effect: write, match: exact_casefold}
    description: Queue a proactive email, a threaded reply or an existing Gmail draft for durable delivery.
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
    target: [account_id,remove_labels,folder_names,label_names]
    touches:
      - {conversation: message_conversation_id, thread: message_thread_id, effect: write}
    description: Mark one message read by its account-bound reference, or a legacy IMAP UID and UIDVALIDITY.
  - name: email_mailbox
    effect: write
    target: [account_id,destination,add_labels,remove_labels,label_id,label_names,folder_names]
    touches:
      - {conversation: message_conversation_id, thread: message_thread_id, effect: write}
      - {conversation: draft_conversation_id, thread: message_thread_id, effect: write}
    description: List/create folders, copy/move messages, update flags, and send or drop a queued mail that has not gone out.
  - name: email_draft
    effect: write
    target: [account_id,to,cc,bcc]
    conversation: conversation_id
    thread: thread_id
    touches:
      - {conversation: folder, effect: write, match: exact}
      - {conversation: folder_names, effect: write, match: exact}
      - {conversation: draft_conversation_id, thread: draft_conversation_id, effect: write, match: exact}
      - {conversation: emitted_conversation_id, thread: emitted_conversation_id, effect: write, match: exact}
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
    target: [account_id,forward_to,add_labels,remove_labels,label_names,values.replyToAddress]
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
never a Gmail thread id. Draft attribution remains a write, not a speaking turn. A reply lookup compares
Message-ID tokens, retaining comments in existing source identities; the written
In-Reply-To is the canonical token. Conflicting matches, including two read messages
sharing one Message-ID, are refused until reply_folder names the folder of the one to
answer (refused again if it holds both). When the emitted reply has a different RFC root (for example,
a legacy multi-ID In-Reply-To), retry_arguments also exposes emitted_conversation_id.
The historical source and emitted root are checked independently with their threads;
new queued sends retain both bindings and delivery rechecks both. Old queue rows
remain unchanged and delivery additionally checks their emitted scope.

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
   Test connection reads the configured Inbox without sending mail. A failed IMAP
   or SMTP test names its stage (connect, TLS or authentication) and a remedy,
   without the server's reply text or any credential.
   The ordinary mail tools and optional intake work with the browser closed.
   After Exchange rejects a login (HTTP 401), that background connection stops
   presenting the rejected credential. Edit and save the connection to resume
   with fresh credentials. Testing it explicitly makes a fresh connection;
   a refused operation such as sending as a particular address does not disable
   the whole mailbox.
2. For Gmail, use **Extensions › Skills › email-presence › Sign-ins** on a host
   that manages sign-in. Choose the issuing client for this mailbox before opening
   Google; Mail's consent is separate from Calendar's. An existing Gmail account
   moves with the client it used, not today's default client, and with the return
   address its saved sign-in recorded. One saved without a return address moves only
   to a host announcing `sign_in_handover_version` 2 (as `return_hint: "unknown"`);
   until then it keeps serving, unreserved. Refresh Email accounts after saving a
   sign-in on the card to see the mailbox and its granted permissions. On older hosts
   the widget still starts the original PKCE flow with granted `GOOGLE_OAUTH_CLIENT_*`
   (or `EMAIL_OAUTH_CLIENT_*`) settings; an older host must still serve the provider
   return page. There a new sign-in also serves a mailbox that moved to a newer host,
   under its account ID. A return already opened by the widget can finish after an
   update. Mail saves that consent and reports when the host has not yet confirmed
   it. A temporarily unavailable configured host is not an older host and does not
   reopen the widget sign-in.
3. Mail requests openid, email and gmail.modify. Gmail settings are optional and
   selected by default. Full Gmail access (`https://mail.google.com/`) is optional
   and off by default; Mail uses it for permanent deletion that skips Trash and
   accepts it for Gmail settings writes in place of the settings permission, as
   the choice says before sign-in. Without either, mail operations still work and
   Gmail settings writes answer `settings_permission_missing`. The displayed
   scopes describe the actual grant, not the requested choices: a renewal Google
   answers without an optional permission keeps ordinary mail, and only what needs
   that permission stops. A bearer Google named no permissions for shows them as
   unknown and gets no optional operation until a renewal, asking for the saved
   limit, records them beside that very token. A transfer carries a bearer with exactly the permissions
   Google named for it, so a narrower one stays narrower; without that record it
   offers the saved limit and the host renews the grant once within it. A host announcing `sign_in_handover_version` 2 files it
   only under the identity Google confirms (`require_verified`). Disconnect a Gmail
   account the host serves on the card, and one its own saved sign-in still serves
   in Email accounts; both keep its history and receipts. A change (send, label,
   draft, settings) is made under the access named when it began: a sign-in of the
   mailbox that changes in between stops it before anything is sent or changed. A
   send whose access cannot be named at that moment, for example while the host is
   unreachable, queues nothing and returns an error.
   Queued Gmail mail that has not gone out goes, as before 0.11, under whichever
   confirmed sign-in of its mailbox's own Google account serves it when it leaves:
   the host's current connection and consent or the mailbox's saved sign-in, whatever
   it was queued with, mail queued by an earlier version included. The same mailbox
   is its verified Google subject, never a matching address. While no confirmed
   sign-in of that account serves it (signed out, or its sign-in to be renewed on the
   card), it waits unsent, and the owner is told once which mail waits and why; it
   goes by itself once the mailbox is signed in again, unless Sending is off or a
   Place held it, which the line then says instead. A saved sign-in that Mail is
   moving to the host is not a sign-out and posts no line. A sign-in host that cannot
   be reached for a queued mail's bearer leaves that mail waiting too, spending no
   attempt, until it answers. A mail whose sending began and whose outcome is unknown
   is never sent a second time: its own mailbox is searched for it by Message-ID;
   found in that mailbox's Sent, it is recorded sent (a copy that came in, in Inbox
   or Spam, proves nothing; the search asks for sent copies, Trash included, and
   reads every page of its answer); otherwise one draft of it (its envelope with Bcc, body,
   attachments and thread; a mail sent from a draft is put back as that draft was
   queued) is kept in that mailbox's Drafts, once the Places of its Drafts allow
   writing there as they do for email_draft, and the owner is told once to check it
   and send it. While a Place or this skill's access keeps Mail from writing those
   Drafts, the mail waits and the owner is told so once. Nothing is drafted in
   another mailbox. These notices go through the host's `notify_owner` (one System row
   in Main, mirrored to the owner's Telegram) and name the mail by its subject and its
   `request_id` (left out only when it cannot be shown whole on one line); until the
   host confirms one (no grant yet, an older host, the host away) it is shown in the
   mailbox's status and the mail's receipt and posted later, only while it is still
   true: a waiting line ends unposted once its mail went, was deleted or settled, or
   its mailbox is signed in again, and each line is read again, with its mailbox's
   sign-in, just before it is posted. The owner's "send" or "delete" in chat is for the
   mind to apply: `email_send` with `draft_id` sends such a draft as it is then,
   `email_mailbox` delete_draft removes it, and `email_mailbox` send_queued or
   discard_queued sends now or drops a mail that has not gone out; `email_accounts`
   lists every mail that waits (`waiting`) by request_id and subject.
   Provider revocation is a separate action and can affect
   other uses of that Google client. An unconfirmed transfer does not fall back to
   its old token: refresh the account view or sign in again on the card.
4. Test the configured connection. Then optionally connect its canonical account
   id as an Email Place. New Places start Silent and activation remains the
   owner's existing choice. Sending off holds every send, including direct
   commands and one whose provider connection was opening when it changed; a
   disconnect does the same. Reading, drafts and organization remain available.
5. Automatic intake starts only for applicable Places. A known account without
   a Place does not create an endless Presence retry queue. Silent/Paused Places
   still reach the Host's intake decision. The first successful intake records
   its baseline; older mail remains available through the ordinary tools.

Existing EMAIL_* configurations keep their original account id and state directory;
no mail or receipt is moved. New account stores live under hashed account directories.
The private accounts.sqlite3 registry (mode 0600) contains mailbox configuration
and legacy credentials; it is not an encrypted backup. Until a transfer is reserved,
the mailbox keeps its saved sign-in within its recorded permissions, including when
it cannot move automatically. The complete host list must name no sign-in for its
subject, and no recorded withdrawal may apply. Each change reads that list again.
A grant that never recorded its permissions, nor those Google named for its current
bearer, keeps reading only; Mail cannot use that fact to change or queue mail, or to
offer a transfer. Recorded permissions without
Gmail access are refused. For transferred Gmail
accounts, the host alone refreshes the grant. The transition keeps the original
credentials dormant in their private fields until a confirmed withdrawal. They
are used again in two cases only: the mailbox's own first offer demonstrably
exchanged nothing (its connection was never made, the host answered per item
`exchange: none`, it filed that word in the receipt for that generation, or it
returned `429` with `handover_started: false` before reading the request), or
a host shown to be older (it answers /identity without
sign_in_version) is asked while they were not withdrawn, still work and keep their
permissions. That receipt closes only the attempt: it confirms no earlier mail, new
owner, break or permission. Such a transfer is offered again after pauses that grow from 30
seconds to an hour; a recover answer never makes them usable. A lost or unclear
answer, including an outage after the request left, activates nothing. A transfer whose answer is lost is not
submitted again unless the host's receipt says so: on a host announcing `sign_in_handover_version` 2 Mail first
reads the host's own receipt of it, without credentials; otherwise it asks with
`recover`, which exchanges nothing, for the outcome it kept, at once and then after pauses that grow from 30 seconds to
15 minutes (pauses are kept in memory, so a restart asks about an unresolved offer again at once).
A recorded `refresh_rejected` ends checks of that offer; the old grant does not resume.
When the receipt says `resubmit` (only an outage stopped it before any exchange, while an earlier
Disconnect or Revoke is known), Mail sends that mailbox's own reserved offer and
registration again as an ordinary transfer after those pauses, unless the withdrawal
record may concern it or cannot be read; the host files it only for the account Google
names, and the dormant credentials serve no mail meanwhile. The host
writes sign_in_withdrawn.json in this skill's state directory before it confirms a
Disconnect or Revoke; this version keeps a copy that record may concern out of
use and judges a local sign-in by withdrawals recorded after it. Earlier versions
do not read this file. A Disconnect observed in the list or in a transfer result
ends the earlier grant. A separate local sign-in keeps its own connection on an older host.
If the newer host still lists a card sign-in, that row can serve Mail without replacing the
saved manual sign-in. Missing scope on the selected local sign-in can permit reading only;
it does not select earlier credentials. An unreadable withdrawal record or an unresolved
transfer does not prove the local sign-in can serve. A local refresh can keep a replacement
refresh token only in the sign-in that requested it. One local renewal runs at a time under that account's own lock,
which a transfer reservation of it also takes, so neither overwrites the other's
replacement; a stopped holder keeps the lock until its process ends. A
bearer Gmail refuses is not presented again: the host is told at once (`rejected`),
also after a refused change, and a read, never a change, is repeated once. A bound
change asks for 30 seconds of remaining bearer lifetime (`min_valid`). If the host
cannot supply that lifetime, Mail has not submitted the change; the consent stays
intact. A transferred grant is never copied back
from the host. Losing the host's
grant does not erase the account ID or its inbox, cursor, or outbox. The source
package contains no credentials.
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
- email_search uses a Gmail or Exchange query, or IMAP criteria. It retrieves
  headers/metadata, not every matching body. Results carry an account-bound
  message_ref; email_read accepts it, or legacy uid/uidvalidity/folder arguments
  from one search result of that folder. Before fetching, a read whose
  UIDVALIDITY differs from the folder's is refused with the folder and both numbers.
  To continue any provider's search, pass next_page_token back as page_token with
  the same account, folder and query or criteria until it comes back empty.
  IMAP criteria are raw SEARCH tokens: quote an ASCII operand containing spaces,
  as in `["SUBJECT", "\"New sign-in\""]`. Unicode operands use UTF-8 literals, with
  or without quotes, and folder names use modified UTF-7. An IMAP page lists
  matches newest UID first (`order: uid_desc`), the order the folder stored them,
  which can differ from their dates. It reports `total`, `returned` and `has_more`;
  its token keeps the first page's highest UID, so mail arriving meanwhile is not
  repeated or shifted, and a new search includes it. A token from another account,
  folder, criteria or UIDVALIDITY is refused. Server-side search can miss messages,
  so an empty or failed search does not prove that a message never arrived. One
  server missed a Cyrillic SUBJECT search (a phrase or one word) that its ASCII
  subject word found. Its cause remains unknown; the correct UTF-8 literal was
  captured only on a synthetic test connection, not that live server. For a
  non-ASCII subject or a missing document, search by FROM with SINCE/BEFORE, page
  through every result, compare each listed message's decoded `subject` yourself,
  read the candidates, and look in other folders where it may be filed.
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
  same item's native body, RFC headers and recipient/attachment metadata. Its
  address headers are parsed before names are decoded, as in MIME, so an encoded
  display name never adds a recipient. The result identifies this fallback, marks missing raw MIME and attachment bytes,
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
  and new names. The move source is a removed label, and Gmail mark-read exposes
  its fixed removal of UNREAD. `label_names` adds current label IDs and names
  (Drafts for DRAFT) not already exposed by the mutation.
- Every message or draft mutation reads the selected item's RFC conversation now,
  exactly as intake derives it (References, In-Reply-To, else Message-ID): Gmail
  delete, trash/untrash, flags, labels, archive, copy, move, mark-read and
  delete_draft; IMAP copy, move, flags and mark-read; Exchange copy, move, trash,
  flags and mark-read. Gmail uses raw MIME; Exchange uses intake’s MIME parser and
  its structured conversion-failure fallback. IMAP peeks at the whole header
  section, then the full source when no root is available, preserving intake’s
  parser boundaries and SHA-256 fallback. `message_conversation_id` (`draft_conversation_id` for
  delete_draft) and the same root in `message_thread_id` reach the Host, so a
  Place stored with conversation and thread holds the mutation, independently of
  folder and label Places. Supplied values are only compared: a missing or stale
  one returns fresh `retry_arguments`, a failed read changes nothing, and the
  provider method, path and body stay exactly the requested change.
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
  Gmail delete_draft and a draft_id update read the draft message first: its current
  labels (DRAFT, Drafts and custom names) and its RFC conversation in
  `draft_conversation_id`. All are Host targets; unrelated destination, label_id or
  add_labels targets are cleared, and the request stays the requested DELETE or the
  new content. A draft_id update checks that old conversation with its thread
  separately from the new content's conversation before saving.
  Permanent deletion by message reference checks the message's current labels too,
  including both names for Drafts and the current names of custom labels. Follow
  retry_arguments if the labels change. The usual full-mail grant is still required.
  Gmail and IMAP read the current targets before writing; another client may
  change the mailbox between that last read and the provider command.
- For Reply All, follow retry_arguments: Mail expands the original visible
  recipients before sending or saving. You may edit the explicit To/Cc/Bcc;
  leaving reply_all false keeps that choice. A permitted group thread remains
  permitted if a separate conversation with one recipient is paused, but a Silent or
  Paused Place of a recipient's bare address holds the whole send until that address
  is removed or its Place changes. Bcc from
  the original message is never copied into a reply. Keep the opaque reply_all_fingerprint
  returned by a prepared send. An exact repeat works even after the source disappears;
  if you edit the recipient list, replay the explicit accepted arguments. For an older
  send without a fingerprint, use its explicit arguments or inspect email_receipt.
  References comments, including encoded text inside them, do not supply ancestry.
  Historical Place and queue identities stay intact; a reply may therefore check both
  its old source scope and its actual emitted thread. A held legacy envelope is never silently corrected or resent.
  Old Exchange structured captures with lost address or comment syntax require a
  fresh read before a new reply; their accepted envelopes remain held for reconciliation.
- Recipients reach the Host as bare addresses, each once across To, Cc and Bcc.
  Named or comma-separated input returns retry_arguments with the bare lists and
  their names in `display_names` (bare address → display name), filled for Reply
  All from the source's From/To/Cc names. Sent and saved headers keep those names,
  quoted commas included. display_names may only name a listed recipient and never
  adds one. A bare call without names, or with matching display_names, queues in one
  call. A replay of a queued request_id keeps its accepted names; different
  display_names conflict. A name is one line of text without control characters;
  the header encoder quotes, folds and encodes it, so there is no other length or
  character limit. Before anything is prepared, queued or saved, the named headers
  are written and parsed again and must read back the same name and deliver to
  exactly the listed addresses: a name that would not (for example literal RFC 2047
  encoded-word text) is refused, and such a name taken from a source header is left
  out. Draft Bcc keeps its names in the saved header.
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
- email_send accepts sender, To/Cc/Bcc, MIME attachments and reply-all. Safety and
  an owner's approval judge the whole call: subject, recipients and their names,
  body, HTML and files; no single argument is declared as its words. With
  `body_type: html`, body is the HTML only when html_body is empty; an explicit body
  is always the plain alternative.
- A reply's `reply_to_message_id` is one Message-ID (another spelling returns
  retry_arguments). Its source must be in this mailbox for every provider: mail
  captured or read before, else the selected folder (reply_folder) whose answer
  carries exactly that ID. References and the conversation are the source's;
  supplied references must match, references without a reply source are refused,
  and the written reply headers are parsed back before queueing or saving. A
  queued request replays unchanged. Reply-all excludes configured own sender addresses from its automatic additions. The same
  account_id/request_id returns its original receipt; changed sender/content
  conflicts. The same request_id in another explicitly selected account is a
  different operation.
- Queued is not sent. Results/receipts expose pending, held, completed, failed,
  cancelled or uncertain state, actual account/sender and native provider receipt
  when available. An ambiguous SMTP/API acceptance is never blindly replayed; for
  Gmail the receipt also names what was found (`unknown_outcome`: sent, drafted,
  unresolved or discarded), the draft kept for it (`draft_id`) and the owner's
  notices. email_receipt also returns, while a mail has not gone out and once it was
  dropped (`cancelled`), the mail as queued (To, Cc and Bcc kept apart, its HTML and
  files), so it can be sent from another mailbox with email_send at the owner's word;
  a mail may have only Cc or Bcc recipients. A Place's or account's hold is asked
  about again and lifts when they allow.
- email_send with a Gmail `draft_id` sends that existing draft as it is now: the
  Host checks its actual To, Cc and Bcc (and its conversation when it replies) and
  Safety reads its subject and text (and HTML), which `retry_arguments` names first;
  its attachments are not named in the call. It is queued like any mail and goes out
  only while the draft is unchanged since that check (users.drafts.send): it is read
  again before its sending begins, under the sign-in it is sent with. A read Gmail
  does not answer, or answers busy or failing (429, 5xx), leaves it waiting without
  spending an attempt, asked again after Gmail's Retry-After (or a pause when it names
  none); a draft gone or changed since that check is refused. Mail keeps the
  draft's source and files as they were queued, with the mailbox's other staged mail:
  its receipt gives them, and rule A puts the draft back from them.
- email_mailbox `discard_queued` with a request_id drops, in one write, a mail of
  that account that has not begun to go out (also one a stopped worker had taken),
  also while the mailbox is signed out; its receipt stays with the mail, so replaying
  that request_id never queues it again. To send a waiting mail from another mailbox,
  drop it here first and then, only when that answers that it will not go out from
  this mailbox, send the mail its result returns from the other mailbox: it can then
  never go out from both. The answer is what the deleting write itself found, never an
  earlier look: a mail whose sending began meanwhile (the mailbox signed in again) is
  answered as possibly gone out. For a mail of unknown outcome not yet checked it
  means no draft is made; such a mail may already have gone out and is never offered
  for sending anywhere. `send_queued` sends a mail that is still queued or held now,
  under the mailbox's current confirmed sign-in.
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
  A send-as `values.replyToAddress` is checked as a destination: one bare address,
  or empty to remove Reply-To; other spellings of that field are refused.
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
same-thread checks select the recipient. Only absent, null or empty-object anchors
fall back to the wake event; an explicit unusable anchor holds the output. Recovering an anchor never changes an already queued receipt's
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
  anchor (absent, null or an empty object), the reply answers the polled mail. An
  explicit unusable anchor queues nothing. A usable anchor means the
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
an ambiguous SMTP disconnect, a Gmail answer lost after its request left, or a
process lost while sending remains uncertain and is never automatically resent; a
Gmail request, or the request for its bearer to the sign-in host, that never
connected sent nothing and is retried a few times, then fails; a Sending-off or
Place hold keeps waiting as before. Reports retain the transmitted text, subject,
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
`content_available: false` plus an error descriptor. A part with a filename or an
attachment disposition, and every attached message or report (message/rfc822,
delivery-status, disposition-notification, global), is an attachment rather than
the sender's body, body_html or signal.words text. Only when a message has no
un-named plain or HTML text of its own is a named inline plain/HTML part at its own
level read as the body (a lone `receipt.html`); an attachment disposition or a part
inside an attached message never is. A report or attached message has no single
payload file and reports `unsupported_mime_type`. The files inside an attached
message (message/rfc822, message/global) and every leaf of an attachment-marked
multipart follow it in the list and are staged like other files. `container_index`
links each child to its nearest carrying descriptor. That descriptor stores full
MIME type, file name and attached mail's From/Subject once in `container`; its own
`container_index` links outward. `depth`/`mime_part` locate the part in raw MIME.
Metadata grows with source headers and file count, without repeating long headers
for every child. Older retained rows keep their original `within` metadata. A blank or
markup-only own part counts as no text. A forwarded mail's text stays out of the outer body and its sender is not
the outer sender. The full raw RFC822 source is staged as an artifact so a clipped
part remains recoverable. These message
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
Every queued email, the automatic reply included, retains a binding for each actual
envelope address (To, Cc, Bcc; Reply-To or From for an automatic reply) beside its
conversation bindings, each with the match mode it was bound with. An address is bound
with `match=exact_casefold` under its policy key, the bare address casefolded (the
sent envelope keeps its spelling). Only Places naming that address apply there; an
account-wide Place stays a matter of the email's conversation, where an Active
conversation Place keeps its exception for a paused mailbox. Connect an address Place
with the bare address as conversation and no thread, in any letter case. Without an
account id it restricts deliveries from every mailbox; with one, only from that
mailbox. Two mailboxes differing only in letter case therefore share one restriction
(mail systems usually treat them as one; this skill claims nothing about a server).
No plus, dot or provider aliases are inferred, and existing Places are not migrated.
An address no Place names keeps the Host's unbound snapshot, so a Place connected
after queueing still holds the send. A new-format row with missing modes or malformed
recipient bindings (including null) is held as corrupt, never read with legacy defaults.
Each Presence event's `message.reply_targets` lists the automatic reply's actual
envelope (`{conversation, effect: write, match: exact_casefold}`), so the Host reads
those recipients' Places before Safety. The queued reply keeps the targets of the
anchor it answers, or of the wake event this transport submitted when there is no
anchor, and is held if they are absent, malformed or differ from its envelope. A
reply prepared before this field existed keeps its words, stays held and is never
resent; send it again with email_send if still needed. An anchor naming no captured
mail of its thread queues nothing; the reply is never redirected to another message.
Older queued mail keeps its rows and is checked for its addresses at delivery.
The SMTP worker re-reads the place immediately before sending. Silent/Paused
or an unavailable lookup holds the same queue item; Active resumes it without
spending the retry budget on policy holds, and an unavailable lookup clears once
Ouroboros answers. When Ouroboros refuses the skill itself (its grant,
enablement, review or token), the hold lasts until its access is fixed in Skills.

`email_read` and `email_folders` are declared read, so a Silent or Advises place can read mail;
`email_mark_read` and the mixed `email_mailbox` (it can mutate) are declared
write. Sending is message; search, receipts and connection checks are declared
read. Each affected scope is a separate `touches` group with its own thread
(PluginAPI 2.5): email_send's primary destination is its source conversation (the
account for a new unthreaded mail), its emitted RFC root is a message group, and
To/Cc/Bcc are audience plus `exact_casefold` write groups. email_draft's To/Cc/Bcc
are audience only; folder, folder names, the replaced draft's and the emitted
conversation are `exact` write groups, and Mail's own folder lookup still holds a
draft under an account-wide Silent/Paused Place. Mailbox writes keep folders and
labels as destinations and pair the message's RFC root with its thread.

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
