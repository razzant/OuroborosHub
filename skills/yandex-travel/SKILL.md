---
name: yandex-travel
description: "Яндекс Еда в Алматы: агент по свежему HTML выбирает адрес, ресторан и блюда, нажимает элементы и собирает корзину. Владелец входит в аккаунт; оформление не реализовано. Яндекс Такси пока без инструмента."
version: 0.2.0
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
  information.
- `yandex_eats_observe(query?, scope?, scroll?, wait_ms?)` returns page text,
  element ids, accessible names, surrounding card text, state and location.
  `query` filters the whole page; `scope=page` reads beyond the viewport.
- `yandex_eats_act(action, observation_id, element_id|target_name, ...)`
  clicks, fills or presses a key on an element from the latest observation.
  Exact `target_name` matches may be ambiguous; choose `element_id` from the
  observed card. For clicks supply an `intent` such as `choose` or `add_item`.
  The result includes a fresh observation; use it for the next action.
- `yandex_eats_search(query, observation_id?, element_id?, press_enter?, trigger_name?)`
  fills the selected search field. If the agent has observed the search button,
  it supplies its exact `trigger_name` with `press_enter=false`; the tool checks
  that this button is unique after filling, clicks it, and returns the results
  in one call. No button name is hardcoded in the skill. Without trigger_name,
  Enter is sent only to a structural search field.
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
