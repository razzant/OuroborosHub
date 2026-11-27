# Changelog

## 0.11.0

- Hosts with service sign-in manage Gmail on the email-presence card, with a
  separate consent per mailbox and its own selected OAuth client. Existing
  Gmail accounts keep their local account IDs, preferences, mail and receipts.
  Manage Gmail settings and permanent deletion remain optional permissions.
- Older hosts keep the local sign-in path. After a transfer, a confirmed older
  host can use a still-valid saved grant within its recorded permissions (a renewal
  without an optional permission keeps ordinary mail), and a new local sign-in there
  serves the moved mailbox under its account ID instead of pointing to a card that
  host lacks; a rotated or withdrawn grant may require new consent. A mailbox its own
  saved sign-in still serves is disconnected in Email accounts on any host.
  Unavailable hosts and unconfirmed transfers never reactivate
  the old grant. IMAP, SMTP, Exchange and Place addressing are unchanged.
- A transfer whose answer never arrived is not submitted again unless the host's
  receipt says `resubmit` (below). Mail first reads
  the host's own receipt of it where the host announces `sign_in_handover_version` 2,
  then asks with `recover` (it registers and exchanges nothing) for the outcome it kept under
  the same account and generation, at once and then after pauses that grow from
  30 seconds to 15 minutes (in memory, so a restart asks again at once). Only
  the host's per-item word that nothing was exchanged lets the saved original
  continue, as does a `429` with `handover_started: false` to its own first offer
  or a connection failure before that offer was sent. The transfer is offered again
  later; another reason, another status code or
  a recover answer changes nothing. When only an outage stopped the transfer before
  any exchange while an earlier Disconnect or Revoke is known, the receipt says
  `resubmit`: unless the withdrawal record may concern that mailbox or cannot be
  read, Mail sends its own reserved offer and registration again as an ordinary
  transfer after those pauses, the host files it only for the account Google
  names, and the saved original stays parked meanwhile. A client is registered only with the
  return address its saved sign-in recorded; one without it moves, as
  `return_hint: "unknown"`, only to a host announcing `sign_in_handover_version` 2
  and keeps serving unreserved until then. No installation default client is
  registered. A change (send, label,
  settings, deletion) names its confirmed connection and requests a bearer valid
  for 30 seconds. Failure to obtain that lifetime does not end the consent. A
  Disconnect or Revoke ends the grant it concerns. A later independent sign-in on
  an older host keeps its own connection, including when a receipt for an older
  transfer arrives afterwards. Earlier skill versions do not read withdrawal records.
- A refused bearer is never presented again (a read is repeated once, a change never),
  and a refresh token Google replaces is kept: a local refresh and a transfer of one
  sign-in never overlap (one lock per account, kept while its process lives), and a
  renewal stores its bearer only into the connection it started under, so an unrelated
  edit meanwhile keeps the rotation. A renewal answer naming no permissions is refused.
  A transfer carries a bearer only with the permissions Google named for it, so a
  narrower one stays narrower; a bearer without that record gets no optional
  operation until a renewal records it. A new local sign-in starts another connection.
  On a host announcing `sign_in_handover_version` 2 a transfer asks for a verified
  identity, and a change keeps the consent of the host's current row. A change (send,
  label, draft, settings) is made under the access named when it began; a sign-in that
  changes in between stops it before anything is sent or changed. A send whose access
  cannot be named at that moment queues nothing.
- Queued Gmail mail that has not gone out goes, as before this version, under
  whichever confirmed sign-in of its mailbox's own Google account (its verified
  subject, never a matching address) serves it when it leaves: the host's current
  connection and consent or the mailbox's saved sign-in, mail queued by an earlier
  version or before a new sign-in included. While no confirmed sign-in serves the
  mailbox it waits unsent and the owner is told once which mail waits; it goes by
  itself once the mailbox is signed in again, unless Sending is off or a Place held
  it, which the line then says instead. A saved sign-in Mail is moving to the host
  is not a sign-out and posts no line. A sign-in host that cannot be reached for the
  mail's bearer leaves it waiting too, held without spending an attempt, until it
  answers.
