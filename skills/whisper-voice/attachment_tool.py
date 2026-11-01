"""Narrow host-staged attachment reader; no arbitrary path or second model owner.

Requires Ouroboros's existing resource binding/media admission helpers. Failures
stay typed; audio remains local and is never included in the result or logs.
"""
from __future__ import annotations

import hashlib
import json

from .whisper_runtime import MAX_AUDIO_BYTES, audio_format, error


def transcribe_attachment(ctx, ordinal, *, state_dir, worker):
    try:
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("ordinal must be a nonnegative attachment number")
        manifest = (getattr(ctx, "task_contract", None) or {}).get("attachment_manifest", [])
        matches = [entry for entry in manifest if entry.get("ordinal") == ordinal]
        if len(matches) != 1:
            raise ValueError("attachment number is missing or ambiguous")
        entry = matches[0]
        if entry.get("status") != "staged" or entry.get("root") != "artifact_store":
            raise ValueError("only host-staged artifact attachments are accepted")
        path = entry.get("relpath")
        if not isinstance(path, str) or not path:
            raise ValueError("attachment has no retained path")
        # Host owns task/resource restrictions, including protected artifacts.
        from ouroboros.tool_access import build_resolved_resource_binding
        from ouroboros.tools.media import _resolve_local_file
        binding = build_resolved_resource_binding(ctx, root="artifact_store", path=path, operation="read")
        admitted, reason = _resolve_local_file(ctx, str(binding.target_path), max_bytes=MAX_AUDIO_BYTES)
        if admitted is None:
            return json.dumps({"ok": False, **error("attachment_blocked", reason)}, ensure_ascii=False)
        with admitted.open("rb") as stream:
            data = stream.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise ValueError("attachment exceeds 393216 bytes")
        if entry.get("sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("attachment bytes no longer match host manifest")
        mime = entry.get("mime", "")
        audio_format(mime, data)
        response = worker(state_dir, "POST", "/transcribe", data, mime)
        value = json.loads(response.body)
        return json.dumps({"ok": response.status_code < 400, **value}, ensure_ascii=False)
    except Exception as exc:
        # Denial/import/IO failure never falls back to unguarded file access.
        return json.dumps({"ok": False, **error("attachment_unavailable", f"{type(exc).__name__}: {exc}")}, ensure_ascii=False)
