"""Offline export readers for Amberkeep (stdlib-only, pure bytes-in).

``read_source`` reads plain JSON/JSONL bytes, and — for the two vendors whose
official exports ship as an archive — a raw ChatGPT or Claude export ZIP: only
its one main ``conversations.json`` member is read, in memory, and nothing is
extracted. One read is bounded: input larger than ``_MAX_EXPORT_BYTES``
uncompressed bytes is refused before it is read, with a way to hand over a
smaller file. Pasted literal material (``text``, ``summary``) is carried
verbatim: no content filter, so a JSON-shaped summary, or one quoting harness
markup, stays exactly what the person supplied.

An export's own markers decide who spoke, what was visible, where one
conversation ends and what was left out — never a display title or a look-alike
copy of the same words. These are rules about the shape of the data, not a
reading of anyone's intent:

* a known harness block is not given to the person: the two record forms of
  one Codex reply are paired on their whole text first, then on an item's
  visible parts alone (an explicit ``kind`` on one form also covers its paired
  copy), and only what is no harness block is kept, once: the item's other
  parts, or the event before it when that event is exactly those parts and
  holds no known block itself, so that row stays where it was;
  Codex text that still holds a known block beside other words, with nothing
  to separate them, is attributed to no one; a Claude Code wrapper element that
  starts a line is cut out in one pass, nested elements of its tag balanced;
  the person's words around it stay separate continuous excerpts, never
  joined, and an excerpt whose shape cannot tell the harness from the
  person — or a record whose type and message role disagree — is attributed
  to no one;
* hidden reasoning is left out, visible commentary stays;
* a conversation is named by its real identifier, never by its title; one Codex
  file may hold several sessions, each told apart by its own header, and what
  has no identifier is addressed by its position in the file, with a note;
* what is left out is counted in ``gaps`` as records; plain bookkeeping goes to
  ``gaps.notes``.
"""
from __future__ import annotations

from bisect import bisect_right
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
# One scan finds the only two things that matter on a line start: a code fence or
# the opening tag of a known harness wrapper. Anything else is the person's text.
_CC_LINE_RE = re.compile(rf"^[ \t]*(?:(`{{3,}}|~{{3,}})|<({_CC_TAGS})(?=[\s>])[^>\n]*>)", re.MULTILINE)
_CC_NEXT_RE = re.compile(rf"[ \t]*<({_CC_TAGS})(?=[\s>])[^>\n]*>")
# The elements Codex wraps its own context in: _CODEX_CTX_RE takes a text that is one whole element,
# _CODEX_OPEN_RE finds where one opens (for _codex_holds_block).
_CODEX_TAGS = ("environment_context", "permissions instructions", "user_instructions", "collaboration_mode", "subagent_notification", "turn_aborted")
_CODEX_CTX_RE = re.compile(rf"\A\s*<({'|'.join(_CODEX_TAGS)})\b[^>]*>(?:(?!</\1>).)*</\1>\s*\Z", re.DOTALL)
_CODEX_OPEN_RE = re.compile(rf"<({'|'.join(_CODEX_TAGS)})\b")
_CODEX_AGENTS = "# AGENTS.md instructions for "
# A ``user_message`` event may name its ``kind``; these two are the harness's own context, mirrored as an event.
_CODEX_SERVICE_KINDS = frozenset({"user_instructions", "environment_context"})
_CG_VISIBLE_CHANNELS = frozenset({"final", "commentary"})


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


def _conv_id(*vals: Any) -> str | None:
    """A conversation's real identifier: the first non-empty string or integer among ``vals``.

    A title or name is never offered here: titles repeat ("New chat"), so they
    cannot tell two conversations apart.
    """
    for val in vals:
        if isinstance(val, int) and not isinstance(val, bool):
            return str(val)
        if text := _s(val):
            return text
    return None


