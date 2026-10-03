"""Scholion — a local second-opinion layer over the owner's own medical data.

The analysis lives in the `scholion` pip package (declared in install_specs and
installed into this skill's isolated prefix); this file adapts its tools to the
frozen PluginAPI contract. One implementation, several doors — the package's
CLI, its local web app, the classic ouroboros/tools module and this Hub skill
all call the same engine facade, so the Hub skill cannot drift from the
product: there is nothing here to drift.

**This skill always runs out of process.** A skill that declares isolated
dependencies is cataloged and dispatched in a short-lived child, and this one
declares `install_specs`, so `register()` below runs again for every tool call,
every route call and every catalog pass. Two consequences are load-bearing:

- nothing here may be expensive, and nothing may assume it runs once. There is
  no «on enable» moment to hang initialisation on, and asking for one is asking
  for a hook the contract does not have;
- an environment variable exported here reaches the work done in the *same*
  child. That is how the profile is given a home below, without a settings
  panel and without the host having to learn anything about this skill.

Honesty about permissions, in one place:

- ``widget`` and ``route`` — the tab a person meets first. On this host nobody
  types a command: the skill arrives by a click into a container whose paths
  the owner has never seen. A skill that can register no surface can only
  answer questions, and the first question — «where do I put my files?» —
  is the one it could not answer at all before this tab existed.
- ``fs`` — the data directory is the one the host hands out, so the ordinary
  case writes nowhere else. Declared because the owner may point
  ``SCHOLION_REPO_DIR`` or a folder of laboratory forms anywhere they like,
  and then the package does write outside the skill's state directory.
- ``net`` — two named lookups, both opt-in by usage: resolving a drug
  that the local knowledge base does not carry (RxNorm/RxClass/CPIC, plus a
  translation service for non-Latin drug names) and rsID lookups (Ensembl);
  and a third, the package's name to PyPI at most once a day, to say whether a
  newer build is out. No key, no account, no telemetry; SCHOLION_OFFLINE=1
  disables all of it.
- ``subprocess`` — the package starts local programs when they are installed:
  ``bcftools`` for the genome, ``pdftotext`` / ``pdftoppm`` / ``tesseract`` for
  laboratory forms. It does NOT start pip here: this host installs the package,
  so ``register()`` says so in ``SCHOLION_MANAGED_BY`` and ``update`` names the
  host instead of installing into the host's interpreter (both found by the
  Hub's maintainer on review of PR #74, 19.09.2026).

Not a medical device: every answer is a second opinion for a conversation
with a physician, and the package's own safety rules travel inside it
(`scholion skill --rules`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

_PREFIX = "sch_"           # the classic tools-module namespace; the Hub host
_MAX_TIMEOUT = 120         # namespaces tools itself, so the prefix comes off

_TAB = "start"             # tab ids and route names are alnum/underscore only
_TARGET = "scholion"       # the poll target every live component reads from


# --- where the person's data lives ----------------------------------------

def _adopt_data_root(api: Any) -> Optional[Path]:
    """Give the data directory a home the host owns, unless the owner named one.

    Left alone, the package falls back to the platform data directory of
    whatever container it landed in — a correct path that the owner has no name
    for and no way to reach. The state directory is the one place the host
    promises to keep, so the layout goes there and «where is my data» has an
    answer that can be printed on the tab.

    Setting a variable is enough precisely because of the out-of-process rule
    above: every tool call and every route call passes through `register()`
    first, inside the same child that will then do the work.
    """
    if os.environ.get("SCHOLION_REPO_DIR") or os.environ.get("SCHOLION_PROFILE_DIR"):
        return _data_root()          # the owner has already decided; do not argue
    try:
        root = Path(api.get_state_dir()).expanduser() / "data"
    except Exception:
        return None
    os.environ["SCHOLION_REPO_DIR"] = str(root)
    return root


def _data_root() -> Optional[Path]:
    try:
        from scholion import core
        return core.repo_dir()
    except Exception:
        return None


def _profile_ready(root: Optional[Path]) -> bool:
    return bool(root) and (root / "profile" / "index.md").exists()


# --- what the tab shows ---------------------------------------------------

def _state_payload(root: Optional[Path]) -> Dict[str, Any]:
    """The live half of the tab. Read on demand, never during registration."""
    ready = _profile_ready(root)
    out: Dict[str, Any] = {
        "ready": ready,
        "needs_profile": not ready,
        "data_dir": str(root) if root else "unknown",
        "labs_dir": str(root / "raw" / "lab") if root else "unknown",
        "genome_dir": str(root / "genome") if root else "unknown",
        "markers": "—",
        "medications": "—",
        "genome": "—",
        "hint": (
            "No data directory yet. Press «Create the data directory» below: it "
            "writes empty templates and a README into every folder, and touches "
            "nothing that already exists."
        ),
    }
    if not ready:
        return out
    try:
        from scholion import engine
        o = engine.overview()
        out["markers"] = o.get("markers_total", 0)
        out["medications"] = o.get("medications_count", 0)
        # `genome` is a report and is always present; the product reads its
        # `ready` flag, and so does this. Testing the dict itself said
        # «connected» over an empty directory — the exact shape of wrong answer
        # this product exists to refuse.
        connected = bool((o.get("genome") or {}).get("ready"))
        out["genome"] = "connected" if connected else "not connected"
        gaps = o.get("genome_gaps") or []
        if not connected:
            out["hint"] = (
                "The directory is ready and empty. Drop laboratory PDFs into "
                f"{out['labs_dir']} and a full VCF (GRCh38, bgzipped, with its "
                f".tbi index) into {out['genome_dir']}; each folder holds a "
                "README that says what belongs in it."
            )
            out["needs_profile"] = False
        elif gaps:
            out["hint"] = "Genome connected. Loci still unread: " + ", ".join(str(g) for g in gaps[:6])
            out["needs_profile"] = False
        else:
            out["hint"] = ""
            out["needs_profile"] = False
    except Exception as exc:                     # a broken read is a fact too
        out["hint"] = f"The profile could not be read: {type(exc).__name__}: {exc}"
    return out


def _render(root: Optional[Path]) -> Dict[str, Any]:
    data_dir = str(root) if root else "the platform data directory"
    return {
        "kind": "declarative",
        "schema_version": 1,
        "span": 2,
        "components": [
            {
                "type": "poll", "id": "state", "target": _TARGET,
                "route": "state", "method": "GET",
                "auto_start": True, "interval_ms": 30000,
                "label": "Refresh",
            },
            {
                "type": "markdown", "id": "what",
                "text": (
                    "**Scholion** reads one person's genome, laboratory history, "
                    "prescriptions and wearable exports **against each other**, on "
                    "this machine. Every statement carries where it came from, and "
                    "a negative answer is qualified by coverage rather than implied "
                    "by silence.\n\n"
                    "_Not a medical device. Material for a conversation with a "
                    "physician, not a diagnosis and not a prescription._"
                ),
            },
            {
                "type": "callout", "id": "hint", "target": _TARGET,
                "tone": "info", "path": "hint",
            },
            {
                "type": "kv", "id": "where", "target": _TARGET,
                "title": "Your data",
                "fields": [
                    {"label": "Data directory", "path": "data_dir"},
                    {"label": "Laboratory forms go here", "path": "labs_dir"},
                    {"label": "Genome (VCF) goes here", "path": "genome_dir"},
                    {"label": "Lab markers loaded", "path": "markers"},
                    {"label": "Prescriptions", "path": "medications"},
                    {"label": "Genome", "path": "genome"},
                ],
            },
            {
                # Shown only while there is nothing there. A button offering to
                # create what exists is a small untruth, and the directory it
                # would create is the one holding the person's medical record.
                "type": "action", "id": "create", "target": _TARGET,
                "condition_key": "needs_profile",
                "route": "init", "method": "POST",
                "submit_label": "Create the data directory",
                "busy_label": "Creating…",
                "fields": [],
            },
            {
                "type": "markdown", "id": "howto",
                "text": (
                    "### Your first three files\n\n"
                    f"1. **Laboratory forms** — PDF or DOCX exactly as they arrived, into `{data_dir}/raw/lab/`. "
                    "Then ask the assistant to read them (`ingest_labs`).\n"
                    f"2. **Genome** — a full VCF against GRCh38, bgzipped, with its `.tbi` index, into `{data_dir}/genome/`. "
                    "A consumer array export is not a full VCF; `genome/README.md` in that folder says how to prepare one.\n"
                    f"3. **Wearable export** — the archive your watch's site gives you, into `{data_dir}/raw/wearables/`.\n\n"
                    "Nothing is uploaded anywhere. If your files already live in a folder of "
                    "their own, point at it below instead of copying them."
                ),
            },
            {
                "type": "form", "id": "folder", "target": _TARGET,
                "route": "folder", "method": "POST",
                "label": "Use a folder I already have",
                "submit_label": "Use this folder for laboratory forms",
                "fields": [
                    {"name": "path", "label": "Absolute path to the folder", "required": True},
                ],
            },
        ],
    }


# --- routes ---------------------------------------------------------------

def _json(payload: Dict[str, Any], status_code: int = 200):
    try:
        from starlette.responses import JSONResponse
        return JSONResponse(payload, status_code=status_code)
    except Exception:                            # a host that hands back plain data
        return payload


async def _payload_of(request) -> Dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _make_routes(api: Any, root: Optional[Path]):
    async def state(_request):
        return _json(_state_payload(root))

    async def init(_request):
        """Create the layout — because a person pressed a button.

        `init` is deliberately absent from the tool set and from the web API:
        the package's own contract says it «creates the profile directory — the
        person's decision, not a model's», and the web has no route because a
        web server that needs a profile cannot be the thing that makes one.
        Neither reason argues against this button. The host loads the skill
        whether or not a profile exists, and the hand on this button is the
        owner's.
        """
        try:
            from scholion import store
            out = store.init_profile()
        except Exception as exc:
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        out = dict(out or {})
        out.update(_state_payload(root))
        api.log("info", f"scholion: data directory prepared at {root}")
        return _json(out)

    async def folder(request):
        payload = await _payload_of(request)
        path = str(payload.get("path") or "").strip()
        if not path:
            return _json({"ok": False, "error": "a path is required"}, 400)
        try:
            from scholion import store
            # `labs_docs` and not `labs`, and the difference is not cosmetic. The
            # host reviews a skill against write-path confinement: writing into a
            # folder the person names, outside the state directory, is a critical
            # failure there. `set_source_folder` moves the domain's JSON into the
            # chosen folder for the domains that HAVE one — `labs`, `medications`,
            # `metrics` — and `labs_docs` has none, so this records the path and
            # writes nothing outside. Changing the domain here would quietly turn
            # a reader into a writer; a test holds it.
            out = store.set_source_folder("labs_docs", path)
        except Exception as exc:
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        out = dict(out or {})
        out.update(_state_payload(root))
        return _json(out)

    return state, init, folder


# --- registration ---------------------------------------------------------

def _register_tools(api: Any) -> int:
    from scholion.ouroboros_tools import get_tools  # installed via install_specs

    count = 0
    for entry in get_tools():
        name = entry.name[len(_PREFIX):] if entry.name.startswith(_PREFIX) else entry.name
        schema: Dict[str, Any] = dict(entry.schema.get("parameters") or {})
        timeout = int(getattr(entry, "timeout_sec", 60) or 60)
        api.register_tool(
            name,
            entry.handler,
            description=str(entry.schema.get("description", "")),
            schema=schema,
            timeout_sec=max(1, min(timeout, _MAX_TIMEOUT)),
        )
        count += 1
    return count


#: What `update` says instead of running pip. This skill runs in a child of the
#: HOST's interpreter, so «pip in the running interpreter» would install into
#: the host's Python rather than the skill's isolated prefix.
_MANAGED_BY = "Ouroboros Hub"


def register(api: Any) -> None:
    os.environ.setdefault("SCHOLION_MANAGED_BY", _MANAGED_BY)
    root = _adopt_data_root(api)
    tools = _register_tools(api)

    # The surfaces are guarded and the tools are not, on purpose. A tool that
    # cannot be registered is a capability the owner paid for and did not get,
    # and it should fail loudly. A surface that cannot be registered — an older
    # host, a narrower permission set than this manifest asks for — costs the
    # onboarding tab and nothing else, and taking the whole skill down over it
    # would trade thirty working tools for a panel.
    try:
        state, init, folder = _make_routes(api, root)
        api.register_route("state", state, methods=("GET",))
        api.register_route("init", init, methods=("POST",))
        api.register_route("folder", folder, methods=("POST",))
        api.register_ui_tab(_TAB, "Scholion", icon="heart", render=_render(root))
        api.log("info", f"scholion: {tools} tools, onboarding tab, data at {root}")
    except Exception as exc:
        api.log("warning", f"scholion: {tools} tools, no onboarding surface "
                           f"({type(exc).__name__}: {exc})")
