---
name: provider_keys
title: Provider keys
description: Account credit, key limits and provider-reported usage for configured provider keys.
version: 1.0.3
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.6"
permissions: [net, route, widget, read_settings]
env_from_settings:
  - OPENROUTER_API_KEY
  - OPENAI_API_KEY
  - OPENAI_BASE_URL
  - ANTHROPIC_API_KEY
  - DEEPSEEK_API_KEY
  - OPENAI_COMPATIBLE_API_KEY
  - OPENAI_COMPATIBLE_BASE_URL
  - OUROBOROS_CONNECTIONS
  - XAI_API_KEY
  - GEMINI_API_KEY
  - GOOGLE_API_KEY
when_to_use: Open Widgets to compare balances and spending limits for configured API connections.
model_experience:
  what_model_sees: A local widget for the owner. No model tools or automatic context are added.
  token_effect: Only skill catalog metadata while enabled.
ui_tab:
  tab_id: keys
  title: Provider keys
  icon: "⚿"
  span: 2
  render: {kind: module, entry: widget.js, appearance: host, start: auto, span: 2}
---
# Provider keys

Open Widgets to see provider readings. Opening the card, returning to the page,
or pressing Refresh can request free provider metadata GETs. The browser sends one POST per such action. If another tab is already refreshing,
it can read the latest local snapshot without asking the providers again. There
is no background provider polling. The host may still record extension/key-access disclosures
for these routes. GET/HEAD snapshot never starts a provider request.

The latest valid figures stay in memory while their exact binding remains
readable. The card labels age, partial refreshes, retries, and refusals of the
metadata endpoint itself. It does not inspect event logs, check inference, or
predict future spending. The key's own limit, a different effective budget
reported by the provider, and account credit are separate constraints. Provider
windows and native currencies are never added together. An organization
report shows one returned bucket, not a daily total. See Costs for Ouroboros's
own accounting.

The widget reads the following Settings through the host's grant-aware Settings API.

| Setting | Use |
| --- | --- |
| OPENROUTER_API_KEY | Read the built-in key budget, usage and account credit. |
| OPENAI_API_KEY | Read OpenAI metadata for the direct issuer, or use this key only with the active legacy compatible binding. |
| OPENAI_BASE_URL | Identify the legacy compatible endpoint and avoid sending its key to another issuer. |
| ANTHROPIC_API_KEY | Check model-list and organization usage-report access. |
| DEEPSEEK_API_KEY | A granted key triggers a free live DeepSeek balance GET; the adapter is tested offline against fixtures. |
| OPENAI_COMPATIBLE_API_KEY | Read metadata only for canonical OpenRouter or OpenAI bases. |
| OPENAI_COMPATIBLE_BASE_URL | Identify the compatible service, including a configured keyless endpoint. |
| OUROBOROS_CONNECTIONS | Resolve named connections, their issuer and shared credential uses. |
| XAI_API_KEY | Read the voice key's flags and scopes. |
| GEMINI_API_KEY | Check the Gemini voice key's model-list access. |
| GOOGLE_API_KEY | If the readable Gemini voice key is absent, check this Google API key's Gemini model-list access; it is not the voice key. |

There is no presence-only grant for built-in credentials. The unsupported list
describes capability, not which built-in keys are configured. The widget does not
request Z.ai, MiniMax, Cloud.ru or GigaChat built-in secrets. The named connections
setting is granted as a whole so built-in overrides can be checked; disabled or
unknown routes never send a request, and unsupported credentials are not retained.
The dedicated compatible base takes its paired key (or no key); it never borrows
the legacy key. Requests go to fixed provider endpoints, not to configured URLs,
and do not follow redirects. There are no disk snapshots.