def _conv_keys(cid: str | None, label: str, idx: int) -> tuple[str, str]:
    """``(conversation, locator part)`` of the ``idx``-th conversation of an export.

    The real identifier names both. Without one the conversation is addressed by
    its position in this file, never by its title; the reader says so in a note.
    """
    return (cid, cid) if cid else (f"{label}:{idx + 1}", f"conv-{idx + 1}")


def _positional_note(kind: str, count: int, what: str = "conversation(s) carry no identifier") -> list[str]:
    return [f"{kind}: {count} {what} and are addressed by their position in this file; "
            "a re-export may number them differently"] if count else []


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
    # Only the hidden channels are reasoning. "commentary" is what the person
    # saw while the answer was being worked on; when it is addressed to a tool
    # the recipient check below sets it aside as tool traffic.
    if ct in {"thoughts", "reasoning_recap"} or (_s(msg.get("channel")) or "final").lower() not in _CG_VISIBLE_CHANNELS:
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
    positional = 0
    for idx, conv in enumerate(items):
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            gaps.append(f"chatgpt: conversation {idx}: skipped malformed entry")
            continue
        cid = _conv_id(conv.get("id"), conv.get("conversation_id"))
        cname, lconv = _conv_keys(cid, safe_label, idx)
        positional += 0 if cid else 1
        scope, omitted = f"chatgpt ({lconv})", Counter()
        nodes = {nid: obj for nid, obj in conv["mapping"].items() if isinstance(obj, dict)}
        omitted["malformed node(s)"] += len(conv["mapping"]) - len(nodes)
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
    return rows, gaps, _positional_note("chatgpt", positional)


