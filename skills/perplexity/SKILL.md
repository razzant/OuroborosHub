---
name: perplexity
description: Deep web research via OpenRouter web_search server tool. Uses any model + grounded search with citations.
version: 0.1.3
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [net, tool, route, widget, read_settings]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User asks for deep research, fact-checking with sources, or web search with LLM synthesis and citations.
timeout_sec: 120
ui_tab:
  tab_id: research
  title: Perplexity Research
  icon: search
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        route: research
        method: POST
        target: research_results
        fields:
          - name: query
            label: Research question
            type: text
            placeholder: "Ask anything — get a grounded answer with sources..."
            required: true
          - name: max_results
            label: Max search results
            type: text
            placeholder: "5"
        submit_label: Research
      - type: markdown
        target: research_results
        path: answer
      - type: json
        target: research_results
        path: citations
---

# Perplexity Research

An LLM-grounded web search extension using OpenRouter's `openrouter:web_search`
server tool. Sends your question to a model that can search the web in real-time
and returns a synthesized answer with citations.

## Requirements

- `OPENROUTER_API_KEY` configured in Settings

## How it works

1. Your query is sent to OpenRouter with the `openrouter:web_search` tool enabled
2. The model (Claude Sonnet by default) decides when to search and synthesizes results
3. You get a grounded answer plus a list of source URLs

## Capabilities

- **Agent tool**: `ext_..._perplexity_research` — deep research with citations
- **HTTP route**: `POST /api/extensions/perplexity/research` — JSON object `{"query": "...", "max_results": 5}`. An omitted, null, empty or zero result count uses 5; integers and integer form text are clamped to 1–25. Malformed JSON, non-object bodies and non-integer counts return HTTP 400 before reading the key or starting research.
- **Widget**: declarative form on the Widgets page

## Network policy

Connects to OpenRouter API (`openrouter.ai`) only. The actual web search
is performed server-side by OpenRouter's infrastructure.
