# Phone line (`telephony`)

Phone calls for Ouroboros. The skill registers a SIP account, places and answers calls, and joins
each call to the host's Live Conversation as a meeting peer. Listening, barge-in, memory and tools
are the host's own call on the voice engine the owner chose (Gemini Live, Grok or OpenAI Realtime,
with that engine's key). The skill is pure standard-library Python: no packages, no native code.

```
 phone network ── SIP provider ── (SIP/RTP, G.711) ── phone_line companion ── (POST /call/open,
                                                      ├ provider adapter       WS /call/{id}/peer)
 agent tools ─ control channel (loopback, token) ──── ├ call manager + limits ── Host Service ── Live Conversation
                                                      └ bridge: 8 kHz <-> 24 kHz FIR, jitter buffer, 20 ms clock
```

## Two ways a call meets the conversation

| | Owner's call (mode S) | Phone place (mode A) |
|---|---|---|
| Started by | `phone_call` from the owner's own live call | an incoming call, or `phone_call` from any other work |
| Host door | `POST /call/open` with the call's `call_id` (needs `call_peer`) | `POST /call/open` with an event (needs `presence` + `call_peer`, the place Active) |
| Who hears whom | the person called hears Ouroboros; the owner hears Ouroboros, not the person | caller and Ouroboros |
| Owner during the call | types into the room; typed messages reach the call | sees the call as running work (Stop works); the place room keeps the transcript |
| Line before the answer | joins the call as it dials, silent both ways until the answer: what Ouroboros says while it rings is for the owner only; the owner ending the call cancels the dial | admitted first; holds Ouroboros's words until someone answers |

A caller's number is reported by the network and is never treated as the owner.

## Requirements on the host

* Ouroboros with **Live Conversation** (Calls beta on) and one realtime voice skill with its key:
  `gemini_live` (`GEMINI_API_KEY`), `grok_realtime` (`XAI_API_KEY`) or `openai_realtime`
  (`OPENAI_API_KEY`).
* Host Service companion call doors: `POST /call/open`, `WS /call/{call_id}/peer`, live call
  protocol 1 (16-byte `OC` frame header). These exist in Ouroboros 7.10.
* PluginAPI 2.3 or newer. The companion call doors and the `call_peer` permission arrived with
  Ouroboros 7.10.0, whose PluginAPI is 2.3, so the skill declares `"2.3"` and an older host refuses it
  at once instead of failing on its first call. Its registrations (tools, routes, the settings
  section, the companion, a supervised task) are PluginAPI 2.0 methods; on a 2.4 host it also calls
  `refresh_companion_process` (2.4), so the line picks up a newly saved SIP password by itself.
* For mode A: Presence places, consciousness on and not Advises, the **Phone** place connected
  (Circles → Connect → Phone → Whole account) and **Active**, with the `direct_message` wake.

### Running in an Ouroboros fork on 7.10

The skill needs nothing outside the public contracts above. A fork must keep, unchanged in protocol:
`ouroboros/live_conversation.py`, `skills/live_conversation`, `ouroboros/gateway/host_call.py`,
`ouroboros/gateway/call_ws.py`, `ouroboros/call_peers.py`, `ouroboros/presence_call.py`, and at
least one realtime voice skill with the user's key. The main text model of the fork can be anything:
calls use the selected realtime engine. Add this skill through a skill source (Hub or a git/local
source), review and enable it as usual. If the fork pins trusted extensions by content hash in a
deployment policy, pin this release. Not verified against any particular fork.

## Setup

1. Turn on Calls (beta) and a voice engine with its key.
2. Install and enable the skill, then open **Settings → Skills → Phone line**. Fill in the provider
   profile, registrar, user names, allowed prefixes and the incoming policy; save. Values that would
   break SIP headers or sockets are refused at save. The line restarts with new settings between
   calls; if it cannot start (DNS, network, port), it keeps retrying with growing pauses.
3. Ask Ouroboros to request the SIP password (`showSecretHitl('telephony', ['sipPassword'])`), save
   it in the card and grant `TELEPHONY_SIP_PASSWORD` to this skill. On PluginAPI 2.4 hosts the line
   picks the new password up by itself within about 45 s (or as soon as the status card or settings
   are used), never when a call is active or its state is unknown (the host compares the companion's
   environment with Settings; the skill never reads the password). On older hosts restart the skill.
4. `phone_status` should report `registration: registered`.

## Providers

