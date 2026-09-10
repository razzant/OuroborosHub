# Canonical skill API excerpts — read 2026-09-06
Source: current system repo docs/CREATING_SKILLS.md (1353 lines read in full by root), ouroboros/extension_plugin_api.py:940-978. Excerpts below are exact documentation snippets, not an invented API. Full doc cannot be copied via shell across system root; this gated file is an explicit excerpt.

## Manifest example (relevant fields)
```yaml
---
name: memory-atlas
description: Read-only core and project memory explorer
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget]
ui_tab:
  tab_id: atlas
  title: Memory Atlas
  icon: "◈"
  render:
    kind: module
    entry: widget.js
    start: manual
    height: 580
---
```
Above manifest is adapted from canonical example to selected identity. Do not include arbitrary file/read-root settings.

## Exact API documentation excerpts
```python
def register(api):
    # HTTP routes — mounted at /api/extensions/<skill>/<path>. GET/HEAD under
    # manifest, module/... and settings_section are host-owned (see "Loading
    # more than one file").
    api.register_route("search", handler=http_search, methods=("POST",))

    # Widget UI tab on the Widgets page.
    api.register_ui_tab(
        "live",
        title="Search",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [...],
        },
    )

    # Cleanup callback when the extension is unloaded / disabled.
    api.on_unload(close_pool)

    # Read-only runtime info (v5.7.0+).
    info = api.get_runtime_info()
    # {runtime_mode, app_version, data_dir, server_port, skill_dir, state_dir,
    #  execution_mode, capabilities}
```
Use render kind module as in manifest, not declarative sample. The entry function is register(api), not start. api.register_route, not register_widget. HTTP handlers receive Starlette Request and return Starlette JSONResponse/Response (actual reference plugin inspection follows separately).

## Runtime data identity (verified code)
get_runtime_info imports DATA_DIR from ouroboros.config and returns data_dir=str(DATA_DIR), or empty string on failure. It is the serving process data root, NOT task drive. Fail visibly on empty. Do not infer HOME or copy real memory into payload.

## Module contract
Entry is classic JS, no top-level import/export. Sibling reviewed JS/mjs served under /api/extensions/memory-atlas/module/ with CORS. Prefer bundled single entry for uncomplicated opaque-origin behavior. Own-prefix OuroborosWidget.fetch returns a real Response, init.signal or timeoutMs supported. No localStorage/cookies/parent DOM/eval/CDN. Register __ouroWidgetOnDispose(fn), never replace that hook; abort pending requests and remove listeners/timers. Explicit bounded height avoids vh resize feedback. No dependency required for Python beyond available Starlette host; use stdlib for reader.

## History truth
Identity new_content/old_content are FULL snapshots unless content_digested true or content fields absent. Knowledge history carries topic and must be filtered per selected topic; it is beside knowledge dir. Scratchpad journal events are activity/retired blocks, not reconstructed whole-file versions. A preview is never a full snapshot. Byte paging must not split UTF-8 codepoints or lose text; character offsets or safe complete-chunk byte cursor is acceptable if contract states it.
