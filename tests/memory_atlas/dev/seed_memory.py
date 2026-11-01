"""Synthetic memory written into an empty data root by Ouroboros's own producers. Development only.

Nothing here is the owner's memory. The words are a design story (``dev/fixtures/corpus.json``:
Mira, Lev, a greenhouse; invented for the design comparison) plus stress material that is plainly
labelled as test text. Every source goes through the producer that writes it in a real installation:

- legacy dialogue: the old writer's files, then ``dialogue_import.run_dialogue_import`` and
  ``ChronicleStore.ensure_activated`` (the one legacy import);
- Projects: ``projects_registry.create_project`` (room labels come from that registry);
- the chronicle: ``ChronicleStore.publish_page`` / ``publish_part`` / ``publish_account`` / ``correct`` /
  ``decide`` / ``select_account`` / ``mark`` / ``set_mark_view`` / ``release_mark`` / ``write_note``, and
  ``ChronicleStore.publish`` for bulk rows (the importer's own path);
- knowledge: ``knowledge.write_knowledge_note`` (versions, archive); one note is a hand-edited file
  with unreadable front matter, as an owner's edit can leave it;
- identity and World: ``memory_publication.publish_memory_file``; scratchpad:
  ``Memory.append_scratchpad_block``; a Project journal: ``project_journal.append_journal_milestone``;
  a workpad: the file a Project keeps.

Usage (the core checkout is imported from ``--core``; ``-I`` keeps the environment out):
  python3 -I -B dev/seed_memory.py --core <checkout> --data <empty data root> [--stress]
Prints a JSON summary of the ids it wrote.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
CORPUS = HERE / "fixtures" / "corpus.json"
MIND = {"kind": "mind", "task_id": "t-main", "focus": {"role": "main chat", "task_id": "t-main"}}
PROJECT_MIND = {"kind": "mind", "task_id": "b41e09", "focus": {"role": "project task", "task_id": "b41e09"}}
LIGHT = {"kind": "helper", "route": "configured-light"}
CHILD = {"kind": "helper", "task_id": "7c21", "focus": {"role": "child", "task_id": "7c21"}}
TEST_LABEL = "Synthetic test text, not anyone's memory."


def ok(result):
    if not result.ok:
        raise SystemExit(f"producer refused: {result}")
    return result.record


class Seeder:
    def __init__(self, root: pathlib.Path, core: pathlib.Path) -> None:
        from ouroboros.chronicle_store import ChronicleStore

        self.root, self.core = root, core
        self.store = ChronicleStore(root)
        self.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.ids: dict = {}
        self.span = 0

    def covers(self, n: int = 2) -> dict:
        rows = [f"row-{self.span + i}" for i in range(n)]
        cover = {"mode": "range", "rows": rows, "stream_span": [self.span, self.span + n - 1]}
        self.span += n
        return cover

    def text(self, record_id: str) -> str:
        return next(r for r in self.corpus["records"] if r["id"].startswith(record_id))["text"]

    # --- the legacy dialogue, imported once -------------------------------------------------------
    def legacy(self) -> None:
        from ouroboros.dialogue_import import run_dialogue_import
        from ouroboros.utils import jsonl_generation_signature

        rows = [{"chat_id": 1, "direction": d, "ts": f"2026-06-0{1 + i}T08:00:00+00:00",
                 "text": f"{TEST_LABEL} Dialogue row {i}."} for i, d in enumerate(["in", "out", "in", "out", "in"])]
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / "archive" / "chat_20260601T080000.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows[:3]), encoding="utf-8")
        (self.root / "logs" / "chat.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows[3:]), encoding="utf-8")
        legacy = [r for r in self.corpus["records"] if r["kind"] == "legacy"]
        blocks = [{"ts": f"2026-0{6 + n}-28T10:00:00+00:00", "type": "summary",
                   "range": f"2026-0{6 + n}-01 00:00 - 2026-0{6 + n}-28 00:00", "message_count": 1,
                   "content": f"### Block {n}", "rooms": [{"room_id": "1", "label": "Main", "message_count": 1,
                                                          "content": record["text"]}]}
                  for n, record in enumerate(legacy)]
        memory = self.root / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "dialogue_blocks.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
        meta = {"chat_log_signature": jsonl_generation_signature(self.root / "logs" / "chat.jsonl"),
                "last_consolidated_offset": 1}
        (memory / "dialogue_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        run_dialogue_import(self.root)
        receipt = self.store.ensure_activated(wait=True)
        if receipt.get("kind") in ("import_pending", "import_refused"):
            raise SystemExit(f"legacy import did not finish: {receipt}")
        self.ids["activation"] = receipt.get("id")

    # --- rooms and the chronicle ------------------------------------------------------------------
    def projects(self) -> None:
        from ouroboros.projects_registry import create_project

        for pid, name in (("garden-sensors", "Garden sensors"), ("release-flow", "Release flow"),
                          ("atlas-ru", "Атлас памяти")):
            self.ids["room:" + pid] = str(create_project(self.root, pid, name=name)["chat_id"])

    def chronicle(self) -> None:
        s, ids = self.store, self.ids
        garden, release, place = ids["room:garden-sensors"], ids["room:release-flow"], "5203"
        # Main
        p1 = ok(s.publish_page(room_id="1", text=self.text("550c6044"), covers=self.covers(), author=MIND))
        p2 = ok(s.publish_page(room_id="1", text=self.text("f6b5bb15"), covers=self.covers(), author=MIND))
        part = ok(s.publish_part(room_id="1", text=self.text("3bbb80b5"), member_ids=[p1["id"], p2["id"]],
                                 author=MIND, expected_sequence=s.room_head("1")))
        weekly = self.quoted_page()
        # Garden sensors
        loop = ok(s.publish_page(room_id=garden, text=self.text("a49bdce2"), covers=self.covers(), author=PROJECT_MIND))
        calib = ok(s.publish_page(room_id=garden, text=self.text("48a18ea7"), covers=self.covers(), author=CHILD))
        ok(s.decide(calib["id"], True, PROJECT_MIND, "Matches my own readings; keeping it as written."))
        gpart = ok(s.publish_part(room_id=garden, text=self.text("3b543791"), member_ids=[loop["id"], calib["id"]],
                                  author=PROJECT_MIND, expected_sequence=s.room_head(garden)))
        cadence = ok(s.publish_page(room_id=garden, text=self.text("584fe3e0"), covers=self.covers(), author=PROJECT_MIND))
        # Release flow and the place
        dry = ok(s.publish_page(room_id=release, text=self.text("a43b7714"), covers=self.covers(), author=PROJECT_MIND))
        wrong = ok(s.publish_page(room_id=release, text=self.text("fdbe6acb"), covers=self.covers(), author=LIGHT))
        shipped = ok(s.publish_page(room_id=release, text=self.text("3d8788f3"), covers=self.covers(), author=PROJECT_MIND))
        photos = ok(s.publish_page(room_id=place, text=self.text("76e77498"), covers=self.covers(), author=MIND))
        # The account cites its sources as they stand now; later corrections arrive after it.
        account = ok(s.publish_account(room_id="1", text=self.text("ac8bb7fb"),
                                       sources=[gpart["id"], cadence["id"], shipped["id"], photos["id"], weekly["id"]],
                                       author=MIND))
        ok(s.select_account(account["id"], replaces=[cadence["id"], dry["id"], photos["id"]], author=MIND,
                            reason="One account tells this lesson better than three pages that each hold a piece of it."))
        ok(s.decide(wrong["id"], False, PROJECT_MIND, "The host stamp says the upload failed; 0.4 shipped on Friday."))
        fix_weekly = ok(s.correct(weekly["id"], self.text("05310162"), MIND))
        ok(s.correct(cadence["id"], self.text("2e76df25"), PROJECT_MIND))
        ok(s.correct(loop["id"], self.text("6909a32d"), PROJECT_MIND))
        candidate = ok(s.publish_page(room_id=garden, text=self.text("d64a1764"), covers=self.covers(), author=CHILD,
                                      metadata={"candidate": True}))
        draft = ok(s.publish_page(room_id=place, text=self.text("3dea3e5f"), covers=self.covers(), author=LIGHT))
        relay = ok(s.publish_page(room_id=garden, text="Relay test log, Saturday: three clicks, no pump. " + TEST_LABEL,
                                  covers=self.covers(), author=LIGHT))
        draft_part = ok(s.publish_part(room_id=garden, text="Light folds the relay test log into one line. "
                                       + TEST_LABEL, member_ids=[relay["id"]], author=LIGHT,
                                       expected_sequence=s.room_head(garden)))
        # A second account shown, then withdrawn from My story.
        withdrawn = ok(s.publish_account(room_id=release, text="The release in one paragraph, tried and then withdrawn. "
                                         + TEST_LABEL, sources=[shipped["id"]], author=PROJECT_MIND))
        ok(s.select_account(withdrawn["id"], replaces=[shipped["id"]], author=PROJECT_MIND, reason="Try it in the story."))
        ok(s.select_account(withdrawn["id"], replaces=[], author=PROJECT_MIND, reason="The October account says it better.",
                            shown=False))
        # Marks: global, room, a view of its meaning only, one released.
        m1 = ok(s.mark({"kind": "chronicle", "id": p1["id"]}, self.text("7b05823d"), MIND, room_id="1", scope="global",
                       quote=self.text("550c6044").split(". ")[0], anchor={"tool": "schedule_followup"}))
        m2 = ok(s.mark({"kind": "chronicle", "id": cadence["id"]}, self.text("7ac1a707"), PROJECT_MIND, room_id=garden))
        m3 = ok(s.mark({"kind": "chronicle", "id": fix_weekly["id"]}, self.text("d5736bf9"), MIND, room_id="1"))
        ok(s.set_mark_view(m3["id"], "meaning", MIND, "The day is what matters; the wording can go."))
        m4 = ok(s.mark({"kind": "chronicle", "id": dry["id"]}, self.text("f9e9a4c4"), PROJECT_MIND, room_id=release))
        ok(s.release_mark(m4["id"], PROJECT_MIND, "The new key arrived on 20 October; the warning no longer applies."))
        note = ok(s.write_note(room_id="1", task_id="t-main", text="Ask Mira on Friday whether the offset can go.",
                               author=MIND))
        ids.update(p1=p1["id"], p2=p2["id"], part=part["id"], weekly=weekly["id"], fix_weekly=fix_weekly["id"],
                   loop=loop["id"], calib=calib["id"], gpart=gpart["id"], cadence=cadence["id"], dry=dry["id"],
                   wrong=wrong["id"], shipped=shipped["id"], photos=photos["id"], account=account["id"],
                   withdrawn=withdrawn["id"], candidate=candidate["id"], draft=draft["id"], draft_part=draft_part["id"],
                   mark_global=m1["id"], mark_room=m2["id"], mark_view=m3["id"], mark_released=m4["id"], note=note["id"])

    def quoted_page(self) -> dict:
        """The weekly page as the direct chat turn writes it: two chat rows stored by the dialogue writers' store,
        then ``chronicle_write`` (the tool handler) seals them and cites a person's words and Ouroboros's own,
        which the host checks against those rows before it publishes. Its covers carry the rows' time span."""
        from ouroboros import chat_chain, dialogue_store
        from ouroboros.task_results import write_task_result
        from ouroboros.tools.chronicle import _chronicle_write
        from ouroboros.tools.tool_context import ToolContext

        rows = [{"chat_id": 1, "direction": "in", "ts": "2026-10-24T07:55:00+00:00", "client_message_id": "seed-weekly-1",
                 "text": "Weekly is fine. Tuesday after lunch works better for Lev. " + TEST_LABEL},
                {"chat_id": 1, "direction": "out", "ts": "2026-10-24T08:05:00+00:00", "task_id": "t-weekly",
                 "text": "Noted: weekly from now on, and Lev signs the build by hand until the new signing key arrives. "
                         + TEST_LABEL}]
        for row in rows:
            dialogue_store.append(self.root, "chat", row)
        # The reply is the mind's by its task result (no row carries lineage in this synthetic chat).
        write_task_result(self.root, "t-weekly", "completed", result="Weekly release noted.")
        address = [chat_chain.format_address(chat_chain.row_address(row)) for row in rows]
        quotes = [{"address": address[0], "text": "Weekly is fine. Tuesday after lunch works better for Lev.", "speaker": "human"},
                  {"address": address[1], "text": "Lev signs the build by hand until the new signing key arrives.",
                   "speaker": "ouroboros"}]
        ctx = ToolContext(repo_dir=self.core, drive_root=self.root, task_id="t-main",
                          current_chat_id=1, is_direct_chat=True)
        answer = json.loads(_chronicle_write(ctx, kind="page", room_id="1", text=self.text("1c597f0f"),
                                             covers={"from": address[0], "to": address[1]}, quotes=quotes))
        if not answer.get("ok"):
            raise SystemExit(f"chronicle_write refused the quoted page: {answer}")
        self.ids.update(quote_rows=address, quotes=quotes)
        return self.store.get(answer["node_id"])

    # --- knowledge, identity, plain sources --------------------------------------------------------
    def knowledge(self) -> None:
        from ouroboros.knowledge import resolve_knowledge_address, write_knowledge_note

        for note in self.corpus["knowledge"]:
            if note["parse_error"]:
                path = self.root / "memory" / "knowledge" / (note["topic"] + ".md")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(note["raw"], encoding="utf-8")  # an owner's hand edit: front matter unreadable
                continue
            address = resolve_knowledge_address(self.root, note["topic"], note["scope"])
            revision = None
            versions = note.get("versions") or [{"text": note["raw"]}]
            # An archived note's last stored version carries the archive itself: archive mode writes that.
            versions = [v for v in versions if not v["text"].split("\n---\n", 1)[0].count("\narchive")]
            for version in versions:
                result = write_knowledge_note(address, version["text"], writer="turn",
                                              **({"expected_revision": revision} if revision else {}))
                if not result.ok:
                    raise SystemExit(f"knowledge refused {note['topic']}: {result}")
                revision = result.current.revision
            if note["state"] == "archived":
                result = write_knowledge_note(address, "", mode="archive", expected_revision=revision,
                                              reason=note["archive"]["reason"], writer="turn")
                if not result.ok:
                    raise SystemExit(f"archive refused {note['topic']}: {result}")

    def sources(self) -> None:
        from ouroboros.memory import Memory
        from ouroboros.memory_publication import publish_memory_file

        mem = Memory(self.root)
        author = {"task_id": "t-main", "focus": {"role": "main chat"}, "route": "test"}
        for version in self.corpus["identity"]["versions"]:
            current = mem.identity_path().read_bytes() if mem.identity_path().exists() else None
            import hashlib
            revision = hashlib.sha256(current).hexdigest() if current else None
            result = publish_memory_file(mem.identity_path(), revision, lambda _old, text=version["text"]: text,
                                         journal=mem.identity_journal_path(), author=author, source_type="task")
            if not result.ok:
                raise SystemExit(f"identity refused: {result}")
        world = self.root / "memory" / "WORLD.md"
        publish_memory_file(world, None, lambda _old: self.corpus["world"]["text"], journal=mem.world_journal_path(),
                            author=author, source_type="task")
        for block in self.corpus["scratchpad"]["blocks"]:
            mem.append_scratchpad_block(block["content"])

    def project_files(self) -> None:
        import ouroboros.config as config
        from ouroboros.tools.project_journal import append_journal_milestone

        config.DATA_DIR = self.root
        for pid, facts in self.corpus["projects"].items():
            for entry in facts["journal"]:
                append_journal_milestone(pid, "note", entry["text"], task_id="b41e09")
            (self.root / "projects" / pid / "workpad.md").write_text(facts["workpad"], encoding="utf-8")

    # --- stress: long lists, a text over 1 MiB, wide metadata, Unicode -----------------------------
    def stress(self) -> None:
        from ouroboros.knowledge import resolve_knowledge_address, write_knowledge_note

        s, room = self.store, self.ids["room:atlas-ru"]
        bulk = [{"id": f"stress-note-{n:03d}", "kind": "note", "room_id": room, "task_id": "t-stress", "author": MIND,
                 "text": f"Заметка {n:03d} ✦ {TEST_LABEL}"} for n in range(120)]
        ok(s.publish(bulk))
        long_text = ("Длинная страница для проверки чтения по частям. 🌱 Emoji outside the Basic Multilingual Plane. "
                     + TEST_LABEL + " ") * 8000
        wide = {f"поле_{n:02d}": f"значение {n}" for n in range(40)}
        wide["long_note"] = ("Метаданные длиннее одного предпросмотра. " + TEST_LABEL + " ") * 12
        cover = self.covers(30)
        big = ok(s.publish_page(room_id=room, text=long_text, covers=cover, author=MIND, metadata=wide))
        ok(s.correct(big["id"], "Исправление к длинной странице: вторая половина повторяет первую. " + TEST_LABEL, MIND))
        self.ids.update(big=big["id"], big_chars=len(long_text))
        scope = "project:atlas-ru"
        for n in range(64):
            topic = f"архив/раздел-{n // 16}/заметка-{n:02d}"
            summary = (f"Краткое описание заметки {n}. " + TEST_LABEL) if n % 5 else None
            front = f"---\ntitle: Заметка {n:02d} ✦\n" + (f"summary: {summary}\n" if summary else "") + "---\n"
            body = f"# Заметка {n:02d}\n\nСм. [первую](заметка-00.md). {TEST_LABEL}\n"
            result = write_knowledge_note(resolve_knowledge_address(self.root, topic, scope), front + body, writer="turn")
            if not result.ok:
                raise SystemExit(f"stress note refused: {result}")
        long_summary = ("Длинное авторское резюме, больше двух тысяч знаков. " + TEST_LABEL + " ") * 40
        # The same long summary over a short body: the whole summary reads without the large body below.
        result = write_knowledge_note(resolve_knowledge_address(self.root, "атлас/длинное-резюме", "global"),
                                      f"---\ntitle: Длинное резюме\nsummary: {long_summary}\n---\n# Длинное резюме\n\n"
                                      f"Короткий текст. {TEST_LABEL}\n", writer="turn")
        if not result.ok:
            raise SystemExit(f"long summary note refused: {result}")
        huge_body = ("## Раздел\n\nТекст большой заметки. " + TEST_LABEL + "\n\n") * 15000
        result = write_knowledge_note(resolve_knowledge_address(self.root, "большая/заметка", scope),
                                      f"---\ntitle: Большая заметка\nsummary: {long_summary}\n---\n# Большая заметка\n\n"
                                      + huge_body, writer="turn")
        if not result.ok:
            raise SystemExit(f"large note refused: {result}")
        self.ids["large_note_chars"] = len(result.current.text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True, type=pathlib.Path)
    ap.add_argument("--data", required=True, type=pathlib.Path)
    ap.add_argument("--stress", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, str(args.core.resolve()))
    root = args.data.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()) and (root / "memory" / "chronicle").exists():
        raise SystemExit("the data root already holds a chronicle")
    seeder = Seeder(root, args.core.resolve())
    seeder.legacy()
    seeder.projects()
    seeder.chronicle()
    seeder.knowledge()
    seeder.sources()
    seeder.project_files()
    if args.stress:
        seeder.stress()
    print(json.dumps(seeder.ids, ensure_ascii=False))


if __name__ == "__main__":
    main()