### Generic SIP (cloud PBX, office PBX, SIP account)

Use the values the provider gives for a "SIP phone" or "softphone": registrar host and port (UDP),
SIP login, password, and the authentication login if it differs. Domain only if the AOR domain
differs from the registrar host; outbound proxy only if the provider names one. Codecs are PCMU and
PCMA; DTMF is RFC 4733 (`telephone-event`) with SIP INFO as a fallback.

A trunk that authenticates by IP instead of registration: turn **Register** off and list the
trunk's signalling addresses under **Extra hosts allowed to send calls**; outgoing calls go to the
registrar host.

**Who may send SIP to the line.** Every DNS address of the registrar and of the outbound proxy is
trusted, re-read at each registration (every 5 minutes without registration). If the provider sends
calls from other addresses, list them under **Extra hosts allowed to send calls**: host names, IPv4
addresses or IPv4 CIDR ranges such as `203.0.113.0/28`. The form refuses IPv6 (the line speaks IPv4
UDP) and any range wider than `/8`, so `0.0.0.0/0` or `128.0.0.0/1` can never open the line to a
large part of the internet. Requests from anywhere else, OPTIONS included, get no reply;
`phone_status` counts them as `dropped_untrusted`, so an incoming call that never rings while that
counter grows means a missing address here.

### Twilio SIP Domain (registration, no public address needed)

1. Twilio Console → Voice → SIP Domains: create a domain (`acme.sip.twilio.com`), enable
   **SIP Registration**, create a Credential List with a user (`agent` and a password) and map it
   for registration and for authenticating calls.
2. Profile **Twilio SIP Domain**, registrar `acme.sip.twilio.com` (or an edge host such as
   `acme.sip.sydney.twilio.com`), username `agent`. With an edge host, put `acme.sip.twilio.com`
   into **SIP domain**: the line's account (and so the Phone place's identity) then stays the same
   whichever edge it registers through. Twilio accepts registration intervals of 600 to 3600 s; the
   skill keeps it in that range and refreshes at half the interval.
3. Incoming: a TwiML Bin on the Twilio number. `answerOnBridge="true"` keeps the caller ringing
   until the line answers, so a call the Phone place declines is never answered by Twilio:
   ```xml
   <Response><Dial answerOnBridge="true"><Sip>agent@acme.sip.twilio.com</Sip></Dial></Response>
   ```
