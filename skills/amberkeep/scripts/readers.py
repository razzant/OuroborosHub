"""Offline export readers for Amberkeep Community (stdlib-only, pure bytes-in).

``read_source`` reads plain JSON/JSONL bytes, and — for the two vendors whose
official exports ship as an archive — a raw ChatGPT or Claude export ZIP: only
its one main ``conversations.json`` member is read, in memory, and nothing is
extracted. One read is bounded: input larger than ``_MAX_EXPORT_BYTES``
uncompressed bytes is refused before it is read, with a way to hand over a
smaller file. Pasted literal material (``text``, ``summary``) is carried
verbatim: no content filter, so a JSON-shaped summary, or one quoting harness
markup, stays exactly what the person supplied.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import io
import json
import math
import re
from typing import Any, Callable
from urllib.parse import quote
import zipfile
import zlib

_KINDS = ("chatgpt", "claude", "claude-code", "codex", "hermes", "text", "summary")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ZIP_KINDS = ("chatgpt", "claude")
_CONVERSATIONS_MEMBER = "conversations.json"
# One read is bounded: input larger than this many uncompressed bytes is refused
# before it is read — the same 32 MiB one member may occupy in a Portable
# package (portable.py's MAX_FILE_BYTES). The refusal names the limit and says
# how to hand over a smaller file.
_MAX_EXPORT_BYTES = 32 * 1024 * 1024
_SPEAKERS, _CLAUDE_ROOT = frozenset({"user", "assistant", "document", "unknown"}), "00000000-0000-4000-8000-000000000000"
_HERMES_DOCS, _HERMES_DENY = {"memory.md": "MEMORY.md", "user.md": "USER.md"}, frozenset({"state.db", "config.yaml", "config.yml", "soul.md", "profile.json", "profile.yaml", ".env"})
_CG_TOOL_CTS = frozenset({"code", "execution_output", "tether_browsing_display", "tether_quote", "system_error"})
_CC_TYPES = frozenset({"user", "assistant", "summary", "system", "file-history-snapshot", "queue-operation", "progress", "custom-title", "last-prompt"})
_CODEX_ENV = frozenset({"session_meta", "turn_context", "response_item", "event_msg", "compacted"})
_CODEX_ITEMS = frozenset({"message", "reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "local_shell_call", "local_shell_call_output", "web_search_call"})
_CODEX_PARTS = frozenset({"input_text", "output_text", "input_image", "input_file", "input_audio", "refusal"})
_TEXT_ONLY, _TEXT_IO = frozenset({"text"}), frozenset({"text", "input_text", "output_text"})
_REASONING_PARTS = frozenset({"thinking", "redacted_thinking", "reasoning", "reasoning_text", "summary_text", "analysis", "thought", "thoughts"})
_ANALYSIS_TAGS = frozenset({"analysis", "reasoning", "thinking", "thought", "thoughts", "cot", "inner_monologue", "reasoning_recap"})
_TOOL_PARTS = frozenset({"tool_use", "tool_result", "server_tool_use", "web_search_tool_result", "mcp_tool_use", "mcp_tool_result", "function_call", "function_call_output"})
_CC_TAGS = "system-reminder|local-command-caveat|local-command-stdout|command-name|command-message|command-args"
_CC_WRAPPER_RE = re.compile(rf"\s*<({_CC_TAGS})\b[^>]*>(?:(?!</\1>).)*</\1>\s*", re.DOTALL)
_CC_LINE_REM_RE = re.compile(rf"(?:^|\r?\n)[ \t]*<({_CC_TAGS})\b[^>]*>(?:(?!</\1>).)*</\1>[ \t]*(?=\r?\n|\Z)", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\r\n]+`", re.DOTALL)
_CODEX_CTX_RE = re.compile(r"\A\s*<(environment_context|permissions instructions|user_instructions|collaboration_mode|subagent_notification|turn_aborted)\b[^>]*>(?:(?!</\1>).)*</\1>\s*\Z", re.DOTALL)


class Gaps(list):
    """Diagnostic lines returned by :func:`read_source` — a plain ``list``.

    Only lines about content that was NOT carried into ``rows``: omitted
    reasoning, unread archive members, skipped records. Non-loss notices —
    bookkeeping about rows that ARE fully kept, such as duplicate locators
    disambiguated with an ``@N`` suffix or mirrored duplicate records
    deduplicated against the first-seen record of the same speech — never
    enter the list: they ride
    the ``notes`` attribute, so a caller can tell "something was skipped"
    from "nothing was lost" without parsing line text.
    """

    notes: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.notes = []


def read_source(kind: str, data: bytes, label: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse raw UTF-8 export bytes of ``kind`` into ``(rows, gaps)``.

    A ChatGPT or Claude official export ZIP is accepted as raw bytes too: its
    one main ``conversations.json`` member is read in memory, other members
    are not read at all, and nothing is extracted. An export larger than
    ``_MAX_EXPORT_BYTES`` uncompressed bytes is refused before it is read.

    ``gaps`` is a :class:`Gaps` list: only lines about content NOT carried
    into ``rows``; non-loss notices about kept rows ride ``gaps.notes``.
    """
    if not isinstance(kind, str) or not isinstance(label, str) or not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("kind/label must be str and data must be bytes")
    if kind not in _KINDS:
        raise ValueError(f"Unsupported source kind {kind!r}; expected one of {', '.join(_KINDS)}")
    raw = bytes(data)
    if not raw:
        raise ValueError("Empty input is not accepted by read_source")
    st = label.strip()
    trimmed = st.rstrip("/\\")
    base = re.split(r"[\\/]+", trimmed)[-1] if trimmed else ""
    safe = (base or kind) if (not st or "/" in st or "\\" in st or st.startswith("~")) else st
    readers = {"chatgpt": _read_chatgpt, "claude": _read_claude, "claude-code": _read_claude_code, "codex": _read_codex, "hermes": _read_hermes}
    rows: list[dict[str, Any]] = []
    gaps = Gaps()
    notes: list[str] = []
    if raw.startswith(_ZIP_MAGIC):
        chosen, skipped, others = _export_zip_members(kind, raw)
        if skipped:
            gaps.append(f"{kind}: read {chosen[0]!r}; {len(skipped)} nested {_CONVERSATIONS_MEMBER} "
                        f"member(s) beside it were not read: {', '.join(skipped)}")
        if others:
            gaps.append(f"{kind}: {others} other archive member(s) were not read; only the main "
                        f"{_CONVERSATIONS_MEMBER} is read, and nothing is extracted")
        member_rows, member_gaps, member_notes = readers[kind](chosen[1], safe)
        rows.extend(member_rows)
        gaps.extend(member_gaps)
        notes.extend(member_notes)
    else:
        if len(raw) > _MAX_EXPORT_BYTES:
            raise ValueError(f"{kind}: this export is {len(raw)} bytes, over the "
                             f"{_MAX_EXPORT_BYTES}-byte limit for one read; pass a smaller file — "
                             "for example, only the conversations you need")
        if raw.startswith((b"\x1f\x8b", b"SQLite format 3\x00", b"%PDF-")) or b"\x00" in raw:
            raise ValueError(f"Binary or compressed input is not supported for {kind!r}")
        try:
            decoded = raw.decode("utf-8").removeprefix("\ufeff")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Input for {kind!r} is not valid UTF-8") from exc
        if not decoded.strip():
            raise ValueError(f"Whitespace-only input for {kind!r}")
        if kind == "text":
            return [_row("text", safe, safe, "doc", 0, "document", decoded, None)], Gaps()
        if kind == "summary":
            # Carried verbatim, JSON-shaped or not: a pasted summary may itself be
            # JSON or quote harness markup, and a content filter would lose it.
            return [_row("summary", safe, safe, "doc", 0, "assistant", decoded, None)], Gaps()
        member_rows, member_gaps, member_notes = readers[kind](decoded, base if kind == "hermes" else safe)
        rows.extend(member_rows)
        gaps.extend(member_gaps)
        notes.extend(member_notes)
    seen: Counter[str] = Counter()
    for r in rows:
        loc = r["locator"]
        seen[loc] += 1
        if seen[loc] > 1:
            r["locator"] = f"{loc}@{seen[loc]}"
    if dups := sum(n - 1 for n in seen.values() if n > 1):
        notes.append(f"{kind}: disambiguated {dups} duplicate locator(s) (every row kept; only @N suffixes added)")
    gaps.notes.extend(notes)
    return rows, gaps