def _read_claude(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    items = _load_conv_list(decoded, "claude", "chat_messages", list, "mapping")
    rows, gaps = [], ([] if items else ["claude: conversations list is empty"])
    positional = 0
    for idx, conv in enumerate(items):
        if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
            gaps.append(f"claude: conversation {idx}: skipped malformed entry")
            continue
        cid = _conv_id(conv.get("uuid"), conv.get("id"))
        cname, lconv = _conv_keys(cid, safe_label, idx)
        positional += 0 if cid else 1
        scope, msgs, omitted = f"claude ({lconv})", conv["chat_messages"], Counter()
        by_uid: dict[str, tuple[int, Any]] = {}
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and (uid := _s(m.get("uuid"))) and uid not in by_uid:
                by_uid[uid] = (i, m)
        if by_uid and any(isinstance(m, dict) and "parent_message_uuid" in m for m in msgs):
            parents = {uid: (None if (p := _s(m.get("parent_message_uuid"))) == _CLAUDE_ROOT else p) for uid, (_, m) in by_uid.items()}
            vis = {uid for uid, (_, m) in by_uid.items() if (_s(m.get("sender")) or _s(m.get("role"))) in {"human", "user", "assistant"}}
            selected = [by_uid[u] for u in _resolve_chain(list(by_uid), parents, vis, _s(conv.get("current_leaf_message_uuid")), "current_leaf_message_uuid" in conv, scope, gaps)]
            # The tree can place only a message with a distinct uuid: the rest are counted, never dropped unseen.
            omitted["message(s) without a distinct identifier, which the conversation tree cannot place"] += len(msgs) - len(by_uid)
        else:
            selected = list(enumerate(msgs))
        for m_idx, msg in selected:
            if not isinstance(msg, dict):
                omitted["malformed message(s)"] += 1
            elif (sender := _s(msg.get("sender")) or _s(msg.get("role")) or "") not in {"human", "user", "assistant"}:
                # A message that names no sender is not called "non-dialogue": its speaker is unknown, not absent.
                omitted["non-dialogue message(s)" if sender else "message(s) with no sender"] += 1
            else:
                cnt, raw_txt = msg.get("content"), msg.get("text")
                parts = _typed_parts(cnt, _TEXT_ONLY, omitted) if isinstance(cnt, list) and cnt else ([(0, raw_txt)] if isinstance(raw_txt, str) and raw_txt.strip() else [])
                omitted["attachment/file item(s)"] += sum(len(msg[k]) for k in ("attachments", "files", "files_v2") if isinstance(msg.get(k), list))
                mid, date = _id_str(msg.get("uuid") or msg.get("id"), f"m{m_idx}"), _format_date(msg.get("created_at")) or _format_date(msg.get("updated_at"))
                spk = "assistant" if sender == "assistant" else "user"
                rows.extend(_row("claude", cname, lconv, mid, p_idx, spk, txt, date) for p_idx, txt in parts)
        _flush_gaps(omitted, scope, gaps)
    return rows, gaps, _positional_note("claude", positional)


def _is_cc_record(obj: dict[str, Any]) -> bool:
    rt = _s(obj.get("type"))
    if rt not in _CC_TYPES:
        return False
    if rt not in {"user", "assistant"}:
        return True
    msg = obj.get("message")
    return (isinstance(msg, dict) and ("content" in msg or "role" in msg)) or _flag(obj.get("isCompactSummary"))


_CC_TAG_RE = {tag: re.compile(rf"</{tag}>|<{tag}(?=[\s>])[^>\n]*>") for tag in _CC_TAGS.split("|")}


def _cc_close(text: str, tag: str, at: int) -> int:
    """Where the element of ``tag`` opened just before ``at`` closes, or -1.

    Nested elements of the same tag are balanced: each opening tag inside
    needs its own closing tag first, so the harness text after a nested
    element is never taken for the end of the outer one. A self-closing tag
    opens nothing. An element that never balances has no known end."""
    depth = 1
    for m in _CC_TAG_RE[tag].finditer(text, at):
        token = m.group(0)
        if token.startswith("</"):
            depth -= 1
            if not depth:
                return m.start()
        elif not token.endswith("/>"):
            depth += 1
    return -1


def _cc_element(text: str, m: re.Match[str]) -> tuple[int, int, int | None]:
    """The wrapper element ``m`` opens, with any chained after it on its line.

    Returns the number of balanced elements, their end, and where an
    element that never closes begins (``None`` when every one closes). A
    closing tag is searched for from where its own element opened, counting
    nested elements of the same tag (``_cc_close``)."""
    tag, at, end, count = m.group(2), m.end(), m.end(), 0
    opened = m.start(2) - 1
    while True:
        close = _cc_close(text, tag, at)
        if close < 0:
            return count, end, opened
        end, count = close + len(tag) + 3, count + 1
        if not (nxt := _CC_NEXT_RE.match(text, end)):
            return count, end, None
        tag, at, opened = nxt.group(1), nxt.end(), nxt.start(1) - 1


def _cc_spans(text: str, pos: int, doubtful: bool) -> tuple[list[tuple[int, int, str]], int, int | None]:
    """The spans of one user text that are not the person's, from ``pos``.

    Each span is ``(start, finish, what)``: ``what`` is ``cut`` (a harness
    wrapper, left out) or ``unknown`` (an excerpt that may be the harness's or
    the person's). A wrapper is a balanced element of a known harness tag whose
    opening tag starts a line, outside a code fence. An opening tag that never
    closes makes the rest of the text ``unknown``. In ``doubtful`` mode (the
    rest of a code fence that never closes) fences are not judged, and a
    balanced element is ``unknown`` rather than cut. Returns the spans, the
    number of elements cut, and where a fence left open began."""
    spans: list[tuple[int, int, str]] = []
    removed = 0
    fence: tuple[str, int, int] | None = None
    while m := _CC_LINE_RE.search(text, pos):
        pos = m.end()
        if mark := m.group(1):
            if doubtful:
                continue
            if fence is None:
                fence = (mark[0], len(mark), m.start())
            elif mark[0] == fence[0] and len(mark) >= fence[1]:
                fence = None
            continue
        if fence:
            continue
        count, end, unclosed = _cc_element(text, m)
        if count:
            eol = text.find("\n", end)
            stop = len(text) if eol < 0 else eol
            whole = unclosed is None and not text[end:stop].strip(" \t\r")
            if doubtful:
                spans.append((m.start(2) - 1, end, "unknown"))
            elif whole:  # the line is the wrapper's alone: it goes with its line break
                spans.append((m.start(), stop + 1 if eol >= 0 else stop, "cut"))
                removed += count
            else:  # other characters share the line: the element(s) only, spacing stays
                spans.append((m.start(2) - 1, end, "cut"))
                removed += count
            pos = max(pos, spans[-1][1])
        if unclosed is not None:
            spans.append((unclosed if count else m.start(), len(text), "unknown"))
            return spans, removed, None
    return spans, removed, fence[2] if fence else None


def _cc_split_wrappers(text: str) -> tuple[list[tuple[str, str]], int]:
    """Split one user text around the harness's wrapper elements.

    Returns ``(pieces, number of elements cut)``: each piece is ``(speaker,
    excerpt)``, a continuous excerpt of the text as written, in order —
    ``user`` for the person's words, ``unknown`` where the shape cannot tell
    the harness from the person (an opening tag that never closes, or a
    wrapper inside a code fence that never closes). Nothing is joined across a
    cut: words a wrapper interrupts become separate excerpts, never one
    certified splice. Blank pieces are left out; a piece loses only the line
    break, and the indent, that led to a span, never the person's own spacing. With
    nothing cut and nothing in doubt, the one piece is the very same text.

    This is a rule about the shape of the text, not a reading of who meant it:
    a tag quoted in prose, in inline code or in a closed fence stays the
    person's, a person who starts a line with a balanced element loses it, and
    the count is reported in ``gaps``. The scan is linear: every step moves
    forward, and an unclosed tag ends it.
    """
    spans, removed, open_at = _cc_spans(text, 0, False)
    if open_at is not None:
        spans += _cc_spans(text, open_at, True)[0]
    if not spans:
        return [("user", text)], 0
    pieces: list[tuple[str, str]] = []
    cut = 0
    for start, finish, what in sorted(spans):
        before = text[cut:start]
        head, brk, indent = before.rpartition("\n")
        if brk and not indent.strip(" \t"):  # the break (and indent) that led to a span is no excerpt's end
            before = head.removesuffix("\r")
        if before.strip():
            pieces.append(("user", before))
        if what == "unknown" and text[start:finish].strip():
            pieces.append(("unknown", text[start:finish]))
        cut = finish
    if text[cut:].strip():
        pieces.append(("user", text[cut:]))
    return pieces, removed


def _read_claude_code(decoded: str, safe_label: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    records, valid_lines, bad = _parse_jsonl(decoded, "claude-code", _is_cc_record, lambda o: _s(o.get("type")) in _CODEX_ENV)
    default_sid = next((_s(o.get("sessionId")) or _s(o.get("session_id")) for ln, o in records if ln in valid_lines and (_s(o.get("sessionId")) or _s(o.get("session_id")))), None)
    scope, omitted = f"claude-code ({default_sid or safe_label})", Counter()
    rows, replays, gaps = [], set(), [f"{scope}: line {ln}: skipped malformed JSONL line" for ln in bad]
    replayed = split = doubtful = disagreed = 0
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
            if role != rt:
                # The record's type and its message's role name different
                # speakers: neither is taken over the other. Its text is kept,
                # attributed to no one, and no wrapper is cut from it.
                role = "unknown"
                disagreed += 1
            conv = _s(obj.get("sessionId")) or _s(obj.get("session_id")) or default_sid or safe_label
            rec_id, date, uid = _id_str(obj.get("uuid"), f"L{ln}"), _format_date(obj.get("timestamp")), _s(obj.get("uuid"))
            for p_idx, txt in _typed_parts(msg.get("content"), _TEXT_ONLY, omitted):
                # A resumed session replays records: the same part of the same record, in the same session, by the same speaker.
                if uid and (conv, role, uid, p_idx, txt) in replays:
                    replayed += 1
                    continue
                if uid:
                    replays.add((conv, role, uid, p_idx, txt))
                if role != "user":
                    rows.append(_row("claude-code", conv, conv, rec_id, p_idx, role, txt, date))
                    continue
                # Known wrapper elements are cut out, and the words around them stay apart; see _cc_split_wrappers.
                pieces, wrappers = _cc_split_wrappers(txt)
                omitted["wrapper/reminder block(s)"] += wrappers
                doubtful += sum(1 for spk, _ in pieces if spk == "unknown")
                if len(pieces) == 1:
                    rows.append(_row("claude-code", conv, conv, rec_id, p_idx, pieces[0][0], pieces[0][1], date))
                    continue
                split += bool(pieces)
                rows.extend(_row("claude-code", conv, conv, rec_id, f"{p_idx}.{n}", spk, piece, date)
                            for n, (spk, piece) in enumerate(pieces, start=1))
    _flush_gaps(omitted, scope, gaps)
    notes = [f"{scope}: deduplicated {replayed} replayed record(s) (same session, speaker, identifier and text; each kept once)"] if replayed else []
    if split:
        notes.append(f"{scope}: {split} user text(s) interrupted by harness elements are kept as separate "
                     "continuous excerpts (locators /pN.M), never joined into one")
    if doubtful:
        notes.append(f"{scope}: {doubtful} excerpt(s) cannot be told apart from harness text by their shape "
                     "and are attributed to no one (speaker unknown)")
    if disagreed:
        notes.append(f"{scope}: {disagreed} record(s) whose type and message role name different speakers "
                     "are kept whole and attributed to no one (speaker unknown)")
    return rows, gaps, notes


def _is_codex_record(obj: dict[str, Any]) -> bool:
    rt = _s(obj.get("type"))
    if rt in _CODEX_ENV:
        return rt == "compacted" or isinstance(obj.get("payload"), dict)
    return rt in _CODEX_ITEMS and (rt != "message" or (_s(obj.get("role")) in {"user", "assistant", "developer", "system"} and isinstance(obj.get("content"), list) and any(isinstance(p, dict) and _s(p.get("type")) in _CODEX_PARTS for p in obj["content"])))


def _codex_is_service(text: str, kind: Any = None) -> bool:
    """Is this user-role text one harness block rather than the person's words?

    Each record form is judged by the markers it carries: a ``response_item``
    part and an ``event_msg`` ``user_message`` are asked alike about the text
    (one whole harness block: environment context, user or AGENTS.md
    instructions, a mode, abort or sub-agent notice), and an event may also say
    so itself through its ``kind``. The person's own words are not service text,
    even when they mention such a tag. A mark found on one form reaches the
    other only through the pairing in ``_read_codex``.
    """
    if not (s := text.strip()):
        return False
    if (_s(kind) or "").lower() in _CODEX_SERVICE_KINDS:
        return True
    return bool(_CODEX_CTX_RE.match(s)) or (s.startswith(_CODEX_AGENTS) and "<INSTRUCTIONS>" in s and s.endswith("</INSTRUCTIONS>"))


def _codex_holds_block(text: str) -> bool:
    """Does this text hold, anywhere in it, a harness block of a shape ``_codex_is_service``
    takes whole: a known element whose closing tag follows its opening tag, or the AGENTS.md
    instructions heading with a closed ``<INSTRUCTIONS>`` after it?

    A rule about shape only; text with no such block is not judged. The scan is linear: an
    opening tag ends at the first ``>`` after it — an end every opening before that ``>``
    shares — and only a closing tag that starts after that end can close the block.
    """
    closes = {tag: text.rfind(f"</{tag}>") for tag in _CODEX_TAGS}
    end = -1
    for m in _CODEX_OPEN_RE.finditer(text):
        if m.end() > end and (end := text.find(">", m.end())) < 0:
            break
        if closes[m.group(1)] > end:
            return True
    at = text.find(_CODEX_AGENTS)
    return at >= 0 and (opened := text.find("<INSTRUCTIONS>", at)) >= 0 and text.find("</INSTRUCTIONS>", opened) >= 0


def _codex_pair(recs: list[tuple[str, Any, bool]], partner: list[int | None], key: Callable[[str, Any], str | None]) -> None:
    """Pair the two record forms of one speech on ``key``, in file order, one to one.

    One event covers ONE equal item: the pairing is spent, never a set that keeps matching
    every equal record. Only the two forms pair — equal records of one form are repeats, and
    both stay. A record already paired, or one ``key`` gives no text, takes no part.
    """
    unmatched: dict[str, list[int]] = {}
    for i, (form, payload, _) in enumerate(recs):
        if partner[i] is not None or (text := key(form, payload)) is None:
            continue
        earlier = next((j for j in unmatched.get(text, ()) if recs[j][0] != form), None)
        if earlier is None:
            unmatched.setdefault(text, []).append(i)
        else:
            unmatched[text].remove(earlier)
            partner[i], partner[earlier] = earlier, i


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
    locator. Both forms keep their original line order. Developer/system items
    and analysis are dropped before a record reaches the pending list, so nothing
    kept here resurrects them.

    Service text. The pair is found before anything is left out, and Codex
    echoes an item in one of two shapes: its whole text (every text part
    joined, harness parts included) or its visible parts alone. Whole texts
    pair first; only then may an event left over meet, by those visible parts,
    an item left over that holds both harness and other parts. An item part
    that is one whole harness block (``_codex_is_service``) is omitted; when
    an item holds one, its pair is emitted once. An earlier event that is
    exactly the item's other parts, echoed by those visible parts alone, and
    holds no known block itself, stays the row it was, with its own locator,
    date and text. Any other pair is emitted as the item's other parts only,
    each a continuous excerpt with its own locator, never the event's mixed
    text (an echo of the whole item holds the harness block). An item of
    harness parts alone leaves out its pair. An event that names a service
    ``kind``, or is one whole block, is omitted with its paired item. Each is
    counted as omitted, one record each — every event, and every part of an
    item. A mark covers only its pair: the other form,
    equal text, in the same turn, spent one to one. A second equal item is a
    repeat and stays the person's, and so does the same text in another turn
    or session. User text that still holds a known block beside other words
    (``_codex_holds_block``), with no part of the export to separate them —
    an echo no item meets, say — is kept whole and attributed to no one, with
    a note; text that holds no known block is not judged.

    A file may hold several sessions. Each ``session_meta`` header ends the
    session before it — its pending speech is settled, its gap lines and notes
    are written — and starts the next one under its own identifier, so an
    event of one session never mirrors an item of another, and a repeat in the
    second session is not taken for a copy of the first. Records before the
    first header are a segment of their own. A session is named by its
    identifier; a file of one segment keeps its old address (the identifier, else
    the file's label), and in a file of several, each segment without an
    identifier is addressed by its position (``conv-N``, shown as ``label:N``),
    with a note.
    """
    records, valid_lines, bad = _parse_jsonl(decoded, "codex", _is_codex_record, lambda o: _s(o.get("type")) in {"user", "assistant"} and isinstance(o.get("message"), dict))
    heads = [(ln, _conv_id(o["payload"].get("id"), o["payload"].get("conversation_id"))) for ln, o in records if ln in valid_lines and _s(o.get("type")) == "session_meta"]
    # Records before the first header are a segment of their own, not part of that session.
    lead = not heads or min(valid_lines) < heads[0][0]
    ids = ([None] if lead else []) + [sid for _, sid in heads]
    if len(ids) == 1:  # a file of one segment keeps its old address: the identifier, else the file's label
        names = [(ids[0], ids[0]) if ids[0] else (safe_label, safe_label)]
    else:  # (conversation, locator part); a segment with no identifier is addressed by its position
        names = [(sid, sid) if sid else _conv_keys(None, safe_label, n) for n, sid in enumerate(ids)]
    starts = ([0] if lead else []) + [ln for ln, _ in heads]
    # A malformed line is reported under the segment its position falls in (before every header: the first one).
    gaps: list[str] = [f"codex ({names[max(bisect_right(starts, ln) - 1, 0)][1]}): line {ln}: skipped malformed JSONL line" for ln in bad]
    cname, lconv = names[0]
    later = iter(names[1:] if lead else names)  # each header opens the next segment
    omitted = Counter()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    # This turn's pending speech, per speaker, in file order: (form, payload, service) with ("event", row) or
    # ("item", (locator id, date, visible parts)); service is the harness mark the record carries itself.
    pend: dict[str, list[tuple[str, Any, bool]]] = {"user": [], "assistant": []}
    mirrored = mixed = 0

    def flush() -> None:
        nonlocal mirrored, mixed
        for r in ("user", "assistant"):
            recs = pend[r]
            # partner[i]: the other form of the same speech, once paired. Codex echoes an item whole —
            # every text part, its harness blocks included — or by its visible parts alone: whole texts
            # pair first, and only then may an event left over meet, by those parts, an item left over
            # that holds both harness and other parts.
            partner: list[int | None] = [None] * len(recs)
            _codex_pair(recs, partner, lambda form, p: p["text"] if form == "event" else "\n".join(t for _, t, _ in p[2]))
            _codex_pair(recs, partner, lambda form, p: p["text"] if form == "event" else (
                "\n".join(t for _, t, s in p[2] if not s) if 0 < sum(s for _, _, s in p[2]) < len(p[2]) else None))
            for i, (form, payload, own_mark) in enumerate(recs):
                j = partner[i]
                if j is not None and j < i:
                    continue  # the later form of one speech: settled with the earlier one
                event = payload if form == "event" else (recs[j][1] if j is not None else None)
                item = payload if form == "item" else (recs[j][1] if j is not None else None)
                # A mark the event carries itself covers its pair, and only its pair.
                marked = (own_mark if form == "event" else (j is not None and recs[j][2]))
                if marked:  # one event, and every part of its item
                    omitted["harness context record(s)"] += int(event is not None) + (len(item[2]) if item else 0)
                    continue
                harness = sum(1 for _, _, s in item[2] if s) if item else 0
                visible = [(p_idx, txt) for p_idx, txt, s in item[2] if not s] if item else []
                omitted["harness context record(s)"] += harness
                if item is not None and not visible:  # the whole speech is harness text
                    omitted["harness context record(s)"] += int(event is not None)
                    continue
                if j is not None:
                    mirrored += 1
                # An earlier event that is all speech stays the row it was, so a later copy moves nothing already read:
                # its item holds no harness part, or the event is exactly the item's visible parts and holds no known
                # block itself. Any other earlier event holds a block beside other words; the item's visible parts,
                # which separate them, speak instead.
                event_speaks = form == "event" and (not harness or (
                    event["text"] == "\n".join(txt for _, txt in visible) and not _codex_holds_block(event["text"])))
                if event_speaks:
                    spoken = [event]
                else:  # the item's visible parts only, once: each one a continuous excerpt as written
                    iid, idate, _parts = item
                    spoken = [_row("codex", cname, lconv, iid, p_idx, r, txt, idate) for p_idx, txt in visible]
                for row in spoken:
                    # A known harness block beside other words, which no part of the export separates, is no one's.
                    if r == "user" and _codex_holds_block(row["text"]):
                        row["speaker"] = "unknown"
                        mixed += 1
                rows.extend(spoken)
            pend[r] = []

    def close() -> None:
        """The end of one segment: its speech is settled, its notes and gap lines are written."""
        nonlocal mirrored, mixed
        flush()
        if mirrored:
            notes.append(f"codex ({lconv}): deduplicated {mirrored} mirrored duplicate record(s) "
                         "(each text kept once, at its first-seen record)")
            mirrored = 0
        if mixed:
            notes.append(f"codex ({lconv}): {mixed} text(s) hold a known harness block beside other words, with no "
                         "part of the export to separate them, and are attributed to no one (speaker unknown)")
            mixed = 0
        _flush_gaps(omitted, f"codex ({lconv})", gaps)
        omitted.clear()

    for ln, obj in records:
        rt, date = _s(obj.get("type")) or "", _format_date(obj.get("timestamp"))
        if ln not in valid_lines:
            omitted["unrecognized record(s)"] += 1
        elif rt == "compacted":
            flush()
            omitted["compacted summary item(s)"] += 1
        elif rt == "session_meta":
            close()
            cname, lconv = next(later)
        elif rt == "event_msg":
            pl = obj["payload"]
            et = _s(pl.get("type")) or ""
            if et in {"task_started", "turn_aborted"}:
                flush()
            elif et in {"agent_reasoning", "agent_reasoning_raw_content", "reasoning", "thinking", "analysis"}:
                omitted["reasoning record(s)"] += 1
            elif et in {"user_message", "agent_message"}:
                if _is_analysis_marked(obj) or _is_analysis_marked(pl):
                    omitted["reasoning record(s)"] += 1
                elif (_s(pl.get("recipient")) or "all") != "all":
                    omitted["tool call/output item(s)"] += 1
                else:
                    role, txt = "user" if et == "user_message" else "assistant", pl.get("message")
                    if role == "user" and pend["assistant"]:
                        flush()
                    if role == "user":
                        omitted["image attachment(s)"] += sum(len(pl[k]) for k in ("images", "local_images") if isinstance(pl.get(k), list))
                    if isinstance(txt, str) and txt.strip():
                        # The event's own marks (its text, its ``kind``) are read now; a paired item shares them at the flush.
                        pend[role].append(("event", _row("codex", cname, lconv, f"L{ln}", 0, role, txt, date),
                                           role == "user" and _codex_is_service(txt, pl.get("kind"))))
        elif rt != "turn_context":
            item = obj["payload"] if rt == "response_item" else obj
            it, role = _s(item.get("type")) or "", _s(item.get("role")) or ""
            if it == "reasoning":
                omitted["reasoning record(s)"] += 1
            elif it != "message":
                omitted["tool call/output item(s)"] += 1
            elif _is_analysis_marked(obj) or _is_analysis_marked(item):
                omitted["reasoning record(s)"] += 1
            elif (_s(item.get("recipient")) or "all") != "all":
                omitted["tool call/output item(s)"] += 1
            elif role not in {"user", "assistant"}:
                omitted["developer/system item(s)" if role in {"developer", "system"} else "non-dialogue item(s)"] += 1
            else:
                # Every text part, each with its own harness mark; the pairing reads the whole item,
                # and only the parts that are not harness blocks are ever emitted (see flush).
                parts = [(p_idx, txt, role == "user" and _codex_is_service(txt))
                         for p_idx, txt in _typed_parts(item.get("content"), _TEXT_IO, omitted)]
                if parts:
                    if role == "user" and pend["assistant"] and not all(s for _, _, s in parts):
                        flush()
                    pend[role].append(("item", (_id_str(item.get("id"), f"L{ln}"), date or _format_date(item.get("timestamp")), parts), False))
    close()
    # A file of one segment keeps its old address; in a file of several, each segment without an identifier says so here.
    notes += _positional_note("codex", sum(1 for sid in ids if not sid) if len(ids) > 1 else 0,
                              "segment(s) carry no session identifier (speech before the first session header, or a header without one)")
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
