# Changelog

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