_ZIP_CHUNK = 1024 * 1024


def _export_zip_members(kind: str, raw: bytes) -> tuple[tuple[str, str], list[str], int]:
    """The one main conversations member of an official export ZIP, decoded in memory.

    The main member is the shallowest ``conversations.json`` (case-insensitive,
    at any depth) — the one the ordinary ChatGPT and Claude exports put at the
    archive root. Nested members of the same name beside it — an attachment's
    folder, for instance — are strangers and are not read. When several
    candidates sit at the same depth (including duplicate entries of the same
    name) and none is distinguishable as the main one, none is read: the
    refusal lists them for an explicit choice. Encryption and alternative
    compression methods are refused without password prompts, and the chosen
    member's uncompressed size is bounded both from the directory metadata and
    while streaming the actual bytes. Returns ``(name, text)``, the
    conversation members passed over, and the count of other members left
    unread.
    """
    if kind not in _ZIP_KINDS:
        raise ValueError(f"ZIP archives are read only for kinds {', '.join(_ZIP_KINDS)}; unpack the archive and "
                         f"pass its file for kind {kind!r}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{kind}: input starts like a ZIP archive but is not a readable one: {exc}") from exc
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        candidates = sorted(
            [info for info in entries if info.filename.rsplit("/", 1)[-1].lower() == _CONVERSATIONS_MEMBER],
            key=lambda info: (info.filename.count("/"), info.filename),
        )
        if not candidates:
            raise ValueError(f"{kind}: the archive holds no {_CONVERSATIONS_MEMBER} member; only that recognized "
                             "export member is read — unpack the archive and pass its JSON, or share the export ZIP")
        min_depth = candidates[0].filename.count("/")
        mains = [info for info in candidates if info.filename.count("/") == min_depth]
        if len(mains) > 1:
            raise ValueError(f"{kind}: the archive holds {len(mains)} {_CONVERSATIONS_MEMBER} member(s) at "
                             f"the same depth, and none was read; pass the one you mean: "
                             f"{', '.join(info.filename for info in mains)}")
        info = mains[0]
        name = info.filename
        if info.flag_bits & 0x1:
            raise ValueError(f"{kind}: {name!r} is encrypted; encrypted archives are not supported")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError(f"{kind}: {name!r} uses unsupported compression method {info.compress_type}")
        if info.file_size > _MAX_EXPORT_BYTES:
            raise ValueError(f"{kind}: {name!r} unpacks to {info.file_size} bytes, over the "
                             f"{_MAX_EXPORT_BYTES}-byte limit for one read; unpack the archive and pass "
                             "the smaller conversations you mean")
        chunks: list[bytes] = []
        total_read = 0
        try:
            with archive.open(info) as handle:
                while True:
                    chunk = handle.read(min(_ZIP_CHUNK, max(1, _MAX_EXPORT_BYTES - total_read + 1)))
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if total_read > _MAX_EXPORT_BYTES:
                        raise ValueError(
                            f"{kind}: {name!r} unpacks to more than {_MAX_EXPORT_BYTES} bytes, over the "
                            f"{_MAX_EXPORT_BYTES}-byte limit for one read; unpack the archive and pass "
                            "the smaller conversations you mean"
                        )
                    chunks.append(chunk)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
            raise ValueError(f"{kind}: {name!r} could not be read from the archive: {exc}") from exc
        payload = b"".join(chunks)
        try:
            text = payload.decode("utf-8").removeprefix("\ufeff")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{kind}: {name!r} in the archive is not valid UTF-8") from exc
        if not text.strip():
            raise ValueError(f"{kind}: {name!r} in the archive is empty")
    return (name, text), [item.filename for item in candidates if item is not info], len(entries) - len(candidates)


