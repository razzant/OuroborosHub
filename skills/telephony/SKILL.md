---
name: telephony
title: Phone line
description: Phone calls through a SIP line over UDP, IPv4 and G.711 (a SIP provider or cloud PBX that supports them, or a Twilio SIP Domain). Ouroboros places and answers calls, and Live Conversation holds the conversation on the owner's voice engine (Gemini Live, Grok or OpenAI Realtime, with its key). The line enforces limits on numbers, call length and calls per day; no packages to install.
version: 0.2.0
type: extension
entry: plugin.py
plugin_api: "2.3"
runtime: python3
os: any
permissions: [tool, route, widget, presence, call_peer, companion_process, supervised_task, net]
env_from_settings: [TELEPHONY_SIP_PASSWORD]
secret_fields:
  sipPassword: TELEPHONY_SIP_PASSWORD
when_to_use: The owner asks Ouroboros to phone someone (a clinic, a restaurant, a shop, a person) or to answer calls to a phone line, and a SIP account (at a SIP provider or cloud PBX that works over UDP, IPv4 and G.711, or a Twilio SIP Domain) is set up in this skill.
model_experience:
  what_model_sees: >-
    Four tools: phone_call, phone_hangup, phone_send_dtmf, phone_status. A call from the owner's own
    live call adds the person called to that call as meeting audio; other calls, incoming or outgoing,
    run as Presence calls in the Phone place. Line events (answered, keys pressed, hung up, time
    warnings) arrive as phone line notices, never as owner words.
  token_effect: >-
    Four small schemas per round while enabled. A call runs the owner's realtime voice engine for as
    long as it lasts, billed by that engine; the line itself costs whatever the SIP provider charges.
timeout_sec: 120
companion_processes:
  - name: phone_line
    command: [python3, scripts/phone_line.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
icon: ui/icon.svg
tools:
  - name: phone_call
    effect: write
    target: number
  - name: phone_hangup
    effect: write
    target: []
  - name: phone_send_dtmf
    effect: write
    target: []
  - name: phone_status
    effect: read
    target: []
contributes:
  - id: place
    provides: place
    label: Phone
    destination: vendor:Phone
    scope: account
    wakes: [direct_message]
    source: {route: status, refresh_s: 15}
    icon: ui/icon.svg
    settings: connection
---

# Phone line

This skill gives Ouroboros a telephone line through a SIP account. The `phone_line` companion
registers with the provider, places and receives calls, and joins each call to Live Conversation as
a meeting peer, so the conversation itself (barge-in, memory, tools) is the host's own call on the
voice engine the owner selected (Gemini Live, Grok or OpenAI Realtime, each with its own key).
The skill installs no packages: standard-library Python only.

## How a call reaches the conversation

- **From the owner's live call.** When the owner is in a call with Ouroboros and asks it to phone
  someone, `phone_call` joins the line to that same call and dials. Nothing passes either way while it
  rings, and the owner ending the call cancels the dial; once the person answers, their voice arrives
  as meeting audio, not as the owner. The owner hears Ouroboros but not the other person, and can type
  into the room during the call; typed messages reach the call.
- **From other work, and every incoming call.** The call runs as a Presence call in the **Phone**
  place (Circles → Connect → Phone, whole account, then make it Active). An outgoing call asks the
  place first and dials only if the place takes it; an incoming call rings while the place decides,
  and is declined without being answered when it does not. The room of that place keeps the
  transcript. A caller's number is reported by the phone network, is not verified, and never gives
  owner authority.

## Rules for the agent on a call

1. Start by saying you are Ouroboros, an AI agent, and on whose behalf you call or answer.
2. Before dialling a number the owner did not name for this purpose, ask the owner. `phone_call`
   requires `owner_approved=true`, which means exactly that.
3. Money, irreversible bookings or cancellations, and the owner's personal data need the owner's
   confirmation of exactly that. Say you will check; ask the owner with `escalate` **without**
   `wait_for_answer`, which then needs an `assumption` (what you do meanwhile), or with
   `send_user_message`; keep the conversation going or end politely and offer to call back. The
   owner's answer reaches the running call through the room's mailbox.
4. Nothing is recorded as audio. The transcript is the call's ordinary captions.
5. Use `phone_send_dtmf` for voice menus.
6. Finishing: in the owner's own call (mode S) say goodbye and use **`phone_hangup` only**:
   `end_call` would end the owner's whole call. In a Presence call of the Phone place, `phone_hangup`
   or `end_call` after the goodbye.

## Limits the line enforces (not the model)

One call at a time; outgoing calls only to the country prefixes the owner allowed (an empty list means
none) and never to premium-rate or satellite ranges; a pause between dials; a daily number of calls; a
maximum call length (600 s by default, at most 1800 s) with a notice 30 s before the line hangs up; a
call that receives no audio packets (RTP) from the line for 60 s is hung up; incoming calls off by
default (`off`, `allowlist` or `all`). SIP requests from outside a call are accepted only from every
DNS address of the registrar and proxy and from the trusted host names, IPv4 addresses or IPv4 CIDR
ranges (IPv6 and ranges wider than /8 are refused); everything else, OPTIONS included, is dropped
without a reply. Requests inside a call must also come from those addresses or from the call's own
remote party.

## Setup

1. Turn on Calls (beta) and a voice engine with its key.
2. Settings → Skills → Phone line: choose the profile (Generic SIP or Twilio SIP Domain), the
   registrar, user names, allowed prefixes and the incoming policy; save.
3. Ask Ouroboros to request the SIP password: `showSecretHitl('telephony', ['sipPassword'])`; save it
   in the card and grant the key to this skill when asked. On PluginAPI 2.4 hosts the line picks the
   new password up by itself within about 45 s (or when the status card or settings are opened),
   between calls; on older hosts restart the skill.
4. `phone_status` reports `registration: registered`.
5. For Presence calls: consciousness on (not Advises), and the Phone place connected (Circles →
   Connect → Phone → Whole account) and Active with the `direct_message` wake.

See README.md for provider guides (generic SIP, Twilio SIP Domain with TwiML Bins, cloud PBXs) and
for what an Ouroboros fork needs.
