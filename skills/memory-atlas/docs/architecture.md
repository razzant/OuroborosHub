# Memory Atlas minimal architecture

`SKILL.md` is the PluginAPI 2.0 extension manifest. `plugin.py` exports
`register(api)`, obtains the canonical runtime `data_dir`, registers seven named GET
routes with `register_route` (`catalog`, `document`, `history`, `history/event`,
`search`, `graph`, and `dialogue`), and registers the `atlas` module tab with
`register_ui_tab`. Async handlers consume Starlette `Request.query_params`, return
`JSONResponse`, disable HTTP caching, and close the retained reader on unload.

`memory_reader.py` is the single read boundary. It enumerates only fixed source
families with one request-wide entry budget, retains a read-only root descriptor,
opens every component with no-follow semantics, and compares identity/stat state
around each bounded read. Source IDs never become filesystem paths. History parsers
understand only authentic identity, knowledge, scratchpad, and project schemas and
keep whole snapshots distinct from digests, previews, blocks, and activity.

The backend has no database, index, watcher, cache, background task, network call,
LLM call, or write path. Literal search is locally bounded and maps folded matches
back to original character positions. The graph starts from one focus and reports
only four kinds of provable relationship: `markdown_link`, `wiki_link`,
`journal_source_ref`, and `shared_task_id`. It makes no model call and performs no
similarity or shared-term inference. The separately owned classic-module `widget.js`
consumes this schema and is responsible for safe text rendering and disposal.
