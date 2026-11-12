# Smart Lists 0.6.0 widget API (implementation seam)

Base: `/api/extensions/smart_lists/`. Legacy routes remain.

`GET board?tag=one&tag=two&query=...&turn=owner&offset_todo=0&offset_in_progress=0&offset_review=0&offset_done=0&limit=100` uses AND for repeated tags. Result: `{ok:true,revision:{store_id,generation},stages:{todo:{entries,total,next_offset},in_progress:{entries,total,next_offset},review:{entries,total,next_offset},done:{entries,total,next_offset}},available_tags:[string],selected_tags:[string]}`. All four counts are computed before independent paging. Each entry summary has `{id,text,stage,status,description_preview,group_id,group_path,due,details,created_at,updated_at,completed_at,archived_at}`; `details` has tags, turn and reference count. Board excludes archived and deleted entries. Tags are explicit only.

`GET archive?tag=...&query=...&turn=...&offset=0&limit=100` returns `{ok:true,revision,entries:[summary],total,next_offset,available_tags}` and excludes deleted entries.

`GET entry?entry_id=e_...` returns `{ok:true,revision,entry:{id,text,description,stage,status,group_id,group_path,due,source,created_at,updated_at,completed_at,archived_at,deleted_at,details}}`. This is complete and never shortened.

`POST edit` body: `{entry_id,changes:{text?,description?,stage?,due?,details?:{tags?,turn?,next_step?,note?,references?}},expected:{matching fields},request_id}`. `due` is raw text or null. Every changed leaf must have its prior value in `expected`, including null for an absent value. Response: `{ok:true,entry:<complete entry>,changed:[field names],replayed:bool}`. Overlapping writer: `{ok:false,code:"field_conflict",error:string,conflicts:[field names]}` and no write. An unrelated concurrent field write survives. Successful identical replay is checked before the expected values. Legacy flat `POST edit` remains supported.

`POST archive` body: `{entry_id,archived:true|false,expected:{archived_at:null|string},request_id}`. Response: `{ok:true,entry:<complete entry>,replayed:bool}`. A conflict uses the same typed error. Archive retains stage and content.

`POST check` remains the legacy `done` toggle: reopening Done yields To do, but `done:false` on an already-open stage preserves it. Legacy flat edit with `status:open` likewise preserves In progress/Review. Edit/archive retries return the same normalized full-card shape with current values. Legacy `select`, `view` and `today` exclude explicitly archived entries before counts/pagination; rows carry `stage` and `archived_at`. Explicit archive reads remain available. Agent `read` remains a paginated summary. The agent `entry` tool returns an exact `overview` with identity, stage, timestamps, counts and lengths, plus lossless pages of `text`, `due` (raw; parsed instant is `overview.due_at`), `tags`, `description`, `references`, `group_path`, `note`, `next_step`, and `capture_key`. String fields page by 500 characters; array fields by one item. Every page includes `revision:{store_id,generation}`. Follow `next_offset` to `total` and restart if the revision changes during the read; no page is silently shortened.

Storage: description ≤16,000 characters; references ≤100, with each label ≤120 and target ≤1,000; all details together ≤128,000 JSON characters. Writes stop before the store exceeds 128 MiB; export restore accepts up to 256 MiB. No references are silently dropped.