def _s(val: Any) -> str | None:
    return val.strip() or None if isinstance(val, str) else None


def _flag(val: Any) -> bool:
    return val is True or (isinstance(val, int) and val == 1)


def _is_analysis_marked(d: dict[str, Any]) -> bool:
    for src in (d, d["metadata"]) if isinstance(d.get("metadata"), dict) else (d,):
        for key in ("channel", "phase", "kind", "subtype"):
            val = _s(src.get(key))
            if val and val.lower() in _ANALYSIS_TAGS:
                return True
        if any(src.get(k) is True for k in ("thought", "is_thought", "is_reasoning", "is_analysis")):
            return True
    return False


def _id_str(val: Any, fallback: str) -> str:
    return str(val) if isinstance(val, int) and not isinstance(val, bool) else (_s(val) or fallback)


def _try_json(txt: str) -> Any:
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return None


def _format_date(val: Any) -> str | None:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(val)
    return _s(val)


def _row(kind: str, conv: str, loc_conv: str, item_id: Any, part: int, speaker: str, text: str, date: str | None) -> dict[str, Any]:
    loc = f"{kind}:{quote(str(loc_conv), safe='-_.')}:{quote(str(item_id), safe='-_.')}/p{part}"
    return {"locator": loc, "speaker": speaker if speaker in _SPEAKERS else "unknown", "text": text, "date": date, "conversation": conv}


def _flush_gaps(omitted: Counter[str], scope: str, gaps: list[str]) -> None:
    gaps.extend(f"{scope}: omitted {n} {cat}" for cat, n in omitted.items() if n > 0)


