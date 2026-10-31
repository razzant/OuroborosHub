# Changelog

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