4. Outgoing: the domain's Voice URL points to a TwiML Bin. `answerOnBridge="true"` makes Twilio
   answer the line only when the person answers (otherwise the agent talks into the ringback and busy
   or no-answer is never reported); `timeout` lets it ring as long as the skill's own ring limit
   (Twilio's default is 30 s):
   ```xml
   <Response><Dial answerOnBridge="true" timeout="45" callerId="+15551234567">{{#e164}}{{To}}{{/e164}}</Dial></Response>
   ```
   The caller ID must be a Twilio number of the account or a verified one. Numbers are dialled as
   E.164 in the Request-URI.
   Sources, read 2026-11-13: [`<Dial>`](https://www.twilio.com/docs/voice/twiml/dial) (page dated
   2026-11-05: `answerOnBridge` default false, "the inbound call [will] ring until the dialed number
   answers", for a SIP call "a 180 or 183 ... until the `<Dial>` call connects"; `timeout` default
   30 s), [`<Sip>`](https://www.twilio.com/docs/voice/twiml/sip) ("All of the existing `<Dial>`
   parameters work with the `<Sip>` noun"), [SIP registration](https://www.twilio.com/docs/voice/api/sip-registration)
   (registration 600-3600 s, the `{{#e164}}{{To}}{{/e164}}` template).
5. Signalling addresses: calls to a registered endpoint come back through the registration's edge.
   Twilio publishes signalling ranges per edge for Elastic SIP Trunking
   (https://www.twilio.com/docs/sip-trunking/ip-addresses, read 2026-11-13), but no page we found
   says those ranges apply to SIP Domain calls to registered endpoints, so the skill does not
   hard-code them. If incoming calls do not ring and `dropped_untrusted` grows, ask Twilio support or
   the Console for the signalling ranges of your edge and add them as CIDR ranges under **Extra
   hosts allowed to send calls**.
6. Trial accounts call only verified numbers. Set geographic permissions and a spending limit in
   the Console as well.

Twilio Media Streams (a provider-pushed WebSocket) is not implemented: it needs a public `wss`
endpoint. The provider seam (`phone/provider.py`) is built so that such an adapter adds a `Leg` with
its own buffering and changes nothing else.

### Cloud PBXs in Russia and elsewhere

Most cloud PBXs and SIP operators offer a SIP account for a softphone: take the server, login and
password from the PBX's device or extension settings, use the Generic SIP profile, allow `+7` (or
your country) for outgoing calls, and route the PBX number to that extension for incoming calls.
Some operators require the caller ID to be a number you own, and some insist on TLS or SRTP, which
this version does not support.

## Limits and safety (enforced by the companion, not the model)

| Limit | Default |
|---|---|
| Calls at a time | 1; a call that has just ended keeps the line until it is closed (its BYE, the agent's notice, the call peer), usually well under a second |
| Hanging up | `phone_hangup` waits up to 5 s for the other side to confirm; a BYE or CANCEL left unanswered keeps running inside the line until its SIP timer. Before any answer from the network no CANCEL may be sent (RFC 3261 §9.1): the call ends at once, and a late provisional gets its CANCEL |
| Stopping (update, restart, skill off) | within 3.5 s of the supervisor's SIGTERM (it kills after 5 s): a call on the line gets its BYE or CANCEL at once, its call peer is closed and its record written without waiting for the other side, and the BYEs, CANCELs and the unregistration still unanswered are waited for together until then; tool requests still in flight are answered (`PHONE_LINE_STOPPING`, or `PHONE_CALL_CANCELLED` for a dial cut short). That is on macOS and Linux; on Windows the supervisor ends the companion's Job at once and nothing goes out: the provider ends a call left up by its own timers, and the registration runs out |
| Call length | 600 s (at most 1800 s), notice to the agent 30 s before the line hangs up |
| Calls per day (both directions) | 20 |
| Pause between outgoing calls | 30 s |
| Ringing | 45 s both ways, counted from the start of the call, so the place's admission and the join to the live call come out of it (`limits.ring_timeout_seconds` in `config.json`, 10-120 s): an outgoing call is then cancelled, an incoming call not yet answered is declined with 480 |
| Outgoing destinations | only the allowed prefixes; empty list = no outgoing calls; numbers in international form (`+7…`), a national `8…` is refused |
| Never called | premium-rate, satellite and shared-cost ranges (`+870..874`, `+881..883`, `+888`, `+979`, `+991`, `+808`, `+1900`, `+1976`, `+7809`, `+7803`, `+449`, `+3389`, `+49900`, `+49137`, `+3989`), plus owner additions |
| No media | an answered call that receives no RTP packets from the line for 60 s is hung up (not while on hold). A party who simply stays silent still sends RTP, so that call goes on |
| Incoming calls | `off` (default), `allowlist`, `all`; the place still decides in mode A |
| Keys (DTMF) | 1-32 keys per `phone_send_dtmf` (0-9, `*`, `#`, A-D), 160 ms each with 80 ms between; RFC 4733 `telephone-event`, or SIP INFO (`application/dtmf-relay`) when the other side offers no `telephone-event` |
| Ports for a firewall | SIP: one UDP port, ephemeral unless `sip.local_port` is set in `config.json`; media (RTP): one even UDP port per call in 16000-32000. Allow UDP between them and the provider's signalling and media addresses; behind NAT the line's own packets keep them open (a keepalive every 25 s while registered) |

* `phone_call` requires `owner_approved=true`. The flag is the agent's own statement that the owner named
  or confirmed the number, and the companion cannot check it; what it checks on every dial is
  the table above: allowed prefixes, never-called ranges, the pause between calls and the daily limit.
* The SIP password lives only in the Settings secret and the companion's environment; it is never
  logged, written to state or shown in a form.
* The line binds an ephemeral UDP port on the interface that reaches the provider; requests from
  outside a call are accepted only from the registrar, proxy and trusted hosts, requests inside a
  call also from that call's remote party; the rest is dropped without a reply.
* The caller's number is the network's claim. It is kept only when it is digits, `+`, `*` or `#`
  (otherwise it reads `unknown`), and is never given a country: an international number becomes
  E.164, a national one such as `89161234567` stays as sent. The incoming allowlist compares
  literally, so list a national number the way the provider sends it. Caller ID is easily spoofed:
  the allowlist keeps casual callers out, it does not authenticate anyone.
* Allowed prefixes are country codes, not regions: `+1` also reaches the Caribbean (often
  expensive), `+7` also reaches Kazakhstan. A number that answers with a dial tone and accepts DTMF
  (a calling card, a DISA line) can forward the call anywhere, past the prefix list; set geographic
  permissions and a spending limit at the provider too.
* Audio is never recorded. `phone_status` and `calls.jsonl` in the skill state keep the caption
  transcript of recent calls.
* Agent instructions (SKILL.md and each call's event text): introduce itself as Ouroboros, an AI agent,
  and say for whom it calls; money, irreversible bookings and personal data only with the owner's
  confirmation, asked with `escalate` without waiting so the voice session keeps running.

## Audio path

The line speaks G.711 at 8 kHz in 20 ms packets: PCMU or PCMA under the payload type numbers the
two SDPs give them, dynamic ones included; packets of any other payload type are dropped. Media goes
to the address the other side's SDP names. If that address stays silent while packets come from
elsewhere (a gateway behind NAT announcing a private address), the line follows that source only when
it is the SDP's host on another port, an address the call's SIP came from, the registrar or proxy, or a
host under **Extra hosts allowed to send calls**, and only after three consecutive packets; it then
keeps that source until a new SDP names another address.

The bridge joins the hub at 24 kHz (every bundled
engine outputs 24 kHz, so the hub does not resample the agent's voice) and converts with a
Kaiser-windowed FIR low-pass (pass band 3.4 kHz, stop band 4.4 kHz, about 70 dB), polyphase, in
pure Python (about 5% of one core per call). The hub's own resampler is linear and would fold 4-12
kHz content into the band. Incoming RTP goes through a 60 ms jitter buffer with loss concealment; a
new stream (a new SSRC or clock, as when a PBX rebuilds its bridge) plays on from where the last one
ended, and late packets of the stream it replaced are dropped while it sends. A buffer that stays more
than two frames deeper than its 60 ms for a second (a sender's fast clock, a burst after a stall) is
trimmed back to it. One monotonic 20 ms clock sends RTP and feeds the hub; the ticks it owes after a stall of the
event loop are paid one by one, between reads of the packets that came meanwhile, so a stall does not
drain the jitter buffer into silence. The hub sends whole answers faster than real time; the
bridge queues them per caption item, plays one frame per tick, reports `played` milliseconds, and
drops everything at `flush`, so an interruption cuts the transcript where the line actually
stopped. Captions share the call socket with the audio but not its one-frame size bound: the Host
Service never cuts a caption, and the bridge reads each one whole, however long.

## Known limitations

* UDP only; no TCP/TLS, SRTP, DNS SRV/NAPTR or PRACK. A call forked to several phones keeps the
  first answer; any other answer gets its ACK and is hung up at once.
* Incoming packets of any length are cut or joined into 20 ms frames by their timestamps; the line
  sends 20 ms packets.
* In mode S the owner does not hear the other party (the host's hub sends peers only the engine's
  voice), and the owner's own speech counts as heard meeting audio while the line is attached, which
  is from the dial (ringing included) until the phone call ends, so approvals during such a call
  should be typed.
* In mode A the place room keeps the transcript, but the host does not stream those rows to an
  open page as they are spoken; the owner cannot listen in to a Presence call.

## Development

The skill itself needs no packages. Its tests (`tests/`, pytest, Python 3.10+) need, for development
only: `pytest`, and for the bridge tests against the host's real `LiveCallHub` and `serve_peer`
also `starlette`, `uvicorn` and `websockets`, plus an Ouroboros core checkout named by
`TELEPHONY_CORE_REPO`; without it those tests are skipped. With `TELEPHONY_REQUIRE_CORE=1`, which the
Hub's CI sets, a missing core, a core without the live call doors, or a missing starlette, uvicorn or
websockets fails the run instead. The protocol pins need a core that has `parse_presence_event` and
PluginAPI 2.4; the Hub pins one in `.github/workflows/catalog-check.yml`.

```bash
pip install pytest starlette uvicorn websockets
TELEPHONY_CORE_REPO=/path/to/ouroboros python -m pytest -q skills/telephony/tests
```

They cover G.711 against the reference, the resampler's stop band, SIP parsing, digest vectors from
RFC 2617/7616, SDP, RTP/RFC 4733, the user agent against a scripted SIP peer (timers, CANCEL, ACK,
dialogs, source filtering, 407+401), the call manager (admission races, limits, startup) and the
bridge. A local Docker stand (Asterisk, a baresip softphone and this line) that runs real SIP calls
is kept out of the payload.