def _typed_parts(content: Any, text_types: frozenset[str], omitted: Counter[str]) -> list[tuple[int, str]]:
    if isinstance(content, str):
        return [(0, content)] if content.strip() else []
    parts: list[tuple[int, str]] = []
    for idx, blk in enumerate(content if isinstance(content, list) else ()):
        if isinstance(blk, str):
            if blk.strip():
                parts.append((idx, blk))
        elif isinstance(blk, dict):
            bt, txt = _s(blk.get("type")) or "", blk.get("text")
            if _is_analysis_marked(blk) or bt in _REASONING_PARTS:
                omitted["reasoning block(s)"] += 1
            elif bt in text_types and isinstance(txt, str):
                if txt.strip():
                    parts.append((idx, txt))
            elif bt not in text_types and bt != "token_budget":
                omitted["tool block(s)" if bt in _TOOL_PARTS else "non-text part(s)"] += 1
        else:
            omitted["non-text part(s)"] += 1
    return parts


def _is_json_or_jsonl(decoded: str) -> bool:
    s = decoded.strip()
    if s.startswith(("{", "[")) and isinstance(_try_json(s), (dict, list)):
        return True
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
    return bool(lines) and all(ln.startswith("{") and isinstance(_try_json(ln), dict) for ln in lines)


def _parse_jsonl(decoded: str, kind: str, is_valid: Callable[[dict[str, Any]], bool], is_foreign: Callable[[dict[str, Any]], bool] | None = None) -> tuple[list[tuple[int, dict[str, Any]]], set[int], list[int]]:
    if decoded.lstrip().startswith("["):
        raise ValueError(f"Expected newline-delimited JSONL for {kind!r}, got JSON array")
    records, bad = [], []
    for ln, raw_line in enumerate(decoded.split("\n"), start=1):
        if (line := raw_line.removesuffix("\r")).strip():
            records.append((ln, obj)) if isinstance(obj := _try_json(line), dict) else bad.append(ln)
    if not records or (is_foreign and any(is_foreign(o) for _, o in records)):
        raise ValueError(f"Invalid or foreign JSONL input for {kind!r}")
    if not (valid_lines := {ln for ln, o in records if is_valid(o)}):
        raise ValueError(f"Input does not match {kind} JSONL schema")
    return records, valid_lines, bad


def _load_conv_list(decoded: str, kind: str, key: str, key_type: type, foreign_key: str) -> list[Any]:
    if (p := _try_json(decoded)) is None:
        raise ValueError(f"Invalid JSON for {kind} conversations.json")
    items = p if isinstance(p, list) else ([p] if isinstance(p, dict) and isinstance(p.get(key), key_type) else (p.get("conversations") if isinstance(p, dict) else None))
    if not isinstance(items, list):
        raise ValueError(f"Unsupported {kind} JSON root; expected conversations with {key!r}")
    if items and any(isinstance(it, dict) and foreign_key in it and key not in it for it in items):
        raise ValueError(f"Input matches {foreign_key!r} export rather than {kind}")
    if items and not any(isinstance(it, dict) and isinstance(it.get(key), key_type) for it in items):
        raise ValueError(f"Input does not match {kind} schema (missing {key!r})")
    return items


def _resolve_chain(order: list[str], parents: dict[str, str | None], vis: set[str], current: str | None, require_current: bool, scope: str, gaps: list[str]) -> list[str]:
    node_set = set(order)
    if current and current in node_set:
        chain, seen, cur = [], set(), current
        while cur is not None and cur not in seen and cur in node_set:
            seen.add(cur)
            chain.append(cur)
            cur = parents.get(cur)
        if cur is not None:
            gaps.append(f"{scope}: {'cycle' if cur in seen else 'broken parent link'} in tree")
        if off := len(vis - seen):
            gaps.append(f"{scope}: omitted {off} message(s) on non-current branch(es)")
        return chain[::-1]
    if require_current or current is not None:
        gaps.append(f"{scope}: missing or invalid current_node")
    if not vis:
        return order if len(order) <= 1 else []
    children: dict[str, list[str]] = {nid: [] for nid in order}
    roots: list[str] = []
    reached: set[str] = set()
    for start in order:
        if start not in vis or start in reached:
            continue
        path: set[str] = set()
        cur = start
        while cur not in reached and cur not in path:
            path.add(cur)
            p = parents.get(cur)
            if p not in node_set:
                roots.append(cur)
                break
            children[p].append(cur)
            cur = p
        reached.update(path)
    if len(roots) != 1:
        gaps.append(f"{scope}: omitted {len(vis)} message(s) across ambiguous root(s) without current_node")
        return []
    chain, seen, cur = [], set(), roots[0]
    while cur is not None and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        nxt = [c for c in children[cur] if c not in seen]
        cur = nxt[0] if len(nxt) == 1 else None
    if ambig := len(vis - seen):
        gaps.append(f"{scope}: omitted {ambig} message(s) on ambiguous branch(es) without current_node")
    return chain


