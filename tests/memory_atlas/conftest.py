"""Shared setup: the payload loaded as the host loads it, and one real Ouroboros host for consumer tests.

The real host (``dev/realhost.py``) is a copy of the core checkout named by ``ATLAS_CORE`` (one with PluginAPI
2.1 ``read_memory``, ``ouroboros/memory_reader.py``; there is no default), started isolated on loopback with
synthetic memory written by Ouroboros's own producers and this payload installed, enabled and reviewed in its
data root only. One host serves the whole session; it is started in a short directory under /tmp
(``ATLAS_RUN_ROOT`` names one instead) and stopped at the end, its logs kept there. ``ATLAS_HOST_ROOT`` attaches
to a host already started by ``python3 -I -B dev/realhost.py`` instead (tests that write memory then write into
that host).

Nothing here is skipped or faked. A run that selects tests needing the core or a browser stops before the first
test and says what to set or install when ``ATLAS_CORE`` (or ``ATLAS_HOST_ROOT``) is unset, names no core with
``read_memory``, or Playwright or starlette is missing (``pytest_collection_finish``). Hub CI runs the whole suite
against the core commit pinned in ``.github/workflows/catalog-check.yml``.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]   # the hub checkout
PAYLOAD = REPO / "skills" / "memory-atlas"
DEV = Path(__file__).resolve().parent / "dev"
CORE = os.environ.get("ATLAS_CORE", "")
PREFIX = "/api/extensions/memory-atlas/atlas/v2/"


def payload_module(name: str):
    if "memory_atlas_payload" not in sys.modules:
        pkg = types.ModuleType("memory_atlas_payload")
        pkg.__path__ = [str(PAYLOAD)]
        sys.modules["memory_atlas_payload"] = pkg
    return importlib.import_module("memory_atlas_payload." + name)


@pytest.fixture(scope="session")
def routes():
    return payload_module("atlas_routes")


@pytest.fixture(scope="session")
def reader_mod():
    return payload_module("atlas_reader")


@pytest.fixture(scope="session")
def real_host():
    if str(DEV) not in sys.path:
        sys.path.insert(0, str(DEV))
    from realhost import RealHost

    attach = os.environ.get("ATLAS_HOST_ROOT")
    if attach:
        yield RealHost.attach(Path(attach))
        return
    if not CORE:
        pytest.fail("set ATLAS_CORE to a core checkout with PluginAPI 2.1 read_memory (ouroboros/memory_reader.py)")
    core = Path(CORE)
    if not (core / "ouroboros" / "memory_reader.py").is_file():
        pytest.fail(f"ATLAS_CORE={CORE} is not a core checkout with PluginAPI 2.1 read_memory (no ouroboros/memory_reader.py)")
    root = Path(os.environ.get("ATLAS_RUN_ROOT") or tempfile.mkdtemp(prefix="atl-t", dir="/tmp"))
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
    host = RealHost(core, root)
    try:
        host.start()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="session")
def playwright_api():
    sync_api = importlib.import_module("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        yield p


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def pytest_collection_finish(session):
    """What the selected tests need is required, never skipped: anything missing stops the run before the first
    test and says what to set or install."""
    if session.config.option.collectonly:
        return
    needs = set().union(*(getattr(item, "fixturenames", ()) for item in session.items))
    missing = []
    if not _importable("starlette"):
        missing.append("install starlette (python -m pip install starlette)")
    if "real_host" in needs and not os.environ.get("ATLAS_HOST_ROOT"):
        if not CORE:
            missing.append("set ATLAS_CORE to a core checkout with PluginAPI 2.1 read_memory (ouroboros/memory_reader.py)")
        elif not (Path(CORE) / "ouroboros" / "memory_reader.py").is_file():
            missing.append(f"ATLAS_CORE={CORE} has no ouroboros/memory_reader.py: name a core with PluginAPI 2.1 read_memory")
    if "playwright_api" in needs and not _importable("playwright.sync_api"):
        missing.append("install Playwright and its browsers (python -m pip install playwright, then"
                       " python -m playwright install chromium webkit)")
    if missing:
        raise pytest.UsageError("tests/memory_atlas: " + "; ".join(missing) + ". The whole suite needs them (README,"
                                " Tests and widgets); Hub CI pins its core in .github/workflows/catalog-check.yml.")


LINKS_SCOPE, LINKS_TARGET, LINKS_MANY = "project:release-flow", "links-check/target", "links-check/many"


def ensure_many_links(host) -> None:
    """Two notes on the release-flow shelf, written by the knowledge producer: a target, and a note with 2,600 links
    whose one link into the target is the 2,501st, past what one links-in step reads. Idempotent."""
    host.producer(
        "from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address, write_knowledge_note\n"
        "def put(topic, text):\n"
        "    address = resolve_knowledge_address(root, topic, %r)\n"
        "    try:\n        revision = read_knowledge_note(address).revision\n    except FileNotFoundError:\n        revision = None\n"
        "    result = write_knowledge_note(address, text, writer='turn', **({'expected_revision': revision} if revision else {}))\n"
        "    assert result.ok or result.reason == 'unchanged', result\n"
        "label = \"Synthetic test text, not anyone's memory.\"\n"
        "put(%r, '---\\ntitle: Links target\\n---\\n# Links target\\n\\n' + label + '\\n')\n"
        "links = ''.join('- [n%%04d](none-%%04d.md)\\n' %% (n, n) for n in range(2500)) + '- [the target](target.md)\\n'"
        " + ''.join('- [m%%04d](more-%%04d.md)\\n' %% (n, n) for n in range(99))\n"
        "put(%r, '---\\ntitle: Many links\\n---\\n# Many links\\n\\n' + label + '\\n\\n' + links)\n"
        % (LINKS_SCOPE, LINKS_TARGET, LINKS_MANY))


def edit_many_links(host) -> str:
    """The many-links note edited while it is read, by the knowledge producer: its link into the target now comes first,
    then 2,600 links under new labels (``w0000`` …). ``ensure_many_links`` writes the first version back. Returns the
    note's revision now."""
    return host.producer(
        "from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address, write_knowledge_note\n"
        "address = resolve_knowledge_address(root, %r, %r)\n"
        "label = \"Synthetic test text, not anyone's memory.\"\n"
        "links = '- [the target](target.md)\\n' + ''.join('- [w%%04d](none-%%04d.md)\\n' %% (n, n) for n in range(2600))\n"
        "result = write_knowledge_note(address, '---\\ntitle: Many links\\n---\\n# Many links\\n\\n' + label + '\\n\\n' + links,"
        " writer='turn', expected_revision=read_knowledge_note(address).revision)\n"
        "assert result.ok, result\n"
        "print(read_knowledge_note(address).revision)\n" % (LINKS_MANY, LINKS_SCOPE)).strip().splitlines()[-1]


