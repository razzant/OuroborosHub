---
name: freshdesk
description: Freshdesk helpdesk tools and place. Read and search tickets and the knowledge base, reply to customers, write notes, update fields by name; connect the place to hear new tickets, customer replies and teammates' notes.
version: 0.1.0
type: extension
entry: plugin.py
plugin_api: "2.4"
runtime: python3
os: [darwin, linux, windows]
permissions: [net, read_settings, route, tool, widget, companion_process, presence]
env_from_settings: [FRESHDESK_API_KEY]
secret_fields: {apiKey: FRESHDESK_API_KEY}
timeout_sec: 120
when_to_use: User wants Ouroboros to work in a Freshdesk helpdesk - read, search or summarize tickets, look up customers or knowledge-base articles, reply to customers, write notes, change ticket fields - or to be present in the helpdesk and hear new tickets and replies.
model_experience:
  what_model_sees: Eleven freshdesk_* tools (six reads, three that put words in front of people, two other changes). A connected place adds Freshdesk events to rooms, one room per ticket, with the ticket's thread.
  token_effect: About 2,300 tokens of tool schemas every round while enabled; reads and events add the actual ticket text. Watching costs no model tokens until an event wakes a focus.
companion_processes:
  - name: freshdesk_watcher
    command: [python3, scripts/watcher.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
tools:
  - name: freshdesk_status
    effect: read
    target: []
  - name: freshdesk_fields
    effect: read
    target: []
  - name: freshdesk_find
    effect: read
    target: []
  - name: freshdesk_ticket
    effect: read
    target: []
  - name: freshdesk_knowledge
    effect: read
    target: []
  - name: freshdesk_get
    effect: read
    target: []
  - name: freshdesk_reply
    effect: message
    target: [account_id, ticket_id]
    conversation: ticket_id
    thread: ticket_id
    text: text
  - name: freshdesk_note
    effect: message
    target: [account_id, ticket_id]
    conversation: ticket_id
    thread: ticket_id
    text: text
    sends:
      when: {audience: [notify]}
      target: [account_id, ticket_id, notify]
      conversation: ticket_id
      thread: ticket_id
      text: text
  - name: freshdesk_email
    effect: message
    target: [account_id, ticket_id, to_emails, cc_emails, bcc_emails]
    conversation: ticket_id
    thread: ticket_id
    text: text
    sends:
      when: {op: [outbound]}
      target: [account_id, to_emails, cc_emails, bcc_emails]
      text: text
  - name: freshdesk_update
    effect: write
    target: [account_id, ticket_id]
    conversation: ticket_id
    thread: ticket_id
  - name: freshdesk_request
    effect: write
    target: [account_id]
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: Freshdesk
    destination: vendor:Freshdesk
    scope: account
    icon: ui/icon.svg
    wakes: [new_issue, assigned, my_thread, mention]
    source: {route: status, refresh_s: 30}
    settings: freshdesk
---

# Freshdesk

Freshdesk is a customer-support helpdesk: every customer request is a ticket with a thread of replies (emailed to
the customer) and notes (private to the team unless made public), plus properties such as status, priority,
group and assigned agent. This skill works in one helpdesk as one Freshdesk agent, with that agent's role and
ticket scope: what the agent may do and see, you may do and see.

## Setting up

1. The owner saves the helpdesk address (example.freshdesk.com) in Settings → Freshdesk.
2. Ask for the agent's API key with `showSecretHitl('freshdesk', ['apiKey'])` and wait; the key never enters the
   chat. The owner grants the saved key to this skill.
3. `freshdesk_status(probe=true)` names the agent, its scope and which endpoints the key may use. It tests no writes.
4. To be present in the helpdesk, the owner connects the Freshdesk place in Circles. It starts Silent.

What the helpdesk's administrator provides: a dedicated agent for Ouroboros (a full-time seat; or an occasional
agent, which needs no licence and, per Freshdesk, spends no day passes on API use), its role (an Agent can reply;
a Collaborator sees only tickets it is tagged on, cannot reply or change properties, but can write private notes;
a custom role without "Reply to ticket" makes the API refuse replies), its ticket scope (all tickets, its groups,
or tickets assigned to it), and the agent's API key (Profile settings → View API key; resetting it revokes it).
Freshdesk's API does not answer on a custom domain, only on the `.freshdesk.com` address. Ticket text goes to this
installation's model provider.

## The place

One place covers every ticket this agent can see; each ticket is its own room (conversation and thread are the
ticket number). The watcher checks Freshdesk every minute while the place is connected and reports what changed:

- a ticket created after the place was connected: `new_issue` (and `assigned` if it is assigned to this agent);
- a new reply or note: `any_message`, plus `my_thread` if this agent wrote earlier in the ticket and `mention` if
  the note notified this agent;
- a ticket newly assigned to this agent: `assigned` (an older ticket first seen this way says the assignment time
  is unknown);
- other property changes (status, priority, group, tags…): no words and no wake; a focus working on that ticket
  hears them as background. Choosing "Any ticket activity" wakes on them too.

Tickets and entries from before the connection are history: they appear in the thread, never as new events. A place
removed and connected again is a new connection. What this skill itself sent, its note edits and property changes,
and tickets it opened (an outbound email, a created ticket), never come back as arrivals; a reply on such a ticket is
`my_thread`. A ticket a colleague opened by outbound email starts with the helpdesk's words, not the customer's.
Customer entries are marked unauthenticated (an email sender is not verified). The watcher finds new entries because
Freshdesk moves a ticket's `updated_at`; a change made and reverted between two checks is not seen. A ticket deleted
or moved out of this agent's scope before it was read is skipped and noted in `freshdesk_status`. This version is
checked against a faithful stand-in of Freshdesk's API, not yet against a live helpdesk.

## Words and changes

- `freshdesk_reply` emails the ticket's requester (and the ticket's existing CCs). `freshdesk_note` writes a private
  note, or a public one with `public=true`; to mail agents about it pass their emails in `notify` with
  `audience="notify"`; `edit_note_id` edits a note and keeps who sees it (say `public=true` for a public note).
  `freshdesk_email` sends with explicit recipients: a reply that adds cc/bcc (a reply always goes to the requester:
  to write to someone else use a forward), a forward, a reply to a forward (to_emails only), or an outbound email
  that opens a new ticket from the mailbox `email_config_id` names (`freshdesk_ticket` shows a ticket's
  `email_config_id`). A recipient Freshdesk would drop is refused, never ignored. This skill addresses customers only
  through these tools (a public note is shown to the requester; the helpdesk's own notifications may also mail). When
  longer work on a ticket ends with a final answer, that answer is posted on the ticket as a private note, not
  emailed. An answer that finishes while the place is Silent or Paused is held: it stays with the owner in the room
  and is not posted later. Work begun before the place was removed is never posted, even after it is connected again.
- Every call that sends or changes something takes `account_id` (the helpdesk address) and, for words, a
  `request_id` you keep for that exact message. Repeating it returns the recorded outcome and never sends twice.
- `uncertain` means Freshdesk may have applied the call but did not answer. Read the ticket before anything else;
  that `request_id` never sends again. A single entry found by readback with the same author, kind, visibility,
  attachments and words counts as delivered; several candidates are listed for you to judge.
- A Silent or Paused place holds every call aimed at it; nothing is sent and the request id stays free. The
  place is checked just before the write, after any reads the call needs.
- Names work where Freshdesk uses numbers: statuses (custom ones too), priorities, groups, agents, types and custom
  fields, including the lower levels of a dependent field; `freshdesk_fields` lists them. Closing a ticket checks
  the fields Freshdesk requires first.
- `freshdesk_get` and `freshdesk_request` reach the rest of the API (contacts, companies, time entries,
  satisfaction ratings, merges, deletions, article drafts). `freshdesk_request` refuses the calls a typed tool
  here sends (replies, notes, forwards, outbound email, note edits) and names that tool. Other endpoints that
  carry words, such as forum posts or messaging-channel messages, have no typed tool here: they go out through
  `freshdesk_request` as writes, and the place's state holds them like any write. A created ticket may trigger
  the helpdesk's own notification emails.
- Canned responses, the groups list and other agents' details are open only to an administrator's key; groups and
  agents still have names through `freshdesk_fields`.

## Limits

Search returns at most 300 tickets, lags a few minutes behind changes, leaves out archived tickets and cannot
search subject or description text; listing by requester or `updated_since` covers what search cannot. The
account's API limit per minute (from 100 on Growth) is shared with the helpdesk's other integrations; the
watcher spends at most the configured share of it (25 % by default). Not covered in this version: Freshservice,
Freshchat beyond chat-sourced tickets, webhooks, several helpdesks per installation, ticket summaries, and wakes
for property changes other than assignment. For occasional use without a place, Freshdesk's own MCP server
(`https://<helpdesk>.freshdesk.com/mcp`) can be connected in Ouroboros's MCP settings; it counts every action
against a small allowance and cannot hear new tickets.