def _chatgpt_node(node_obj: Any) -> tuple[str, list[tuple[int, str]], int]:
    msg = node_obj.get("message") if isinstance(node_obj, dict) else None
    if not isinstance(msg, dict):
        return ("empty" if msg is None else "malformed message node(s)"), [], 0
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    cnt = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    ct, role = _s(cnt.get("content_type")) or "", _s((msg.get("author") or {}).get("role")) if isinstance(msg.get("author"), dict) else ""
    if ct in {"thoughts", "reasoning_recap"} or (_s(msg.get("channel")) or "final") != "final":
        return "reasoning/analysis node(s)", [], 0
    if ct in {"user_editable_context", "model_editable_context"} or meta.get("is_user_system_message") is True:
        return "context metadata node(s)", [], 0
    if meta.get("is_visually_hidden_from_conversation") is True or role in {"system", "developer"}:
        return "empty", [], 0
    if role == "tool" or (_s(msg.get("recipient")) or "all") != "all" or ct in _CG_TOOL_CTS:
        return "tool/execution node(s)", [], 0
    if role not in {"user", "assistant"} or ct not in {"text", "multimodal_text"}:
        return ("unrecognized-role node(s)" if role not in {"user", "assistant"} else "non-text content node(s)"), [], 0
    raw = cnt["parts"] if isinstance(cnt.get("parts"), list) else ([cnt["text"]] if isinstance(cnt.get("text"), str) else [])
    non_str = sum(1 for p in raw if not isinstance(p, str)) or (len(meta["attachments"]) if isinstance(meta.get("attachments"), list) else 0)
    return role, [(i, p) for i, p in enumerate(raw) if isinstance(p, str) and p.strip()], non_str