# Producer code shared by the fixtures below: a Project registered by the registry's own producer, and a note put
# on a shelf by the knowledge producer (written again only when its words differ).
PROJECT_AND_NOTE = (
    "from ouroboros.projects_registry import create_project\n"
    "from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address, write_knowledge_note\n"
    "def put(scope, topic, title, body=''):\n"
    "    address = resolve_knowledge_address(root, topic, scope)\n"
    "    try:\n        revision = read_knowledge_note(address).revision\n    except FileNotFoundError:\n        revision = None\n"
    "    text = '---\\ntitle: ' + title + '\\n---\\n# ' + title + \"\\n\\nSynthetic test text, not anyone's memory.\\n\" + body\n"
    "    result = write_knowledge_note(address, text, writer='turn', **({'expected_revision': revision} if revision else {}))\n"
    "    assert result.ok or result.reason == 'unchanged', result\n")

ORDER_SCOPE, ORDER_LONG, ORDER_SHORT, ORDER_TARGET = "project:links-order", "a/long", "a-short", "z-target"


def ensure_links_order(host) -> None:
    """A Project shelf of three notes: ``a/long`` (2,100 links, none into the target), ``a-short`` (one link into
    it) and ``z-target``. The core lists a shelf in path order, ``a/long`` first; as strings ``a-short`` sorts
    before ``a/long``. Idempotent."""
    host.producer(PROJECT_AND_NOTE + (
        "create_project(root, 'links-order', name='Links order')\n"
        "put(%r, %r, 'Order target')\n"
        "put(%r, %r, 'Short', '\\n- [the target](z-target.md)\\n')\n"
        "put(%r, %r, 'Long', '\\n' + ''.join('- [n%%04d](none-%%04d.md)\\n' %% (n, n) for n in range(2100)))\n"
        % (ORDER_SCOPE, ORDER_TARGET, ORDER_SCOPE, ORDER_SHORT, ORDER_SCOPE, ORDER_LONG)))


