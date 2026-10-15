---
name: yandex-travel
description: "Яндекс Еда в Алматы: агент по свежему HTML выбирает адрес, ресторан и блюда, нажимает элементы и собирает корзину. Владелец входит в аккаунт; оформление не реализовано. Яндекс Такси пока без инструмента."
version: 0.2.2
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
timeout_sec: 105
permissions: [tool, net, subprocess, companion_process]
dependencies: [playwright]
companion_processes:
  - name: eats_browser
    command: [python3, scripts/eats_browser.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 3
when_to_use: "Владелец хочет, чтобы агент в видимом окне Яндекс Еды выбрал адрес, ресторан и блюда, собрал корзину и показал результат перед отдельным оформлением."
---

# Yandex Eats — agent assembles the cart

The skill keeps one persistent, visible Chrome window on `https://eda.yandex.kz/`.
The agent reads fresh HTML evidence, chooses a specific delivery address,
restaurant and dish, and uses page elements to assemble the cart. The owner
signs in on Yandex ID. Once the cart is ready, show the owner its observed
items, quantities, prices, address and delivery details. Capture
the relevant cart region and send it to the owner with `send_photo`. Wait for the owner's **«заказывай»**
before any separate checkout. This skill has no dedicated checkout/submit
tool. A generic click still has residual transaction risk, explained below.

**Status: Draft, not installed-host verified.** A separate development probe
used live Eats DOM to assemble an I’M cart in a visible window and send a
screenshot, but the skill's registered tools, companion lifecycle and
persisted profile have not been exercised under an installed Ouroboros host.
Offline tests exercise the protocol and synthetic Chromium pages. There is no
measured speed or reliability guarantee.

**Yandex Taxi is deferred.** There is no Taxi tool or route/booking implementation.
The next separate implementation would need to observe a live Taxi page with
Andrei present, prefer his existing «Мои адреса» over a second address store,
verify pickup and destination on the map (geolocation in the separate
Playwright profile timed out), and stop at a reviewable route/price before
any booking. None of those Taxi steps is certified by Eats fixtures. Do not
open his account or test the saved profile while he is away.

## Session and tools

Playwright runs in a host-supervised companion (`scripts/eats_browser.py`),
which holds one Chrome window across short-lived tool calls. Tool children
connect over `127.0.0.1` using a random per-start token in a mode 0600
endpoint file. Chrome uses the skill's own marked `chrome-profile/` under its
state directory. A symlink or non-empty unmarked directory is refused; no
personal profile is copied or read. A `companion_unavailable` result just
after enabling may mean the companion is still starting.

- `yandex_eats_open(home?)` starts or reuses the window; `home=true` loads the
  service root again. Do not reload while the owner is signing in or entering
  information. An empty service page gets up to eight 250 ms event-pump/read
  polls for delayed client rendering. `readiness=empty_after_wait` means it
  stayed empty, not that the site or cart is ready.
- `yandex_eats_observe(query?, scope?, scroll?, wait_ms?)` returns page text,
  element ids, accessible names, surrounding card text, state and location.
  `query` filters the whole page; `scope=page` reads beyond the viewport.
- `yandex_eats_act(action, observation_id, element_id|target_name, ...)`
  clicks, fills or presses a key on an element from the latest observation.
  Exact `target_name` matches may be ambiguous; choose `element_id` from the
  observed card. For clicks supply an `intent` such as `choose` or `add_item`.
  The result includes a fresh observation; use it for the next action.
  Optional `wait_ms` (0–10000, default 0) pumps browser events after one input
  before reading, for an asynchronous UI update you expect. It never retries
  the input or proves the expected update occurred: check the returned cart.
- `yandex_eats_search(query, observation_id?, element_id?, press_enter?, trigger_name?)`
  fills the selected search field. If the agent has observed the search button,
  it supplies its exact `trigger_name` with `press_enter=false`; the tool checks
  that this button is unique after filling, clicks it, and returns the results
  in one call. No button name is hardcoded in the skill. Without trigger_name,
  Enter is sent only to a structural search field. Optional `wait_ms` has the
  same bounded event-pumping meaning as on `act`, after the search inputs.
- `yandex_eats_capture(observation_id, element_id?)` writes a private PNG of
  the visible page or one selected non-actionable region from the current observation.
  Editable and role-based text fields, selects and iframes are masked. It returns a local path, **not** a sent photo;
  send the cart image with `send_photo`.
- `yandex_eats_close()` closes the window and keeps the skill profile.

An action checks the latest observation id, page navigation epoch, element
identity, disabled/covered state, link origin and sensitive-field structure
before input. A refusal with a fresh observation means choose from that new
observation. If an action reports an unknown effect, observe the page before
anything else.

### Reusable research session (not an installed-skill test)

For an owner-present development probe, the **Hub checkout's operator script**
`scripts/yandex_eats_research_session.py` replaces the task-local `eats_live.py`
and `eats_call.py` files. It is outside the skill payload and catalog: its
explicit profile/state paths are operator inputs, not installed-skill access.
From that Hub checkout, start it **once** as a
tracked service using the Python environment that has Playwright, an explicitly
chosen existing private profile and a private state directory outside Git:

```text
python scripts/yandex_eats_research_session.py --state-dir <private-session-dir> serve --profile <owner-selected-profile>
python scripts/yandex_eats_research_session.py --state-dir <same-dir> call open '{}'
python scripts/yandex_eats_research_session.py --state-dir <same-dir> call search '{"query":"..."}'
```

The first command only publishes a loopback endpoint; Chrome launches at
`open`. Later calls reuse the same visible window and profile. Stop the tracked
service at the end: its window closes, its profile remains. Choose a stable
private state directory under the local application state if the endpoint and
audit must be findable on a later task; a task-drive directory is disposable.
Start the service again on the next task rather than retaining an unowned
background process. Do not move or copy
the owner's profile into the skill's `chrome-profile/` implicitly, and do not
run a personal-account probe without the owner present. The installed skill
uses its own host-supervised companion and marked profile; this research CLI
is not an alternate installed entry point or a credential migration.

Check the chosen interpreter's Playwright import once before `serve`; reuse
that interpreter and the same state directory for subsequent `call` commands.
The CLI prints the structured result and exits nonzero for refused, unavailable,
login-required, outside/changed page, empty-after-wait, lost-session or unknown-effect results.
An input can have been performed even when the subsequent page is incomplete;
read the structured effect and current page before deciding
what to do; an unsuccessful call must not cause a blind repeat of an input.

Combine only steps whose target and outcome are mechanically determined:
`open` already observes, `act` already returns a fresh observation, and
`search` fills, optionally activates the **agent-selected** search control,
waits, and returns a new observation. Do not call `observe` again just to
repeat those results. A changed page, multiple matching fields, ambiguous
restaurant/dish, unavailable item or uncertain effect calls for a new agent
decision on the returned DOM or a screenshot. Never batch independent choices
or checkout behind one blind script.

### When the site changes

Do not preserve a restaurant/dish/button route from an old observation. If a
node is renamed, replaced, covered or the tab navigates, read the fresh state
returned with the refusal and choose again. An ambiguous search field or
missing control is a real decision point, not permission to use `.first` or
old coordinates. Use `query` to inspect a focused question, remembering that
it replaces the latest element registry; use the full returned observation
when the next choice spans multiple regions. Inspect a screenshot for an
image-only or unclear state and before handing over the cart; PNG dimensions
alone do not prove its items and total are visible.

For a demonstrated observer/controller gap, preserve the failing observation,
reproduce its structural shape with a nonsensitive intercepted fixture, repair
the smallest helper, test it, and obtain a new observation after restarting
the tracked research service. The profile persists; the old observation IDs
do not. Do not turn a script failure into success or repeat an uncertain
cart-changing action. A different script/control method remains an agent
choice based on new evidence, not a permanently encoded site workflow.

## Working loop

1. Open and observe. If `login_required`, the owner signs in in the visible
   window; observe after they finish. A Yandex ID document is never read.
2. Choose the delivery address **before** evaluating restaurant availability.
   Find the requested restaurant and dishes from fresh observations.
   Use `act` and `search` to make each choice, checking the returned
   observation before the next action. Ask the owner about ambiguity, a
   pre-existing cart, unavailable dishes or material price changes.
3. Observe the assembled cart and report address, restaurant, items,
   quantities, prices and delivery details. Capture the cart region and send
   the resulting image to the owner. Stop and await
   **«заказывай»** for a separate checkout process.

The skill has no site-wording table or button-label denylist. `intent` states
the agent's purpose; it cannot prove a click is safe. A generic click on a
misleading same-site control could trigger a transaction. The agent must
stop before any checkout, payment or final confirmation control. The lack of
a submit tool does **not** guarantee that a generic click cannot place an
order.

## Privacy and limits

- A Yandex ID page returns `login_required` with only its kind and host; its
  elements, text, title, image and URL path/query are not read. Other origins
  are likewise not read. A Yandex ID iframe is flagged without reading it.
- Field values, editable text, cookies and storage are never read. Password,
  one-time-code and structurally marked card fields are `owner_only`; filling
  or pressing them is refused. Controls inside a form containing such fields
  are not clicked. The owner enters credentials and payment details.
- Capture can be viewport- or region-scoped and masks form inputs, role-based text fields, selects and iframes. The owner has authorized
  sending the cart screenshot as it appears; do not turn masking ordinary
  payment labels into an extra approval step. Yandex ID and other-origin pages
  remain unavailable to the capture tool.
- URLs omit queries, fragments, ports and credentials. The action log records
  operation/status/effect and page kind/host, not typed text or observations.
- Live DOM and identity-host coverage are unverified. Ordinary visible page
  text may contain personal data. A misleading control can still make a
  generic click transactional, so the agent's stop before checkout matters.