- A Gmail mail whose sending began and whose outcome is unknown is never sent a second
  time. Once Gmail search can show it, its own mailbox is searched for it by Message-ID:
  found in that mailbox's Sent, it is recorded sent (a copy that came in, in Inbox or
  Spam, proves nothing; the search asks for sent copies, Trash included, and reads every
  page of its answer); found as a draft, that draft is kept; otherwise one draft of it
  (envelope with Bcc, body, attachments, thread, Message-ID) is saved in that mailbox's
  Drafts. A mail sent from an existing draft is put back as that draft was queued: Mail
  keeps the draft's source and files when it queues it. The draft is saved only once the
  Places of that mailbox's Drafts, and for a reply those of its conversation (asked with
  its thread), allow writing there, asked as email_draft asks; while a Place or this
  skill's access holds them, the mail waits and the owner is told once.
  Nothing is drafted in another mailbox. The owner is told once, in one line naming the
  subject and a short reference Mail gave the mail in its mailbox (never the caller's
  request_id, which may carry an address and may read like another's): "The email ...
  (ref m-...) is in Drafts of ...: check it and send it". Gmail may give a sent message a Message-ID of its own, so a
  search that finds nothing never proves it unsent: that is why the line asks to check it.
- Those lines go through the host's `notify_owner` permission (`POST /notify`, one System
  row in the owner's chat, mirrored to Telegram); recipients, Bcc and body stay out of
  them. Until the host confirms one (no grant yet, an older host without `notify_version`,
  the host away) it stays pending, is shown in the mailbox's status, the widget and the
  mail's receipt, and is posted later; a 503 from the host is posted again after a pause.
  A line is posted only while it is still true: one saying a mail waits ends unposted, in
  the same write, once the mail goes, is deleted or settled, or once its mailbox is
  signed in again; each line is read again, with its mailbox's sign-in, just before it is
  posted, so a change made while the host took the line before it is seen. A send whose worker stopped during it is known to be of unknown
  outcome also while its mailbox is signed out.
- "Send" and "delete" in chat are the mind's: `email_send` with a Gmail `draft_id` sends
  an existing draft as it is now (the Host checks its actual recipients and Safety reads
  its subject and text, returned in `retry_arguments`; its attachments are not named in
  the call; it goes out only while unchanged since that check);
  `email_mailbox` delete_draft removes it; `email_mailbox` `discard_queued` drops, in one
  write, a mail that has not begun to go out, also one a stopped worker had taken (its
  receipt stays as `cancelled`, with the mail, so a replayed request_id never queues it
  again) and `send_queued` sends one now under the mailbox's current sign-in. To send a
  waiting mail from another mailbox, the mind drops it first and sends the returned mail
  there only when the drop answers that it will not go out from here, so it never goes
  from both; that answer is what the deleting write itself found, so a mail whose sending
  began meanwhile is answered as possibly gone out, and a mail of unknown outcome is never
  offered for that.
  `email_receipt` names the unknown outcome found (`unknown_outcome`), the draft kept
  (`draft_id`), the owner's notices and, while a mail has not gone out or once it was
  dropped, the mail as queued: To, Cc and Bcc apart (a mail may have only Cc or Bcc
  recipients), HTML and files, those of a draft sent by its id included. `email_accounts`
  lists every mail that waits (`waiting`) by request_id, ref and subject; those tools and
  `email_receipt` take the ref in `request_id` as they take the request_id itself, and a
  new request_id that is another mail's ref in its mailbox is refused. A draft sent by its
  id is read again before its sending begins, under the sign-in it is then sent with, so a
  read that fails leaves it queued and a renewed bearer serves both; a read Gmail does not
  answer, or answers busy or failing (429, 5xx), waits without spending an attempt, asked
  again after Gmail's Retry-After, so a long outage never ends the mail.
- An unreserved mailbox keeps its local sign-in within recorded Gmail permissions
  even when automatic transfer is not possible. The host's complete list must name
  no sign-in of its subject and no withdrawal may apply. A change reads the list
  anew; ordinary reads in a search or poll can share its snapshot. A grant that
  recorded neither its permissions nor those Google named for its current bearer keeps
  reading only; no change, enqueue or transfer is inferred. The host's own receipt of one exact
  generation with `refused` and `exchange: none` closes that attempt like the first
  answer's word and gives the saved sign-in back, whatever its reason; it confirms
  no earlier mail, new owner, break or permission, and a recover answer still never
  does. Only a transfer's `imported` receipt gives its mail a consent.
- A change Gmail refuses with 401 is never repeated, and the host hears at once of
  that very bearer (`rejected`). A Gmail request that never connected sent nothing: it
  is retried a few times, then fails, never uncertain; a request for its bearer to the
  sign-in host that never connected leaves the mail waiting, as above. Sending-off and
  Place holds keep waiting. A lost answer after the request left stays uncertain and is
  looked for as above.
- Full Gmail access is named so before sign-in, with what Mail uses it for (permanent
  deletion that skips Trash, and Gmail settings); no permission changed. The card is
  pointed to by its technical name, email-presence; Email inbox is the catalog's title
  for it, which may not be what its installed card shows.

## 0.10.11

- Requires PluginAPI 2.5. Tool rows declare `touches` groups instead of flat targets
  mixed with one conversation/thread. email_send keeps its source conversation and
  thread as the primary destination; To, Cc and Bcc are its audience plus separate
  write groups matched `exact_casefold`; an emitted RFC root is its own message group
  with its own thread. email_draft keeps To/Cc/Bcc as audience only (a draft delivers
  nothing); its folder, folder names, replaced draft and emitted root are write groups
  matched `exact`, beside the unchanged folder lookup that treats an unconnected
  folder as writable. email_mailbox and email_mark_read pair the message's RFC root
  with its own thread instead of applying that thread to every folder and label.
  email_send still declares no words argument: Safety and the owner card judge the
  whole call.
- A Silent or Paused Place naming a bare email address now holds every delivery to
  that address: email_send To, Cc and Bcc, Reply All, and the automatic Presence reply,
  whose envelope is Reply-To (or From). Each envelope address is bound with
  `match=exact_casefold` under its policy key (the bare address, casefolded; the wire
  keeps its spelling) and the mode is stored beside every new binding and sent again at
  check. A Place stored in another letter case applies; equally ranked spellings hold
  when any is Silent/Paused; an address Place without an account applies to every
  mailbox and one with an account only to that mailbox. No aliases are inferred. A
  paused RFC conversation still holds only that conversation. An address no Place
  names keeps the Host's unbound snapshot, so a Place connected later still applies.
  Rows accepted earlier are checked at delivery without being rewritten; a new-format
  row with missing modes or malformed recipient bindings, including null, is held as
  corrupt, never read as legacy.
- Each Presence event carries `message.reply_targets`, the automatic reply's actual
  envelope, so the Host reads those recipients' Places before Safety. The queued reply
  keeps the targets of the anchor it answers (or of the submitted wake event) and is
  held when they are absent, malformed or differ from its envelope. Automatic replies
  prepared before this version keep their words and stay held; nothing is resent. An
  explicit unusable anchor queues nothing. Only an absent, null or empty-object anchor
  falls back to the polled mail; malformed selections stay in the retained receipt.
- References use RFC tokens: angle brackets inside comments are not message IDs.
  Old captures keep their Place keys; new replies derive ancestry from retained raw
  headers and check both historical and emitted scopes. A fresh read cannot erase
  an old scope's hold. Already accepted comment-derived envelopes stay unchanged and
  held. The advertised legacy Place lookup also carries the conversation's thread.
- Intake, Exchange reads and retained raw headers read References/In-Reply-To with
  one rule: an encoded word outside comments decodes as intake always decoded it, so
  replies keep that ancestry; one inside a comment or quoted string stays comment
  text. Old Exchange text that survives only decoded is not guessed where decoding
  could have moved a comment boundary: a new reply needs a fresh read, an accepted
  envelope stays held, and the stored scope is still checked.
- Old Exchange structured captures that lost raw address syntax cannot choose new
  reply recipients. Read the original again for a new request; previously accepted
  envelopes remain held, with their identities and content preserved. New captures
  retain raw address/thread headers; no mass migration or uncertain resend occurs.
- Message and draft mutations on Gmail, IMAP and Exchange (delete, trash/untrash, flags,
  labels, archive, copy, move, mark-read, delete_draft) read the item's RFC conversation
  with the same parser and fallback chain as intake. IMAP peeks at the whole header
  section, then the full source when needed; Gmail reads raw MIME and Exchange uses
  its MIME/native structured fallback. Lossy metadata never supplies mutation authority.
  IMAP and Exchange accepted no conversation target before. The root reaches the Host as
  `message_conversation_id` (`draft_conversation_id` for delete_draft) and as the new
  `message_thread_id`, paired in one touches group on email_mailbox and email_mark_read, so
  Places stored with conversation and thread hold these writes; folder and label Places
  still apply. A Gmail draft_id update checks the replaced draft's conversation with its
  thread. Provider methods, paths and bodies are unchanged; a canonical call that omits
  the new targets returns them once in retry_arguments.
- Gmail send-as `values.replyToAddress` is a declared Host destination: one bare
  address or empty. Settings values and filter actions must be objects before any
  coercion; pair arrays and alternative Reply-To field spellings are refused.
- Replies: `reply_to_message_id` must be one Message-ID (another spelling returns
  retry_arguments) and its source is looked up in this mailbox for every provider. On IMAP
  and Exchange, caller references used to skip that lookup and decide the conversation.
  References and the conversation now come from the verified source; different supplied
  references return `conversation_identity_mismatch`, and references without a reply
  source are refused. Store and provider lookups compare canonical tokens while retaining
  comment-bearing source IDs and refuse conflicting matches across pages. The written
  reply headers are parsed back before queueing or saving. A legacy multi-ID In-Reply-To
  keeps its key; an independently emitted root is exposed as emitted_conversation_id and
  checked through existing Place bindings/holds, including automatic replies and delivery.
  Accepted queue rows replay unchanged; old rows gain no rewritten binding or headers.
- `email_send` declares no words argument, as in 0.10.7: Safety and owner approval cover
  the whole call, including subject, names, HTML and files. A Safety hold therefore follows
  the exact call (another request_id is read again) and the turn's send record carries no
  mail text. With `body_type: html` and both fields, the explicit body is now the plain
  alternative instead of being replaced by text rendered from html_body.
- Search documentation names the observed miss of a Cyrillic SUBJECT search and the
  procedure: FROM with SINCE/BEFORE, every page, then the decoded subjects compared locally.
  Only synthetic wire captures verify the UTF-8 literal; the live Proton mismatch cause
  remains unknown. Gmail search metadata cannot inject header separators.
- Gmail search of Spam or Trash sends `includeSpamTrash` on every page.
- Exchange's structured fallback (no MIME) decoded each whole address header before
  splitting it, so an encoded display name with a comma or `@` became an extra
  recipient of automatic replies and Reply All. Address headers are now parsed from raw
  syntax with the MIME reader's model, names included; native SMTP mailboxes remain the
  fallback for an absent or damaged header.
- A second `email_read` of another message with the same Message-ID but different reply
  authority no longer replaces the first in the reply cache: the replaced read is kept
  (`read_source:` beside the unchanged `read_message:` key), so the ID stays ambiguous
  and is refused instead of answering whichever was read last. `reply_folder` selects
  the provider's verified source in that folder. Repeat reads of one message, identical
  copies and older cache entries behave as before.
- An old queue row whose emitted scope is bound at delivery is held, without spending
  attempts, when the Host cannot answer (it used to fail after five tries, unsent).
- Replies write the source's canonical Message-ID token in In-Reply-To/References: a
  comment spelling another ID is no longer read as the conversation root. Stored sources,
  queue rows and Place keys keep their spelling; both scopes are still checked.
- Recipients reach the Host as bare addresses, each once across To, Cc and Bcc; named,
  comma-separated or repeated input returns `canonical_recipients_required` with the
  bare lists. 0.10.10 then dropped the display names from the sent headers. The names now
  travel in the new optional `display_names` argument (bare address → name), filled from
  named input and, for Reply All, from the source's From/To/Cc. Sent and saved To/Cc keep
  them, quoted commas included. `display_names` can only name a listed recipient. Bare
  calls still queue in one call; queued request_ids, including rows queued before this
  version, replay unchanged. Before anything is prepared, queued or saved, the named
  To/Cc/Bcc headers are written and parsed again; they must read back each name and
  deliver to exactly the bare addresses. A name that would not, such as literal RFC 2047
  encoded-word text that decodes into an address, or a line separator that cannot be
  written, is refused, and one taken from a source header is left out. A name the header
  model cannot carry is an error, never a silent bare address. The 256-character limit is
  gone; long names are folded. Draft Bcc keeps its names.
- MIME: a non-root part with a filename or attachment disposition, and every attached
  message or report, belongs to the attachment list, never to `body`, `body_html` or the
  `signal.words` text. Delivery, read-receipt and message/global reports were staged as
  empty files marked available; they now report `unsupported_mime_type`, and the retained
  raw source holds them. Files inside an attached message (message/rfc822,
  message/global) or an attachment-marked multipart are still listed and staged, as in
  0.10.7, with full parent metadata stored once as `container` on its descriptor. Children
  link by `container_index`, with `depth` and `mime_part` locating them; long headers
  are not repeated across nesting or fanout. Older rows keep `within`. The attached
  mail's text never becomes the outer body. A named inline plain/HTML part is the body only
  when the message has no readable un-named text of its own; a blank or markup-only first
  part no longer hides it.
- Reply All keeps RFC 2047 names that contain a comma, such as "Doe, John": the source's
  To/Cc/From/Reply-To names are stored as parsed `address_names`. Reads stored by earlier
  versions keep the old decoded-header lookup.
- IMAP folder listing failed when the server sent a folder name as a literal. Listing and
  the CREATE parent lookup now share one parser for literals and their trailers.
- Delivery re-reads the account after opening the provider connection. Sending turned off
  or a disconnect during that time holds the same queued item before anything is sent.
- Account test in the widget: a rejected IMAP/SMTP login or a TLS/connection failure
  answers JSON with its stage and remedy instead of HTTP 500; server reply text and
  credentials are not repeated. The OAuth routes answer a non-object JSON body with a
  structured 400, and a Google transport failure with a redacted 502.
- Exchange HTTP 401 diagnostics name the remedy (edit and save the connection).

## 0.10.10

- IMAP search continues past its first page. `email_search` returned only the `limit`
  newest matching UIDs (at most 200) and a `total`, with no way to reach older matches.
  An IMAP page now also reports `returned`, `has_more` and `next_page_token`; passing that
  token back as `page_token` with the same account, folder and criteria returns the next
  older UIDs. Pages run newest UID first (`order: uid_desc`), the folder's storage order,
  which can differ from message dates. The token keeps the first page's highest UID, so
  mail that arrives meanwhile is neither repeated nor shifted. A token from another
  account, folder, criteria or UIDVALIDITY is refused. Nothing is stored between pages.
- Reading with a UIDVALIDITY that does not match the folder now names the folder and both
  numbers. It used to say that the mailbox UIDVALIDITY changed, also when the UID came
  from a search of another folder. The refusal still comes before anything is fetched.
- The tool schema, SKILL.md and README say that criteria are raw IMAP tokens: an ASCII
  operand containing spaces needs quotes, as in `["SUBJECT", "\"New sign-in\""]`. They
  also say that an empty or failed search is not proof that a message never arrived.

## 0.10.9

- Connecting Google mail can leave out the permission to manage Gmail settings (filters,
  forwarding, sender addresses). A Workspace whose administrator approved only mail access
  refused the whole sign-in with `admin_policy_enforced` because of it. The choice is on by
  default, as before; without it mail works, and a settings write answers
  `settings_permission_missing` before contacting Google.

## 0.10.8

- A message's `Date` header with a time zone is also sent as `message.sent_at` (ISO, in the
  sender's offset; `-0000` as UTC), so the Host can say when the person wrote. A Date without
  a zone or an unreadable one leaves it unknown; the server's receipt time is never used.
- When Exchange cannot render a message as MIME, its native read now names the message's own
  Date header as `date`, as the MIME reader does; Exchange's receipt time stays `internal_date`.

## 0.10.7

- Before permanently deleting a Gmail message, check its actual label IDs and
  names, including Drafts when the message is a draft. Recheck on retry.
- Before creating a nested IMAP mailbox, include missing intermediate folders
  and their nearest existing parent, using both selected and server-returned
  names when they differ.

## 0.10.6

- A hold line says the state and the next step, not the Host's answer behind it. An
  unread Place policy reads `Held: It retries by itself.`, a refused skill
  `Held: Check this skill's access in Skills.` Mail that will never go asks to be sent
  again: `Held: Work mail was disconnected, so it will not go out; send it again if still
  needed.` names the Place, which the owner may have disconnected on purpose, and
  `Held: It will not go out; send it again once a place covers it.` says that none covered
  it at queueing. A send no connected Place covers now, including one queued before Place
  bindings, reads `Held: No connected place covers this send.`, and sending turned off
  reads `Held: Sending is off for this mailbox.` A Place's state reads as before; codes
  stay in the debug log (`LOG_LEVEL=DEBUG`).

## 0.10.5

- Check the actual folders changed by IMAP and Exchange mailbox actions.
  Copying from a folder still requires only read access to the source.
- Check the Gmail Drafts folder before deleting a draft by its draft ID.
- Return the complete Reply All recipient list before queuing a send or saving
  a draft. The mailbox and RFC thread continue to govern group replies.

## 0.10.4

- Keep one connection per mailbox store open instead of opening one for every call. An
  inbox or outbox loop with nothing to claim now only reads: it takes no write lock and
  writes nothing. A claim with work still settles a send whose SMTP acceptance is unknown
  as `uncertain` first and is never sent again; which messages are leased, in what order,
  is unchanged. Every commit stays `synchronous=FULL`. The companion closes a mailbox's
  connection when its worker stops, including host/client cleanup failures. Discarding
  a temporary tool store closes its connection too.

## 0.10.3

- Check the selected Gmail account, forwarding address, and affected label IDs
  and names before creating a filter. All Gmail settings writes check the actual
  account; a canonical retry preserves the complete rule.
- Keep malformed Message-ID headers from stopping intake or hiding later mail.
  Header-only search may return a message reference without an RFC ID; read by
  that reference for the full identity. IMAP rescans recognize identical old
  captures from their retained source, without merging different messages.

## 0.10.2

- Preserve existing Exchange message and folder references while verifying their
  actual mailbox with the server before using native identifiers.
- Expose draft folders and the Gmail labels a mailbox action changes to the
  Host before writing, including when a caller uses an alternate argument.
  Writes with an implicit sole account, draft folder, or incomplete Gmail targets can
  return `needs_arguments` without writing; replay the returned `retry_arguments`.

## 0.10.1

- Say what a hold means when the Host did not say whether a Place allows sending:
  `Held: Ouroboros did not answer whether this place allows sending. Nothing was sent
  yet; it retries by itself and no approval is needed.` `Held: place policy unavailable`
  and a printed Host code read like a pending approval.
- Name the Place's state in a Host's refusal (`Held: Work mail is paused`), not its code
  (`Held: place_paused`). A send no connected Place covers says so. The code stays in the
  debug log (`LOG_LEVEL=DEBUG`).
- A send whose Place was disconnected after it was queued says that it will not be sent
  and should be sent again if it is still needed. Connecting the Place again does not
  release it, so `Held: Work mail is disconnected` was wrong once Work mail was back.
- A send no connected Place covered when it was queued says once that it will not be
  sent and should be sent again once a Place covers it. The tool's answer and the
  receipt after the outbox's next pass used to say this fact in two different sentences.
- A Host that refuses the skill itself (no grant, turned off, unreviewed or a stale
  token) waits for its owner, not for a retry: `Held: Ouroboros refused this skill's
  request, so nothing was sent; check the skill's access in Skills.` It replaces
  `Held: host authorization blocked` and, for sends queued before Place bindings,
  the words that promised a retry without approval.

## 0.10.0

- Connect on-premises Exchange mailboxes over HTTPS with NTLM. Exchange uses
  the existing account widget, mail tools, optional Place intake and outbox.
  Mailbox addresses and credentials belong to the local installation.
- Preserve native Exchange item references alongside RFC reply identity.
  Reads do not mark messages read. A successful Exchange send can have no item
  ID; an ambiguous response stays uncertain without an automatic resend.

## 0.9.3

- Keep already-admitted deferred replies in custody across unreadable work polls;
  canonical terminal results still settle once without resubmitting the turn.
- Declare actual mailbox and filter-forwarding destinations before effects.
  Canonical argument preparation preserves public message references, label properties and filter data,
  with ordinary matching calls remaining one call and no additional approval.

## 0.9.2

- Prepare the canonical mailbox account before new sends and drafts so the Host
  checks the actual reply author. Explicit-account sends remain one call, and
  replaying an existing receipt does not enqueue another message.
- Preserve reply anchors on retained released and terminal outputs so selected
  speech targets the captured message the Host named. Existing queued mail keeps
  its original recipient.

## 0.9.1

- Parse complete RFC mailbox lists before To/Cc/Bcc grouping and envelope delivery,
  preserving quoted display names and rejecting malformed or empty destinations.
- Expose canonical RFC conversation fields and prepare missing reply arguments
  before the existing Host guard admits a send or draft. No new core seam or
  mandatory Place is needed; existing queued requests retain their receipts.
- Deliver selected terminal speech even when its task failed or was cancelled,
  preserving output identity and releasing cached views after durable settlement.
- Settle supported No-Place refusals once without replaying old inbox items when
  a Place is later connected; transient and unsupported-Host refusals keep retry custody.
- Create new mailbox state directories and databases with private permissions.
- Keep malformed-recipient diagnostics from stopping the mailbox worker or
  ending an active author before its later valid outputs can be delivered.
- Use direct transport for the local Google return-page probe, preserving
  public origins, deployment prefixes and injected transports.
- Retain readable malformed incoming address headers without blocking later
  mail, and normalize the remaining stdlib outgoing-address parser failures.
- Decode ordinary raw UTF-8 and folded header views for capture and tool results;
  keep undecodable words readable and preserve the original MIME bytes.

## 0.9.0

- Add multiple independent Gmail API and IMAP/SMTP accounts, separate sender
  addresses, account-bound message references and account-scoped request ids.
  Existing EMAIL_* accounts retain their original state and credentials.
- Add skill-owned Google PKCE sign-in through the builtin return page, metadata
  search, native Gmail drafts/labels and provider-supported account settings.
  Calendar consent stays separate; full-mail deletion scope is optional.
- Support Bridge STARTTLS with explicit CA trust or certificate pin, separate
  SMTP credentials, Unicode folders/search and trailing IMAP FETCH metadata.
- Keep ordinary account tools independent from optional Presence intake while
  preserving applicable Silent/Paused holds and retained enqueue bindings.
  Known replies carry their thread, capture outages retry the cached output, and
  held rows avoid provider login while still rechecking before actual delivery.
- Add account setup/preferences in the existing opaque widget, queue counts and
  recent receipts in both account discovery and UI, including after disconnect.
  No signatures, automatic purge or per-message approval flow is introduced.

## 0.8.0

- Negotiate Presence continuation version 2 only when `/identity` lists it in
  `presence_continuation_versions`. Hosts that do not list it keep plain turns.
- Treat the early `continuing` answer as custody: store it, release the thread
  and observe the author with one poll per later claim.
- Run the inbox as two loops under the companion's existing lifecycle: one only
  submits new mail, the other only polls stored references. A slow poll never
  holds new mail, and a continuous inflow never holds observation.
- Keep a `presence_capacity_full` refusal as its thread's head. It is resubmitted
  unchanged after max(hint, backoff): the hint is the Host's `retry_after`, never
  shortened; the backoff is this transport's own, 1 second doubling to a 30-second
  cap. Other threads continue meanwhile.
- Reply to the captured mail a version 2 output's `reply_anchor` names; an
  initial answer's receipt keeps its anchor too. Its message's
  folder/UIDVALIDITY/UID select the mail, which must match the anchor's
  `source_event_id` and Message-ID and be in the polled mail's thread.
  Children, retained views and unbound anchors reply to the polled mail as
  before. Each `output_ref` is queued once.

## 0.7.0

- Keep the core mail fact plain in the manifest for older hosts; publish
  "Mail addressed to you" in the status payload for the place sheet.
- Declare the mailbox's sending/reading identity and unverified sender status
  only to hosts advertising `presence_identity: 1`.
- Report direct mail, automated senders and configured whole-word signals;
  suggest no immediate wakes at Connect.
- Add a sending switch while retaining reads, drafts and mailbox tidying.
- Refuse reuse of a request id for a different payload; identical retries keep
  the first delivery and its receipt.
- Preserve authored outgoing text without an added signature or machine marker.
