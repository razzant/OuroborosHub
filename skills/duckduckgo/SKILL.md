---
name: duckduckgo
description: Web search via DuckDuckGo — free, no API key required. Returns titles, URLs, and snippets.
version: 0.1.3
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [net, tool, route, widget]
env_from_settings: []
dependencies:
  - ddgs
when_to_use: User asks to search the web, find information, look up facts, or research a topic — and no OpenAI key is available for web_search.
timeout_sec: 60
ui_tab:
  tab_id: search
  title: DuckDuckGo Search
  icon: search
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        route: search
        method: POST
        target: search_results
        fields:
          - name: query
            label: Search query
            type: text
            placeholder: "Search the web..."
            required: true
          - name: max_results
            label: Max results
            type: text
            placeholder: "5"
        submit_label: Search
      - type: json
        target: search_results
---

# DuckDuckGo Search

A zero-config web search extension using the DuckDuckGo search engine.
No API keys required — works with any Ouroboros configuration.

## Capabilities

- **Agent tool**: `ext_..._duckduckgo_search` — the LLM can call this to search the web
- **HTTP route**: `POST /api/extensions/duckduckgo/search` — JSON body `{"query": "...", "max_results": 5}`
- **Widget**: declarative form on the Widgets page for manual searches

## Dependencies

Requires the `ddgs` Python package. Ouroboros installs it automatically
from the manifest `dependencies` declaration during review/install.

## Network policy

Connects to DuckDuckGo servers only. No other external hosts are contacted.