def _read_chatgpt(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    items = _load_conv_list(decoded, "chatgpt", "mapping", dict, "chat_messages")
    rows, gaps = [], ([] if items else ["chatgpt: conversations list is empty"])
    for idx, conv in enumerate(items):
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            gaps.append(f"chatgpt: conversation {idx}: skipped malformed entry")
            continue
        cid = _s(conv.get("id")) or _s(conv.get("conversation_id"))
        cname, lconv = cid or _s(conv.get("title")) or f"{safe_label}:{idx + 1}", cid or f"conv-{idx + 1}"
        scope, omitted = f"chatgpt ({lconv})", Counter()
        nodes = {nid: obj for nid, obj in conv["mapping"].items() if isinstance(obj, dict)}
        parents = {nid: _s(nobj.get("parent")) for nid, nobj in nodes.items()}
        for nid, nobj in nodes.items():
            for ch in (nobj["children"] if isinstance(nobj.get("children"), list) else ()):
                if (ch_id := _s(ch)) in nodes and parents.get(ch_id) not in nodes:
                    parents[ch_id] = nid
        parsed = {nid: _chatgpt_node(nobj) for nid, nobj in nodes.items()}
        vis = {nid for nid, (tag, pts, ns) in parsed.items() if tag in {"user", "assistant"} and (pts or ns > 0)}
        for nid in _resolve_chain(list(nodes), parents, vis, _s(conv.get("current_node")), True, scope, gaps):
            tag, parts, non_str = parsed[nid]
            if tag in {"user", "assistant"}:
                omitted["non-text part(s)"] += non_str
                msg = nodes[nid]["message"]
                mid, date = _id_str(msg.get("id"), nid), _format_date(msg.get("create_time")) or _format_date(msg.get("update_time"))
                rows.extend(_row("chatgpt", cname, lconv, mid, p_idx, tag, txt, date) for p_idx, txt in parts)
            elif tag != "empty":
                omitted[tag] += 1
        _flush_gaps(omitted, scope, gaps)
    return rows, gaps, []


def _read_claude(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    items = _load_conv_list(decoded, "claude", "chat_messages", list, "mapping")
    rows, gaps = [], ([] if items else ["claude: conversations list is empty"])
    for idx, conv in enumerate(items):
        if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
            gaps.append(f"claude: conversation {idx}: skipped malformed entry")
            continue
        cid = _s(conv.get("uuid")) or _s(conv.get("id"))
        cname, lconv = cid or _s(conv.get("name")) or _s(conv.get("title")) or f"{safe_label}:{idx + 1}", cid or f"conv-{idx + 1}"
        scope, msgs, omitted = f"claude ({lconv})", conv["chat_messages"], Counter()
        by_uid = {uid: (i, m) for i, m in enumerate(msgs) if isinstance(m, dict) and (uid := _s(m.get("uuid")))}
        if by_uid and any(isinstance(m, dict) and "parent_message_uuid" in m for m in msgs):
            parents = {uid: (None if (p := _s(m.get("parent_message_uuid"))) == _CLAUDE_ROOT else p) for uid, (_, m) in by_uid.items()}
            vis = {uid for uid, (_, m) in by_uid.items() if (_s(m.get("sender")) or _s(m.get("role"))) in {"human", "user", "assistant"}}
            selected = [by_uid[u] for u in _resolve_chain(list(by_uid), parents, vis, _s(conv.get("current_leaf_message_uuid")), "current_leaf_message_uuid" in conv, scope, gaps)]
        else:
            selected = list(enumerate(msgs))
        for m_idx, msg in selected:
            if not isinstance(msg, dict):
                omitted["malformed message(s)"] += 1
            elif (sender := _s(msg.get("sender")) or _s(msg.get("role")) or "") not in {"human", "user", "assistant"}:
                omitted["non-dialogue message(s)"] += 1
            else:
                cnt, raw_txt = msg.get("content"), msg.get("text")
                parts = _typed_parts(cnt, _TEXT_ONLY, omitted) if isinstance(cnt, list) and cnt else ([(0, raw_txt)] if isinstance(raw_txt, str) and raw_txt.strip() else [])
                omitted["attachment/file item(s)"] += sum(len(msg[k]) for k in ("attachments", "files", "files_v2") if isinstance(msg.get(k), list))
                mid, date = _id_str(msg.get("uuid") or msg.get("id"), f"m{m_idx}"), _format_date(msg.get("created_at")) or _format_date(msg.get("updated_at"))
                spk = "assistant" if sender == "assistant" else "user"
                rows.extend(_row("claude", cname, lconv, mid, p_idx, spk, txt, date) for p_idx, txt in parts)
        _flush_gaps(omitted, scope, gaps)
    return rows, gaps, []


def _is_cc_record(obj: dict[str, Any]) -> bool:
    rt = _s(obj.get("type"))
    if rt not in _CC_TYPES:
        return False
    if rt not in {"user", "assistant"}:
        return True
    msg = obj.get("message")
    return (isinstance(msg, dict) and ("content" in msg or "role" in msg)) or _flag(obj.get("isCompactSummary"))


def _read_claude_code(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    records, valid_lines, bad = _parse_jsonl(decoded, "claude-code", _is_cc_record, lambda o: _s(o.get("type")) in _CODEX_ENV)
    default_sid = next((_s(o.get("sessionId")) or _s(o.get("session_id")) for ln, o in records if ln in valid_lines and (_s(o.get("sessionId")) or _s(o.get("session_id")))), None)
    scope, omitted = f"claude-code ({default_sid or safe_label})", Counter()
    rows, replays, gaps = [], set(), [f"{scope}: line {ln}: skipped malformed JSONL line" for ln in bad]
    for ln, obj in records:
        rt = _s(obj.get("type")) or ""
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if ln not in valid_lines:
            omitted["unrecognized record(s)"] += 1
        elif _flag(obj.get("isCompactSummary")) or _flag(msg.get("isCompactSummary")):
            omitted["compact summary record(s)"] += 1
        elif _flag(obj.get("isSidechain")) or _flag(obj.get("isMeta")):
            omitted["sidechain/meta entry(ies)"] += 1
        elif _is_analysis_marked(obj) or _is_analysis_marked(msg):
            omitted["reasoning block(s)"] += 1
        elif rt not in {"user", "assistant"}:
            if rt in {"summary", "system"}:
                omitted[f"{rt} record(s)"] += 1
        else:
            role = _s(msg.get("role")) or rt
            if role not in {"user", "assistant"}:
                omitted["non-dialogue message(s)"] += 1
                continue
            conv = _s(obj.get("sessionId")) or _s(obj.get("session_id")) or default_sid or safe_label
            rec_id, date, uid = _id_str(obj.get("uuid"), f"L{ln}"), _format_date(obj.get("timestamp")), _s(obj.get("uuid"))
            for p_idx, txt in _typed_parts(msg.get("content"), _TEXT_ONLY, omitted):
                spk = role
                if role == "user":
                    pos = 0
                    while pos < len(txt) and (m := _CC_WRAPPER_RE.match(txt, pos)):
                        pos = m.end()
                    if pos > 0 and pos == len(txt):
                        omitted["wrapper/reminder block(s)"] += 1
                        continue
                    if _CC_LINE_REM_RE.search(_CODE_SPAN_RE.sub("", txt)):
                        spk = "unknown"
                        omitted["inseparable reminder block(s) marked unknown"] += 1
                if not uid or (uid, p_idx, txt) not in replays:
                    if uid:
                        replays.add((uid, p_idx, txt))
                    rows.append(_row("claude-code", conv, conv, rec_id, p_idx, spk, txt, date))
    _flush_gaps(omitted, scope, gaps)
    return rows, gaps, []


def _is_codex_record(obj: dict[str, Any]) -> bool:
    rt = _s(obj.get("type"))
    if rt in _CODEX_ENV:
        return rt == "compacted" or isinstance(obj.get("payload"), dict)
    return rt in _CODEX_ITEMS and (rt != "message" or (_s(obj.get("role")) in {"user", "assistant", "developer", "system"} and isinstance(obj.get("content"), list) and any(isinstance(p, dict) and _s(p.get("type")) in _CODEX_PARTS for p in obj["content"])))


def _read_codex(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Keep the FIRST record form of each piece of speech, in file order.

    A Codex rollout may carry the same speech twice: a ``response_item`` message
    and the ``event_msg`` mirroring it. Within a turn, one event row and one
    response item whose whole visible text is equal are one piece of speech: the
    EARLIER line of the two is emitted, with its own locator, parts and date,
    and the later one is deduplicated — never an omission; the count rides
    ``notes``. A partial copy of the journal that later adds the other form
    therefore leaves an already-read row where it was, so a stored address or
    quote keeps pointing at the same record.

    The pairing is spent, one event per equal item: equal records of one form
    are genuine repeats and stay visible, and an item no event mirrors (a last
    reply of an export cut mid-turn, say) stays visible with its own parts and
    locator. Both forms keep their original line order. Developer/system items,
    analysis and harness-context blocks are dropped before a record reaches the
    pending list, so nothing kept here resurrects them.
    """
    records, valid_lines, bad = _parse_jsonl(decoded, "codex", _is_codex_record, lambda o: _s(o.get("type")) in {"user", "assistant"} and isinstance(o.get("message"), dict))
    sid = next((_s(o["payload"].get("id")) or _s(o["payload"].get("conversation_id")) for ln, o in records if ln in valid_lines and _s(o.get("type")) == "session_meta" and isinstance(o.get("payload"), dict) and (_s(o["payload"].get("id")) or _s(o["payload"].get("conversation_id")))), None)
    conv, scope, omitted = sid or safe_label, f"codex ({sid or safe_label})", Counter()
    rows: list[dict[str, Any]] = []
    gaps: list[str] = [f"{scope}: line {ln}: skipped malformed JSONL line" for ln in bad]
    notes: list[str] = []
    # This turn's pending speech, per speaker, in file order: ("event", row) or
    # ("item", (locator id, date, visible parts)).
    pend: dict[str, list[tuple[str, Any]]] = {"user": [], "assistant": []}
    mirrored, r_counts = 0, [0, 0]

    def flush() -> None:
        nonlocal mirrored
        for r in ("user", "assistant"):
            keep = [True] * len(pend[r])
            unmatched: dict[str, list[int]] = {}
            for i, (form, payload) in enumerate(pend[r]):
                text = payload["text"] if form == "event" else "\n".join(txt for _, txt in payload[2])
                # One event covers ONE equal item: the pairing is spent, never a
                # set that keeps matching every equal record. Only the two forms
                # pair — equal records of one form are repeats, and both stay.
                earlier = next((j for j in unmatched.get(text, ()) if pend[r][j][0] != form), None)
                if earlier is None:
                    unmatched.setdefault(text, []).append(i)
                else:
                    unmatched[text].remove(earlier)
                    keep[i] = False  # the later form of one speech: a mirror, not a repeat
                    mirrored += 1
            for i, (form, payload) in enumerate(pend[r]):
                if not keep[i]:
                    continue
                if form == "event":
                    rows.append(payload)
                else:
                    iid, idate, parts = payload
                    rows.extend(_row("codex", conv, conv, iid, p_idx, r, txt, idate) for p_idx, txt in parts)
            pend[r] = []
        if max(r_counts):
            omitted["reasoning item(s)"] += max(r_counts)
            r_counts[0] = r_counts[1] = 0

    for ln, obj in records:
        rt, date = _s(obj.get("type")) or "", _format_date(obj.get("timestamp"))
        if ln not in valid_lines:
            omitted["unrecognized record(s)"] += 1
        elif rt == "compacted":
            flush()
            omitted["compacted summary item(s)"] += 1
        elif rt == "event_msg":
            pl = obj["payload"]
            et = _s(pl.get("type")) or ""
            if et in {"task_started", "turn_aborted"}:
                flush()
            elif et in {"agent_reasoning", "agent_reasoning_raw_content", "reasoning", "thinking", "analysis"}:
                r_counts[1] += 1
            elif et in {"user_message", "agent_message"}:
                if _is_analysis_marked(obj) or _is_analysis_marked(pl):
                    r_counts[1] += 1
                elif (_s(pl.get("recipient")) or "all") != "all":
                    omitted["tool call/output item(s)"] += 1
                else:
                    role = "user" if et == "user_message" else "assistant"
                    if role == "user" and pend["assistant"]:
                        flush()
                    if role == "user":
                        omitted["image attachment(s)"] += sum(len(pl[k]) for k in ("images", "local_images") if isinstance(pl.get(k), list))
                    txt = pl.get("message")
                    if isinstance(txt, str) and txt.strip():
                        pend[role].append(("event", _row("codex", conv, conv, f"L{ln}", 0, role, txt, date)))
        elif rt not in {"session_meta", "turn_context"}:
            item = obj["payload"] if rt == "response_item" else obj
            it, role = _s(item.get("type")) or "", _s(item.get("role")) or ""
            if it == "reasoning":
                r_counts[0] += 1
            elif it != "message":
                omitted["tool call/output item(s)"] += 1
            elif _is_analysis_marked(obj) or _is_analysis_marked(item):
                r_counts[0] += 1
            elif (_s(item.get("recipient")) or "all") != "all":
                omitted["tool call/output item(s)"] += 1
            elif role not in {"user", "assistant"}:
                omitted["developer/system item(s)" if role in {"developer", "system"} else "non-dialogue item(s)"] += 1
            else:
                vis: list[tuple[int, str]] = []
                for p_idx, txt in _typed_parts(item.get("content"), _TEXT_IO, omitted):
                    s = txt.strip()
                    if role == "user" and s and (_CODEX_CTX_RE.match(s) or (s.startswith("# AGENTS.md instructions for ") and "<INSTRUCTIONS>" in s and s.endswith("</INSTRUCTIONS>"))):
                        omitted["harness context block(s)"] += 1
                    else:
                        vis.append((p_idx, txt))
                if vis:
                    if role == "user" and pend["assistant"]:
                        flush()
                    pend[role].append(("item", (_id_str(item.get("id"), f"L{ln}"), date or _format_date(item.get("timestamp")), vis)))
    flush()
    if mirrored:
        notes.append(f"{scope}: deduplicated {mirrored} mirrored duplicate record(s) "
                     "(each text kept once, at its first-seen record)")
    _flush_gaps(omitted, scope, gaps)
    return rows, gaps, notes


def _read_hermes(decoded: str, base_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    base = base_label.lower()
    if base in _HERMES_DENY or base.endswith((".db", ".sqlite", ".sqlite3", ".yaml", ".yml")):
        raise ValueError(f"Unsupported Hermes file {base!r}: state.db and raw profiles are rejected")
    if base in _HERMES_DOCS:
        canon = _HERMES_DOCS[base]
        if _is_json_or_jsonl(decoded):
            raise ValueError(f"Expected Markdown text in {canon}, got JSON/JSONL")
        return [_row("hermes", canon, canon, "doc", 0, "document", decoded, None)], [], []
    records, valid_lines, bad = _parse_jsonl(decoded, "hermes", lambda o: bool(_s(o.get("id"))) and isinstance(o.get("messages"), list) and "mapping" not in o and "chat_messages" not in o)
    rows, gaps = [], [f"hermes: line {ln}: skipped malformed JSONL line" for ln in bad]
    for ln, sess in records:
        if ln not in valid_lines:
            gaps.append(f"hermes: line {ln}: skipped non-session JSON object")
            continue
        sid, omitted = _s(sess["id"]) or f"L{ln}", Counter()
        for m_idx, msg in enumerate(sess["messages"]):
            if not isinstance(msg, dict):
                omitted["malformed message(s)"] += 1
            elif _flag(msg.get("_compressed_summary")) or _flag(msg.get("is_compressed_summary")):
                omitted["compressed summary message(s)"] += 1
            elif not bool(1 if msg.get("active") is None else msg["active"]) and not bool(msg.get("compacted") or 0):
                omitted["undone message(s)"] += 1
            elif _is_analysis_marked(msg):
                omitted["reasoning message(s)"] += 1
            else:
                role = _s(msg.get("role")) or ""
                if role in {"tool", "system", "developer"}:
                    omitted[f"{'tool' if role == 'tool' else 'system'} message(s)"] += 1
                    continue
                if any(_s(msg.get(k)) or (isinstance(msg.get(k), list) and msg[k]) for k in ("reasoning", "reasoning_content", "thinking")):
                    omitted["reasoning field(s)"] += 1
                if isinstance(msg.get("tool_calls"), list):
                    omitted["tool call(s)"] += len(msg["tool_calls"])
                if role in {"user", "assistant"}:
                    mid, date = _id_str(msg.get("id"), f"m{m_idx}"), _format_date(msg.get("timestamp"))
                    rows.extend(_row("hermes", sid, sid, mid, p_idx, role, txt, date) for p_idx, txt in _typed_parts(msg.get("content"), _TEXT_IO, omitted))
                else:
                    omitted["unrecognized-role message(s)"] += 1
        _flush_gaps(omitted, f"hermes ({sid})", gaps)
    return rows, gaps, []
