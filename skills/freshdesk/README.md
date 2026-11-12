# Freshdesk for Ouroboros

Connects one Freshdesk helpdesk. Without a place it gives Ouroboros eleven tools: find and read tickets and their
threads, search the knowledge base, reply to customers, write private or public notes, send emails with explicit
recipients, change ticket properties by name, and reach the rest of Freshdesk's API v2. With the Freshdesk place
connected, Ouroboros is present in the helpdesk: it hears new tickets, customer replies and teammates' notes, and
the owner decides in Circles what wakes it (new tickets, assignments, replies on tickets it wrote in, notes that
notify it, or everything).

**Status:** version 0.1.0 is tested against a stand-in of Freshdesk's API built from Freshdesk's documentation and
against a real Ouroboros core's Presence routes. It has not yet run against a live helpdesk; the first live use is
its first live check (see below).

## What the helpdesk's administrator provides

- **An agent for Ouroboros.** A dedicated agent keeps its words and changes attributed to it. A full-time seat
  works; an occasional agent needs no licence and, per Freshdesk, spends no day passes on API use once its API key
  has been retrieved ([full-time vs occasional agents](https://support.freshdesk.com/en/support/solutions/articles/37602)).
- **Its role.** An Agent can reply to customers. A Collaborator sees only tickets it is tagged on, cannot reply or
  change properties, and can write private notes
  ([collaborators](https://support.freshdesk.com/support/solutions/articles/50000003573)). A custom role without
  "Reply to ticket" makes the API refuse replies. The API acts with exactly the agent's rights.
- **Its ticket scope:** all tickets, tickets of its groups, or tickets assigned to it. This sets what Ouroboros sees.
- **Its API key:** in Freshdesk, the profile picture → Profile settings → View API key. Resetting the key revokes it.

The helpdesk must be addressed as `<name>.freshdesk.com`: Freshdesk's API does not answer on a custom domain.
Ticket text is sent to the model provider this Ouroboros installation uses.

## Setting up

1. Install the skill from the catalog (Ouroboros Community reviews it before it runs).
2. Settings → Freshdesk: save the helpdesk address. Ask Ouroboros to set up Freshdesk; it opens the protected form
   for the API key (the key never enters the chat). Grant the key to the skill when Skills asks.
3. Settings → Freshdesk → Check connection: it names the agent and its scope and shows which endpoints the key may
   use. It writes nothing.
4. To be present in the helpdesk, connect the Freshdesk place in Circles. It starts **Silent**: Ouroboros hears and
   thinks but writes nothing in Freshdesk until you make the place Active. Notes on the place guide its judgment.

## How it behaves

- Every reply, note and email is recorded under a request id before it is sent; repeating the id never sends twice.
  If Freshdesk does not answer, the call is marked uncertain and is not resent; Ouroboros reads the ticket and
  decides.
- Just before each call that sends or changes something, the skill asks Ouroboros whether the place is Silent or
  Paused; if so, nothing is sent.
- When longer work on a ticket ends with a final answer, the answer is posted on the ticket as a private note.
  Customers receive only what Ouroboros explicitly sends with the reply or email tools. An answer that finishes
  while the place is Silent or Paused stays with you in the room (Circles) as a held reply and is not posted later;
  work begun before the place was removed is never posted.
- Its own replies, notes and the tickets it opens (outbound emails) never come back to it as news; a customer's
  answer to them is a reply on a ticket it wrote in.
- The watcher checks Freshdesk once a minute (30–600 s) while the place is connected and spends at most a
  configurable share (25 % by default) of the account's per-minute API limit, which the helpdesk's other
  integrations share. It finds new entries because Freshdesk moves a ticket's `updated_at`; a change made and
  reverted between two checks is not seen. Tickets and entries from before the connection are history, and a
  place removed and connected again starts a new connection.

## First live check

On the first helpdesk this runs against, first ask Ouroboros, before connecting the place, to write one private
note and one reply on a test ticket whose requester is your own address (the tools work without a place; a Silent
place would hold them). Then connect the place, keep it Silent for a while and compare what the place shows with
the helpdesk: a new ticket, a customer reply, a private note that notifies the agent, and an assignment should each
appear. If a customer reply never appears, Freshdesk did not move the ticket's `updated_at` for it; please report
that, with the ticket's timeline, as an issue in this repository. Make the place Active when this matches.

## Tests

Unit tests need only pytest; the core-consumer test also needs a core checkout:

```bash
python -m pytest -q skills/freshdesk/tests
OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q tests/test_freshdesk_core_consumer.py
```

The stand-in Freshdesk (`tests/fake_freshdesk.py`) serves invented data on reserved example domains.

Freshdesk is a trademark of Freshworks Inc.; this skill is not affiliated with or endorsed by Freshworks. The icon
is original geometry.
