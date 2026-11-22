"""Synthetic unit tests for Amberkeep Community offline export readers."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.amberkeep.scripts import readers  # noqa: E402
from skills.amberkeep.scripts.readers import read_source  # noqa: E402

KINDS = ("chatgpt", "claude", "claude-code", "codex", "hermes", "text", "summary")


def _jsonl(*objs: dict) -> bytes:
    return ("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n").encode("utf-8")


def _zip(*members: tuple[str, bytes]) -> bytes:
    """An invented export archive: the named members, nothing else."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buffer.getvalue()


class AmberkeepReadersTests(unittest.TestCase):
    def _check_rows(self, rows: list[dict]) -> None:
        locs = [r["locator"] for r in rows]
        self.assertEqual(len(locs), len(set(locs)))
        for r in rows:
            self.assertEqual(set(r), {"locator", "speaker", "text", "date", "conversation"})
            self.assertIn(r["speaker"], {"user", "assistant", "document", "unknown"})
            self.assertTrue(isinstance(r["text"], str) and r["text"].strip())
            self.assertTrue(r["date"] is None or isinstance(r["date"], str))
            self.assertTrue(isinstance(r["conversation"], str) and r["conversation"])

    def test_rejects_unsupported_kinds_archives_and_binary_inputs(self) -> None:
        for bad_kind in ("dots", "duet", "telegram", ""):
            with self.assertRaises(ValueError):
                read_source(bad_kind, b"synthetic text", "export.txt")
        for kind in KINDS:
            label = "MEMORY.md" if kind == "hermes" else "sample.dat"
            for payload in (b"", b"  \n ", b"PK\x03\x04zip", b"SQLite format 3\x00db", b"a\x00b", b"\xff\xfe"):
                with self.assertRaises(ValueError):
                    read_source(kind, payload, label)

    def test_chatgpt_branches_parent_only_chains_multipart_and_channel_filtering(self) -> None:
        unicode_msg = "  Привет 世界 \u2028\x85 café 🧪  "
        export = [{
            "id": "cg-1", "title": "Tree Chat", "current_node": "n-ans",
            "mapping": {
                "root": {"id": "root", "message": None, "parent": None, "children": ["u-old", "u-cur"]},
                "u-old": {"id": "u-old", "parent": "root", "message": {"id": "u-old", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Old draft"]}}},
                "u-cur": {"id": "u-cur", "parent": "root", "message": {
                    "id": "m#dup-2", "author": {"role": "user"}, "create_time": 1700000100.0,
                    "content": {"content_type": "multimodal_text", "parts": [{"content_type": "image_asset_pointer"}, unicode_msg, "Second block"]},
                }},
                "n-ctx": {"id": "n-ctx", "parent": "u-cur", "message": {"id": "n-ctx", "author": {"role": "user"}, "content": {"content_type": "user_editable_context", "user_profile": "CTX_SECRET"}}},
                "n-an": {"id": "n-an", "parent": "u-cur", "message": {"id": "n-an", "author": {"role": "assistant"}, "channel": "analysis", "content": {"content_type": "text", "parts": ["ANALYSIS_SECRET"]}}},
                "n-tl": {"id": "n-tl", "parent": "n-an", "message": {"id": "n-tl", "author": {"role": "tool"}, "content": {"content_type": "execution_output", "text": "42"}}},
                "n-ans": {"id": "n-ans", "parent": "n-an", "message": {"id": "m-ans", "author": {"role": "assistant"}, "channel": "final", "content": {"content_type": "text", "parts": ["Final reply."]}}},
            },
        }]
        rows, gaps = read_source("chatgpt", json.dumps(export).encode("utf-8"), "conversations.json")
        self._check_rows(rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [("user", unicode_msg, "chatgpt:cg-1:m%23dup-2/p1"), ("user", "Second block", "chatgpt:cg-1:m%23dup-2/p2"), ("assistant", "Final reply.", "chatgpt:cg-1:m-ans/p0")],
        )
        self.assertEqual(rows[0]["date"], "2023-11-14T22:15:00Z")
        gap_blob = "\n".join(gaps)
        self.assertIn("non-current branch", gap_blob)
        for hidden in ("ANALYSIS_SECRET", "CTX_SECRET", "Old draft"):
            self.assertNotIn(hidden, gap_blob)

        parent_only = [{"id": "cg-p", "mapping": {
            "r": {"id": "r", "message": None, "parent": None},
            "u1": {"id": "u1", "parent": "r", "message": {"id": "u1", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Parent-only Q"]}}},
            "a1": {"id": "a1", "parent": "u1", "message": {"id": "a1", "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Parent-only A"]}}},
        }}]
        rows_p, gaps_p = read_source("chatgpt", json.dumps(parent_only).encode("utf-8"), "c.json")
        self.assertEqual([r["text"] for r in rows_p], ["Parent-only Q", "Parent-only A"])
        self.assertTrue(any("current_node" in g for g in gaps_p))

        forked_no_cur = [{"id": "cg-f", "mapping": {
            "r": {"id": "r", "message": None, "parent": None},
            "u0": {"id": "u0", "parent": "r", "message": {"id": "u0", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Shared prefix"]}}},
            "h0": {"id": "h0", "parent": "u0", "message": {"id": "h0", "author": {"role": "tool"}, "content": {"content_type": "execution_output", "text": "hidden"}}},
            "u1": {"id": "u1", "parent": "h0", "message": {"id": "u1", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Second shared"]}}},
            "b1": {"id": "b1", "parent": "u1", "message": {"id": "b1", "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Branch 1"]}}},
            "b2": {"id": "b2", "parent": "u1", "message": {"id": "b2", "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Branch 2"]}}},
        }}]
        rows_f, gaps_f = read_source("chatgpt", json.dumps(forked_no_cur).encode("utf-8"), "c.json")
        self.assertEqual([r["text"] for r in rows_f], ["Shared prefix", "Second shared"])
        self.assertTrue(any("ambiguous branch" in g for g in gaps_f))

    def test_claude_and_claude_code_multipart_unicode_and_false_attribution_guards(self) -> None:
        claude_exp = [{
            "uuid": "cl-1", "name": "", "account": {"uuid": "SECRET_ACC"}, "current_leaf_message_uuid": "cm-1",
            "chat_messages": [
                {"uuid": "cm-alt", "parent_message_uuid": "00000000-0000-4000-8000-000000000000", "sender": "human", "text": "Alt branch"},
                {
                    "uuid": "cm-1", "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                    "sender": "assistant", "created_at": "2026-10-01T09:00:05Z", "text": "Legacy duplicate",
                    "content": [
                        {"type": "thinking", "thinking": "SECRET_THINK"},
                        {"type": "text", "text": "Block A"},
                        {"type": "tool_use", "name": "repl", "input": {}},
                        {"type": "text", "text": "Block B"},
                    ],
                },
            ],
        }]
        c_rows, c_gaps = read_source("claude", json.dumps(claude_exp).encode("utf-8"), "conversations.json")
        self._check_rows(c_rows)
        self.assertEqual([(r["text"], r["locator"]) for r in c_rows], [("Block A", "claude:cl-1:cm-1/p1"), ("Block B", "claude:cl-1:cm-1/p3")])
        self.assertIn("non-current branch", "\n".join(c_gaps))
        for hidden in ("SECRET_THINK", "SECRET_ACC", "Alt branch"):
            self.assertNotIn(hidden, "\n".join(c_gaps))

        inseparable = "<system-reminder>HOOK_NOTE</system-reminder>\nUser question with U+2028:\u2028and NEL:\x85!"
        inline_lit = "Keep inline `<system-reminder>tag</system-reminder>` as user speech."
        cc_stream = _jsonl(
            {"type": "user", "uuid": "u-iso", "sessionId": "s-1", "message": {"role": "user", "content": "<system-reminder>ONLY_REMINDER</system-reminder>"}},
            {"type": "user", "uuid": "u-meta", "sessionId": "s-1", "isMeta": True, "message": {"role": "user", "content": "meta injection"}},
            {"type": "user", "uuid": "u-cs", "sessionId": "s-1", "isCompactSummary": True, "message": {"role": "user", "content": "COMPACT_SUMMARY_SECRET"}},
            {"type": "user", "uuid": "u-tool", "sessionId": "s-1", "message": {"role": "user", "content": [{"type": "tool_result", "content": "TOOL_OUT"}]}},
            {"type": "user", "uuid": "u-mix", "sessionId": "s-1", "message": {"role": "user", "content": inseparable}},
            {"type": "user", "uuid": "u-lit", "sessionId": "s-1", "message": {"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>SEP_BLOCK</system-reminder>"},
                {"type": "text", "text": inline_lit},
            ]}},
            {"type": "assistant", "uuid": "a-1", "sessionId": "s-1", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "HIDDEN_THOUGHT"},
                {"type": "text", "text": "Answer."},
            ]}},
        )
        cc_rows, cc_gaps = read_source("claude-code", cc_stream, "/Users/private/sess.jsonl")
        self._check_rows(cc_rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in cc_rows],
            [("unknown", inseparable, "claude-code:s-1:u-mix/p0"), ("user", inline_lit, "claude-code:s-1:u-lit/p1"), ("assistant", "Answer.", "claude-code:s-1:a-1/p1")],
        )
        cc_gap_blob = "\n".join(cc_gaps)
        self.assertIn("compact summary", cc_gap_blob)
        for hidden in ("/Users/private", "COMPACT_SUMMARY_SECRET", "HIDDEN_THOUGHT", "TOOL_OUT"):
            self.assertNotIn(hidden, cc_gap_blob)

    def test_vendor_export_zip_reads_only_its_conversations_member(self) -> None:
        """An uploaded ChatGPT/Claude official export ZIP is read in memory: its one main
        conversations.json member, with the same item identities as the plain JSON path."""
        chatgpt_doc = [{
            "id": "cg-zip", "title": "Zip chat", "current_node": "a1",
            "mapping": {
                "r": {"id": "r", "message": None, "parent": None},
                "u1": {"id": "u1", "parent": "r", "message": {"id": "u1", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Zipped question"]}}},
                "a1": {"id": "a1", "parent": "u1", "message": {"id": "a1", "author": {"role": "assistant"}, "create_time": 1700000200.0, "content": {"content_type": "text", "parts": ["Zipped answer"]}}},
            },
        }]
        chatgpt_json = json.dumps(chatgpt_doc, ensure_ascii=False).encode("utf-8")
        plain_rows, _ = read_source("chatgpt", chatgpt_json, "conversations.json")

        export = _zip(
            ("chat.html", b"<html>SECRET_ASSET</html>"),
            ("user.json", b'{"email": "SECRET_ASSET@example.invalid"}'),
            ("conversations.json", chatgpt_json),
            ("attachments/blob.png", b"\x89PNG\r\n\x1a\nSECRET_ASSET"),
        )
        zip_rows, zip_gaps = read_source("chatgpt", export, "chatgpt-export.zip")
        self._check_rows(zip_rows)
        self.assertEqual(zip_rows, plain_rows)
        self.assertTrue(any("3 other archive member(s) were not read" in gap for gap in zip_gaps))
        self.assertNotIn("SECRET_ASSET", "\n".join(zip_gaps))

        nested = _zip(("ChatGPT-export/conversations.json", chatgpt_json), ("chat.html", b"SECRET_ASSET"))
        nested_rows, _ = read_source("chatgpt", nested, "chatgpt-export.zip")
        self.assertEqual(nested_rows, plain_rows)

        claude_doc = [{"uuid": "cl-zip", "current_leaf_message_uuid": "cm-1", "chat_messages": [
            {"uuid": "cm-1", "parent_message_uuid": "00000000-0000-4000-8000-000000000000", "sender": "human",
             "created_at": "2026-11-01T10:00:00Z", "text": "Zipped human turn"},
        ]}]
        claude_zip = _zip(("conversations.json", json.dumps(claude_doc).encode("utf-8")))
        claude_rows, claude_gaps = read_source("claude", claude_zip, "claude-export.zip")
        self._check_rows(claude_rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"], r["date"]) for r in claude_rows],
            [("user", "Zipped human turn", "claude:cl-zip:cm-1/p0", "2026-11-01T10:00:00Z")],
        )
        self.assertEqual(claude_gaps, [])

        second = [{"id": "cg-zip-2", "mapping": {
            "r": {"id": "r", "message": None, "parent": None},
            "u2": {"id": "u2", "parent": "r", "message": {"id": "u2", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Second zipped question"]}}},
        }}]
        # A nested member of the same name is a stranger beside the main one:
        # it is not read, and none of its content reaches rows or gaps.
        merged = _zip(("conversations.json", chatgpt_json),
                      ("backup/conversations.json", json.dumps(second).encode("utf-8")))
        merged_rows, merged_gaps = read_source("chatgpt", merged, "chatgpt-export.zip")
        self.assertEqual([r["text"] for r in merged_rows], ["Zipped question", "Zipped answer"])
        self.assertTrue(any("backup/conversations.json" in gap and "were not read" in gap
                            for gap in merged_gaps))
        self.assertNotIn("Second zipped question", json.dumps(merged_rows, ensure_ascii=False))
        self.assertNotIn("Second zipped question", "\n".join(merged_gaps))

        for unrecognized in (_zip(), _zip(("chat.html", b"<html>only</html>")),
                             _zip(("users.json", b"{}"), ("export/conversations-notes.json", b"[]"))):
            with self.assertRaises(ValueError) as caught:
                read_source("chatgpt", unrecognized, "export.zip")
            self.assertIn("conversations.json", str(caught.exception))
        for other_kind in ("claude-code", "codex", "hermes", "text", "summary"):
            with self.assertRaises(ValueError):
                read_source(other_kind, _zip(("conversations.json", chatgpt_json)), "export.zip")
        for broken in (b"PK\x03\x04truncated",
                       _zip(("conversations.json", b"\xff\xfe\x00")),
                       _zip(("conversations.json", b"   \n ")),
                       _zip(("conversations.json", json.dumps(claude_doc).encode("utf-8")))):
            with self.assertRaises(ValueError):
                read_source("chatgpt", broken, "export.zip")

    def test_zip_with_indistinguishable_members_refuses_and_reads_no_body(self) -> None:
        """Two conversations.json members at the same depth: none is read; the
        refusal lists them for an explicit choice, and no body was parsed."""
        twins = _zip(("a/conversations.json", "НЕ_ЧИТАТЬ_ОДИН"),
                     ("b/conversations.json", "НЕ_ЧИТАТЬ_ДВА"))
        with self.assertRaises(ValueError) as raised:
            read_source("chatgpt", twins, "export.zip")
        message = str(raised.exception)
        self.assertIn("a/conversations.json", message)
        self.assertIn("b/conversations.json", message)
        self.assertIn("pass the one you mean", message)
        self.assertNotIn("НЕ_ЧИТАТЬ", message)

    def test_oversized_exports_are_refused_before_reading_with_a_named_limit(self) -> None:
        """Input over the reader's byte limit is refused before it is read, with
        the limit named and a way to hand over a smaller file; below it, the same
        export reads as before."""
        doc = [{
            "id": "cg-mini", "title": "Miniature", "current_node": "n-1",
            "mapping": {
                "root": {"id": "root", "message": None, "parent": None, "children": ["n-1"]},
                "n-1": {"id": "n-1", "parent": "root", "message": {
                    "id": "m-1", "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Факт."]}}},
            },
        }]
        export = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        saved = readers._MAX_EXPORT_BYTES
        try:
            readers._MAX_EXPORT_BYTES = 10
            with self.assertRaises(ValueError) as plain:
                read_source("chatgpt", export, "conversations.json")
            self.assertIn("over the 10-byte limit", str(plain.exception))
            self.assertIn("pass a smaller file", str(plain.exception))
            with self.assertRaises(ValueError) as zipped:
                read_source("chatgpt", _zip(("conversations.json", export)), "export.zip")
            self.assertIn("unpacks to", str(zipped.exception))
            self.assertIn("over the 10-byte limit", str(zipped.exception))
        finally:
            readers._MAX_EXPORT_BYTES = saved
        rows, _ = read_source("chatgpt", export, "conversations.json")
        self.assertEqual([row["text"] for row in rows], ["Факт."])

    def test_codex_scoped_mirror_response_fallback_metadata_and_strict_recognition(self) -> None:
        with self.assertRaises(ValueError):
            read_source("codex", _jsonl({"id": "not-codex", "timestamp": "2026-11-01T00:00:00Z"}), "x.jsonl")

        rollout = _jsonl(
            {"timestamp": "2026-11-15T12:00:00Z", "type": "session_meta", "payload": {"id": "cx-9", "cwd": "/secret/cwd"}},
            {"timestamp": "2026-11-15T12:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Dev prompt"}]}},
            {"timestamp": "2026-11-15T12:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "continue"}},
            {"timestamp": "2026-11-15T12:00:01Z", "type": "turn_context", "payload": {"cwd": "/secret/cwd"}},
            {"timestamp": "2026-11-15T12:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context><cwd>/secret/cwd</cwd></environment_context>"}]}},
            {"timestamp": "2026-11-15T12:00:01Z", "type": "response_item", "payload": {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}},
            {"timestamp": "2026-11-15T12:00:02Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "SECRET_REASON"}]}},
            {"timestamp": "2026-11-15T12:00:02Z", "type": "response_item", "payload": {"type": "message", "id": "a-an", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "Internal note"}]}},
            {"timestamp": "2026-11-15T12:00:02Z", "type": "event_msg", "payload": {"type": "agent_message", "channel": "analysis", "message": "Internal note"}},
            {"timestamp": "2026-11-15T12:00:03Z", "type": "response_item", "payload": {"type": "message", "id": "a1", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "Step 1."}]}},
            {"timestamp": "2026-11-15T12:00:03Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "Step 1."}},
            {"timestamp": "2026-11-15T12:01:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "continue"}},
            {"timestamp": "2026-11-15T12:01:00Z", "type": "response_item", "payload": {"type": "message", "id": "m2", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}},
            {"timestamp": "2026-11-15T12:01:01Z", "type": "response_item", "payload": {"type": "message", "id": "a2-an", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "Second internal note"}]}},
            {"timestamp": "2026-11-15T12:01:02Z", "type": "response_item", "payload": {"type": "message", "id": "a2", "role": "assistant", "content": [
                {"type": "output_text", "text": "Analysis: prose stays literal."},
                {"type": "output_text", "text": "Fallback part 1."},
            ]}},
        )
        rows, gaps = read_source("codex", rollout, "rollout.jsonl")
        self._check_rows(rows)
        # "Step 1." is in the file twice: its response_item (line 10) comes
        # first, so that is the record kept and addressed; the event_msg mirror
        # on line 11 is deduplicated. The user turn keeps its event row, the
        # earlier line of that pair.
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [
                ("user", "continue", "codex:cx-9:L3/p0"), ("assistant", "Step 1.", "codex:cx-9:a1/p0"),
                ("user", "continue", "codex:cx-9:L12/p0"), ("assistant", "Analysis: prose stays literal.", "codex:cx-9:a2/p0"),
                ("assistant", "Fallback part 1.", "codex:cx-9:a2/p1"),
            ],
        )
        cx_gap_blob = "\n".join(gaps)
        self.assertIn("reasoning", cx_gap_blob)
        for hidden in ("/secret/cwd", "SECRET_REASON", "Dev prompt", "Internal note", "Second internal note"):
            self.assertNotIn(hidden, cx_gap_blob)
        # Mirrored duplicates are deduplicated, not omitted: the count rides
        # Gaps.notes, and the gap lines carry only content actually left unread.
        self.assertTrue(any("deduplicated 3 mirrored duplicate record(s)" in n for n in gaps.notes))
        self.assertNotIn("mirrored", cx_gap_blob)
        self.assertNotIn("unmirrored", cx_gap_blob)
        for hidden in ("Dev prompt", "Internal note", "Second internal note"):
            self.assertNotIn(hidden, "\n".join(gaps.notes))

        an_only, an_gaps = read_source(
            "codex",
            _jsonl({"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "Internal note"}]}}),
            "only_analysis.jsonl",
        )
        self.assertEqual(an_only, [])
        self.assertTrue(any("reasoning" in g for g in an_gaps))
        self.assertNotIn("Internal note", "\n".join(an_gaps))

    def test_hermes_two_session_export_and_memory_files_plus_text_summary(self) -> None:
        hermes_export = _jsonl(
            {
                "id": "hs-alpha", "title": "First", "system_prompt": "SYS_SECRET", "timings": {"total": 1.2},
                "lineage": [{"id": "old-seg", "messages": [{"id": 1, "role": "user", "content": "Duplicate lineage copy", "active": 1, "compacted": 0}]}],
                "messages": [
                    {"id": 0, "role": "user", "content": "AUTOGEN_SUMMARY_SECRET", "_compressed_summary": True, "timestamp": 1699999999, "active": 1, "compacted": 0},
                    {"id": 1, "role": "user", "content": "Older compacted user turn", "timestamp": 1700000000, "active": 0, "compacted": 1},
                    {"id": 2, "role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}], "reasoning": "THINK_SECRET", "timestamp": 1700000001, "active": 1, "compacted": 0},
                    {"id": 3, "role": "tool", "content": "TOOL_SECRET", "timestamp": 1700000002, "active": 1, "compacted": 0},
                    {"id": 4, "role": "user", "content": "Undone turn", "timestamp": 1700000003, "active": 0, "compacted": 0},
                    {"id": 5, "role": "assistant", "content": [{"type": "text", "text": "Active answer"}], "timestamp": 1700000004, "active": 1, "compacted": 0},
                ],
            },
            {
                "id": "hs-beta",
                "messages": [{"id": 1, "role": "user", "content": "Same msg id=1 in second session", "timestamp": 1700000500.5, "active": 1, "compacted": 0}],
            },
        )
        h_rows, h_gaps = read_source("hermes", hermes_export, "history.jsonl")
        self._check_rows(h_rows)
        self.assertEqual(
            [(r["conversation"], r["speaker"], r["text"], r["locator"]) for r in h_rows],
            [
                ("hs-alpha", "user", "Older compacted user turn", "hermes:hs-alpha:1/p0"),
                ("hs-alpha", "assistant", "Active answer", "hermes:hs-alpha:5/p0"),
                ("hs-beta", "user", "Same msg id=1 in second session", "hermes:hs-beta:1/p0"),
            ],
        )
        h_gap_blob = "\n".join(h_gaps)
        self.assertIn("compressed summary", h_gap_blob)
        self.assertIn("undone", h_gap_blob)
        self.assertIn("tool", h_gap_blob)
        for hidden in ("SYS_SECRET", "THINK_SECRET", "TOOL_SECRET", "Undone turn", "Duplicate lineage copy", "AUTOGEN_SUMMARY_SECRET"):
            self.assertNotIn(hidden, h_gap_blob)

        mem_rows, mem_gaps = read_source("hermes", b"# Memory\n- Fact 1\n", "/dir/MEMORY.md")
        self.assertEqual((mem_rows[0]["speaker"], mem_rows[0]["conversation"], mem_gaps), ("document", "MEMORY.md", []))
        for bad_hermes in ("state.db", "config.yaml", "SOUL.md", "notes.md"):
            with self.assertRaises(ValueError):
                read_source("hermes", b"# Markdown\n", bad_hermes)

        plain_rows, plain_gaps = read_source("text", b"plain text", "note.md")
        self._check_rows(plain_rows)
        self.assertEqual((plain_rows[0]["speaker"], plain_rows[0]["text"], plain_rows[0]["conversation"], plain_rows[0]["locator"], plain_gaps), ("document", "plain text", "note.md", "text:note.md:doc/p0", []))
        t_rows, _ = read_source("text", b"Verbatim doc", "/Users/u/doc.md")
        slash_rows, _ = read_source("text", b"Root label doc", "/")
        s_rows, _ = read_source("summary", b"Prior agent summary", "handoff.md")
        self.assertEqual((t_rows[0]["speaker"], t_rows[0]["conversation"]), ("document", "doc.md"))
        self.assertEqual((slash_rows[0]["speaker"], slash_rows[0]["conversation"]), ("document", "text"))
        self.assertEqual((s_rows[0]["speaker"], s_rows[0]["conversation"]), ("assistant", "handoff.md"))

    def test_pasted_summary_and_text_carry_verbatim_without_a_content_filter(self) -> None:
        """The person's own choice: literal pasted material is never filtered by keyword.
        A summary may itself be JSON, or quote harness markup from the other side."""
        markup = ("My previous assistant wrote this down:\n"
                  "<system-reminder>example marker quoted from the other harness</system-reminder>\n"
                  "Keep the quote intact — it is an example, not an instruction.")
        rows, gaps = read_source("summary", markup.encode("utf-8"), "handoff.md")
        self._check_rows(rows)
        self.assertEqual((rows[0]["speaker"], rows[0]["text"], rows[0]["conversation"], gaps),
                         ("assistant", markup, "handoff.md", []))

        as_json = json.dumps({"summary": "Prior assistant state", "note": "café «quoted» \u2028 line"}, ensure_ascii=False)
        json_rows, json_gaps = read_source("summary", as_json.encode("utf-8"), "summary.json")
        self.assertEqual((json_rows[0]["speaker"], json_rows[0]["text"], json_gaps), ("assistant", as_json, []))

        jsonl = '{"turn": 1, "text": "one"}\n{"turn": 2, "text": "two"}\n'
        jsonl_rows, _ = read_source("summary", jsonl.encode("utf-8"), "summary.jsonl")
        self.assertEqual((jsonl_rows[0]["text"], jsonl_rows[0]["conversation"]), (jsonl, "summary.jsonl"))

        text_rows, _ = read_source("text", markup.encode("utf-8"), "pasted.txt")
        self.assertEqual((text_rows[0]["speaker"], text_rows[0]["text"]), ("document", markup))

    @staticmethod
    def _mock_zip(entries: list[SimpleNamespace], stream_bytes: bytes = b"{}") -> tuple[object, MagicMock]:
        open_mock = MagicMock(side_effect=lambda _info: io.BytesIO(stream_bytes))

        class FakeArchive:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def infolist(self):
                return entries

            open = open_mock

        return FakeArchive(), open_mock

    def test_zip_with_duplicate_same_name_members_refuses_and_reads_no_body(self) -> None:
        """Duplicate entries with the exact same name must not collapse in a set."""
        dup_entries = [
            SimpleNamespace(filename="conversations.json", flag_bits=0, compress_type=zipfile.ZIP_STORED,
                            file_size=10, is_dir=lambda: False),
            SimpleNamespace(filename="conversations.json", flag_bits=0, compress_type=zipfile.ZIP_STORED,
                            file_size=10, is_dir=lambda: False),
        ]
        archive, open_mock = self._mock_zip(dup_entries)
        with patch.object(readers.zipfile, "ZipFile", return_value=archive):
            with self.assertRaises(ValueError) as raised:
                read_source("chatgpt", b"PK\x03\x04stub", "export.zip")
        self.assertIn("2 conversations.json member(s)", str(raised.exception))
        open_mock.assert_not_called()

    def test_zip_enforces_streaming_byte_limit_when_header_size_is_small(self) -> None:
        """A stream that yields past the limit is stopped mid-read even if file_size is small."""
        entry = SimpleNamespace(
            filename="conversations.json",
            flag_bits=0,
            compress_type=zipfile.ZIP_STORED,
            file_size=5,
            is_dir=lambda: False,
        )
        payload = b'[{"mapping":{},"padding":"' + b"a" * 60 + b'"}]'
        archive, open_mock = self._mock_zip([entry], stream_bytes=payload)
        with patch.object(readers, "_MAX_EXPORT_BYTES", 50), patch.object(readers.zipfile, "ZipFile", return_value=archive):
            with self.assertRaises(ValueError) as raised:
                read_source("chatgpt", b"PK\x03\x04stub", "export.zip")
        message = str(raised.exception)
        self.assertIn("unpacks to", message)
        self.assertIn("over the 50-byte limit", message)
        open_mock.assert_called_once()

    def test_zip_refuses_encrypted_and_alternative_compression_methods(self) -> None:
        """Encrypted entries and non-STORED/DEFLATED compression fail clearly without password prompts."""
        enc_entry = SimpleNamespace(
            filename="conversations.json",
            flag_bits=0x1,
            compress_type=zipfile.ZIP_STORED,
            file_size=10,
            is_dir=lambda: False,
        )
        enc_archive, enc_open = self._mock_zip([enc_entry])
        with patch.object(readers.zipfile, "ZipFile", return_value=enc_archive):
            with self.assertRaises(ValueError) as enc:
                read_source("chatgpt", b"PK\x03\x04stub", "encrypted.zip")
        self.assertIn("encrypted", str(enc.exception))
        enc_open.assert_not_called()

        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, 93):
            alt_entry = SimpleNamespace(
                filename="conversations.json",
                flag_bits=0,
                compress_type=method,
                file_size=10,
                is_dir=lambda: False,
            )
            alt_archive, alt_open = self._mock_zip([alt_entry])
            with patch.object(readers.zipfile, "ZipFile", return_value=alt_archive):
                with self.assertRaises(ValueError) as comp:
                    read_source("chatgpt", b"PK\x03\x04stub", "alt.zip")
            self.assertIn("unsupported compression method", str(comp.exception))
            alt_open.assert_not_called()

    def test_duplicate_locators_are_disambiguated_deterministically(self) -> None:
        dup_sessions = _jsonl(
            {"id": "hs-dup", "messages": [{"id": 1, "role": "user", "content": "First", "active": 1, "compacted": 0}]},
            {"id": "hs-dup", "messages": [{"id": 1, "role": "user", "content": "Second", "active": 1, "compacted": 0}]},
        )
        rows, gaps = read_source("hermes", dup_sessions, "history.jsonl")
        self._check_rows(rows)
        self.assertEqual([r["locator"] for r in rows], ["hermes:hs-dup:1/p0", "hermes:hs-dup:1/p0@2"])
        # The @N suffix note is bookkeeping about kept rows: a non-loss notice.
        self.assertIsInstance(gaps, readers.Gaps)
        self.assertEqual(list(gaps), [])
        self.assertTrue(any("disambiguated 1 duplicate locator(s)" in n for n in gaps.notes))
        self.assertNotIn("disambiguated", "\n".join(gaps))

    def test_codex_incomplete_export_keeps_last_response_item_reply(self) -> None:
        """A normal export cut mid-turn keeps the human-visible last reply that
        appears only in response_item: actual event mirrors are deduplicated
        (counted in notes, never a gap), and unmirrored items stay visible
        beside the kept record of their turn, parts and file order intact."""
        truncated = _jsonl(
            {"type": "session_meta", "payload": {"id": "cx-cut", "cwd": "/secret/cwd"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Ship the widget."}},
            {"type": "response_item", "payload": {"type": "message", "id": "u-mirror", "role": "user",
                                                  "content": [{"type": "input_text", "text": "Ship the widget."}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "SECRET_PLAN"}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "a-last", "role": "assistant", "content": [
                {"type": "output_text", "text": "Half a reply; the export cut mid-turn."},
                {"type": "output_text", "text": " Second part kept whole."},
            ]}},
        )
        rows, gaps = read_source("codex", truncated, "cut.jsonl")
        self._check_rows(rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [
                ("user", "Ship the widget.", "codex:cx-cut:L2/p0"),
                ("assistant", "Half a reply; the export cut mid-turn.", "codex:cx-cut:a-last/p0"),
                ("assistant", " Second part kept whole.", "codex:cx-cut:a-last/p1"),
            ],
        )
        self.assertIn("omitted 1 reasoning item(s)", "\n".join(gaps))
        self.assertTrue(any("deduplicated 1 mirrored duplicate record(s)" in n for n in gaps.notes))
        self.assertNotIn("mirrored", "\n".join(gaps))
        for hidden in ("SECRET_PLAN", "/secret/cwd"):
            self.assertNotIn(hidden, "\n".join(gaps))
            self.assertNotIn(hidden, "\n".join(gaps.notes))

    def test_codex_equal_items_pair_one_mirror_each(self) -> None:
        """Equal text in one turn: each event row covers one response item, so
        the equal items left over are genuine repeats and stay visible, with
        their own parts and locators."""
        rollout = _jsonl(
            {"type": "session_meta", "payload": {"id": "cx-eq"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Ship the widget."}},
            {"type": "response_item", "payload": {"type": "message", "id": "u-1", "role": "user",
                                                  "content": [{"type": "input_text", "text": "Ship the widget."}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "u-2", "role": "user",
                                                  "content": [{"type": "input_text", "text": "Ship the widget."}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done."}},
            {"type": "response_item", "payload": {"type": "message", "id": "a-1", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Done."}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "a-2", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Done."}]}},
        )
        rows, gaps = read_source("codex", rollout, "equal.jsonl")
        self._check_rows(rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [
                ("user", "Ship the widget.", "codex:cx-eq:L2/p0"),
                ("user", "Ship the widget.", "codex:cx-eq:u-2/p0"),
                ("assistant", "Done.", "codex:cx-eq:L5/p0"),
                ("assistant", "Done.", "codex:cx-eq:a-2/p0"),
            ],
        )
        self.assertTrue(any("deduplicated 2 mirrored duplicate record(s)" in n for n in gaps.notes))
        self.assertNotIn("mirrored", "\n".join(gaps))

    def test_codex_partial_copy_keeps_the_first_seen_address_and_parts(self) -> None:
        """A copy that later adds the other record form of speech already read
        leaves every already-read row exactly where it was: the earlier line of
        each pair keeps its locator, parts and text — response_item before
        event_msg and the reverse alike — and the later one is deduplicated
        instead of appearing a second time."""
        head = [
            {"type": "session_meta", "payload": {"id": "cx-copy"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Ship the widget."}},
            {"type": "response_item", "payload": {"type": "message", "id": "u-1", "role": "user",
                                                  "content": [{"type": "input_text", "text": "Ship the widget."}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "a-1", "role": "assistant", "content": [
                {"type": "output_text", "text": "Part one."},
                {"type": "output_text", "text": "Part two."},
            ]}},
        ]
        added = [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Part one.\nPart two."}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "And the receipt?"}},
            {"type": "response_item", "payload": {"type": "message", "id": "u-2", "role": "user",
                                                  "content": [{"type": "input_text", "text": "And the receipt?"}]}},
        ]
        first_rows, first_gaps = read_source("codex", _jsonl(*head), "copy.jsonl")
        rows, gaps = read_source("codex", _jsonl(*(head + added)), "copy.jsonl")
        self._check_rows(rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in first_rows],
            [
                ("user", "Ship the widget.", "codex:cx-copy:L2/p0"),
                ("assistant", "Part one.", "codex:cx-copy:a-1/p0"),
                ("assistant", "Part two.", "codex:cx-copy:a-1/p1"),
            ],
        )
        # The speech the first read already carried keeps its exact rows: the
        # added mirrors bring only the newer turn, never a second copy of a row
        # and never a new address for an already-read reply.
        self.assertEqual(rows[:3], first_rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [
                ("user", "Ship the widget.", "codex:cx-copy:L2/p0"),
                ("assistant", "Part one.", "codex:cx-copy:a-1/p0"),
                ("assistant", "Part two.", "codex:cx-copy:a-1/p1"),
                ("user", "And the receipt?", "codex:cx-copy:L6/p0"),
            ],
        )
        self.assertTrue(any("deduplicated 1 mirrored duplicate record(s)" in n for n in first_gaps.notes))
        self.assertTrue(any("deduplicated 3 mirrored duplicate record(s)" in n for n in gaps.notes))
        self.assertNotIn("mirrored", "\n".join(gaps))

    def test_codex_equal_records_of_one_form_are_never_merged(self) -> None:
        """Two equal response items with one event mirror: the event covers one
        item, the other stays as a genuine repeat, and the pair keeps the
        first-seen item — a mirror is never merged into a repeat, and a
        developer item or a reasoning record stays out of the speech."""
        rollout = _jsonl(
            {"type": "session_meta", "payload": {"id": "cx-two"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                  "content": [{"type": "input_text", "text": "Dev prompt"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "SECRET_PLAN"}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "x-1", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Done."}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "x-2", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Done."}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done."}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Next."}},
        )
        rows, gaps = read_source("codex", rollout, "two.jsonl")
        self._check_rows(rows)
        self.assertEqual(
            [(r["speaker"], r["text"], r["locator"]) for r in rows],
            [
                ("assistant", "Done.", "codex:cx-two:x-1/p0"),
                ("assistant", "Done.", "codex:cx-two:x-2/p0"),
                ("user", "Next.", "codex:cx-two:L7/p0"),
            ],
        )
        self.assertIn("omitted 1 developer/system item(s)", "\n".join(gaps))
        self.assertIn("omitted 1 reasoning item(s)", "\n".join(gaps))
        self.assertTrue(any("deduplicated 1 mirrored duplicate record(s)" in n for n in gaps.notes))
        self.assertNotIn("mirrored", "\n".join(gaps))
        for hidden in ("Dev prompt", "SECRET_PLAN"):
            self.assertNotIn(hidden, "\n".join(gaps))
            self.assertNotIn(hidden, "\n".join(gaps.notes))


if __name__ == "__main__":
    unittest.main()