LATE_SCOPE = "project:late-overview"


def ensure_late_overview(host) -> None:
    """A Project shelf whose overview comes after 105 other notes (``earlier/note-000`` …) in the core's order,
    past the Knowledge view's first page of 100. Idempotent."""
    host.producer(PROJECT_AND_NOTE + (
        "create_project(root, 'late-overview', name='Late overview')\n"
        "for n in range(105):\n"
        "    put(%r, 'earlier/note-%%03d' %% n, 'Earlier note %%03d' %% n)\n"
        "put(%r, 'overview', 'Overview')\n" % (LATE_SCOPE, LATE_SCOPE)))


MANY_SHELVES = 104


def ensure_many_shelves(host) -> None:
    """104 more Projects (``shelf-check-000`` …), each with one note on its own shelf: with the seeded ones the
    shelves run past the overview's first page of 100. Idempotent."""
    host.producer(PROJECT_AND_NOTE + (
        "for n in range(%d):\n"
        "    pid = 'shelf-check-%%03d' %% n\n"
        "    create_project(root, pid, name='Shelf check %%03d' %% n)\n"
        "    put('project:' + pid, 'about', 'About shelf %%03d' %% n)\n" % MANY_SHELVES))


NESTED_PROJECT = "cursor-check"
# Producer code for a nested-changes set: the chronicle store, the mind as author, and that Project's own room.
CHRONICLE = (
    "from ouroboros.chronicle_store import ChronicleStore\n"
    "from ouroboros.projects_registry import create_project\n"
    "store = ChronicleStore(root)\n"
    "author = {'kind': 'mind', 'task_id': 't-main', 'focus': {'role': 'main chat', 'task_id': 't-main'}}\n"
    "label = \"Synthetic test text, not anyone's memory.\"\n"
    "room = str(create_project(root, %r, name='Cursor check')['chat_id'])\n"
    "def done(result):\n    assert result.ok, result\n    return result.record\n" % NESTED_PROJECT)


def make_nested_changes(host, corrections: int) -> dict:
    """In a Project room of its own: two pages, a part folding them, an account citing that part, then ``corrections``
    corrections of the two pages, in turn. A part keeps its members by id, so every correction is one of the account's
    source's nested changes. Each call writes a new set; returns its ids and each page's current revision."""
    out = host.producer(CHRONICLE + (
        "n = len(store.records(room, kinds=['page']))\n"
        "def cover(i):\n"
        "    return {'mode': 'range', 'rows': ['row-%%d' %% (950 + i), 'row-%%d' %% (951 + i)], 'stream_span': [950 + i, 951 + i]}\n"
        "p1 = done(store.publish_page(room_id=room, text='The first page the part folds. ' + label, covers=cover(2 * n), author=author))\n"
        "p2 = done(store.publish_page(room_id=room, text='The second page the part folds. ' + label, covers=cover(2 * n + 2), author=author))\n"
        "part = done(store.publish_part(room_id=room, text='A part folding the two pages. ' + label, member_ids=[p1['id'], p2['id']],"
        " author=author, expected_sequence=store.room_head(room)))\n"
        "account = done(store.publish_account(room_id=room, text='An account citing the part. ' + label, sources=[part['id']], author=author))\n"
        "revision = {p1['id']: p1['id'], p2['id']: p2['id']}\n"
        "for k in range(%d):\n"
        "    page = (p1, p2)[k %% 2]['id']\n"
        "    revision[page] = done(store.correct(page, 'Correction %%d of the page. ' %% k + label, author,"
        " expected_revision=revision[page]))['id']\n"
        "print(json.dumps({'room': room, 'pages': [p1['id'], p2['id']], 'part': part['id'], 'account': account['id'],"
        " 'revision': revision}))\n" % corrections))
    return json.loads(out.strip().splitlines()[-1])


def correct_page(host, fixture: dict, page: str) -> None:
    """One more correction of a page of a nested-changes set (``make_nested_changes``): one more nested change."""
    out = host.producer(CHRONICLE + (
        "fix = done(store.correct(%r, 'A correction written while the list is read. ' + label, author, expected_revision=%r))\n"
        "print(fix['id'])\n" % (page, fixture["revision"][page])))
    fixture["revision"][page] = out.strip().splitlines()[-1]
