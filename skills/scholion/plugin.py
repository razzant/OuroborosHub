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
- ``net`` — optional reference lookups send drug, gene, variant or score
  identifiers (RxNorm/RxClass/CPIC, Ensembl, PGS Catalog and a translation
  service for non-Latin drug names). Explicit reference preparation can
  download public catalogue files. PyPI is asked at most once a day whether
  a newer build is out. No Scholion key, account or telemetry;
  SCHOLION_OFFLINE=1 disables Scholion's network operations, not the host's
  model requests or dependency installation.
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
import json
import hashlib
import tempfile
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
    state = Path(api.get_state_dir()).resolve()
    os.environ["SCHOLION_HUB_STATE"] = str(state)
    # Route children have a minimal environment. Read this declared, nonsecret
    # preference through PluginAPI too, so tools, routes and the companion agree.
    settings = api.get_settings(["SCHOLION_OFFLINE"])
    saved = _connection(state)
    if not (os.environ.get("SCHOLION_REPO_DIR") or os.environ.get("SCHOLION_PROFILE_DIR")) and saved:
        os.environ.update(saved)
    if settings.get("SCHOLION_OFFLINE") not in (None, ""):
        os.environ["SCHOLION_OFFLINE"] = str(settings["SCHOLION_OFFLINE"])
    if os.environ.get("SCHOLION_REPO_DIR") or os.environ.get("SCHOLION_PROFILE_DIR"):
        return _data_root()          # the owner has already decided; do not argue
    try:
        root = Path(api.get_state_dir()).expanduser() / "data"
    except Exception:
        return None
    os.environ["SCHOLION_REPO_DIR"] = str(root)
    return root


_BOUND_ENV = ("SCHOLION_REPO_DIR", "SCHOLION_PROFILE_DIR", "SCHOLION_GENOME_DIR",
              "SCHOLION_GENOME_VCF", "SCHOLION_RAW_DIR", "SCHOLION_WORK_DIR",
              "SCHOLION_WORKSTATION", "SCHOLION_OFFLINE", "SCHOLION_LANG")


def _connection(state: Path) -> Dict[str, str]:
    try:
        data = json.loads((state / "connection.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or not data.get("SCHOLION_REPO_DIR"):
        raise ValueError("The saved Scholion connection is unreadable; it was not replaced.")
    return {k: str(v) for k, v in data.items() if k in _BOUND_ENV}


def _private_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".scholion-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _binding() -> Dict[str, str]:
    from scholion import core
    out = {k: os.environ[k] for k in _BOUND_ENV if k in os.environ}
    out["SCHOLION_REPO_DIR"] = str(core.repo_dir().resolve())
    # Pin the same profile even on a workstation with another active patient.
    out["SCHOLION_PROFILE_DIR"] = str(core.profile_dir().resolve())
    return out


def _binding_id(binding: Dict[str, str]) -> str:
    paths = {k: v for k, v in binding.items() if k not in ("SCHOLION_OFFLINE", "SCHOLION_LANG")}
    return hashlib.sha256(json.dumps(paths, sort_keys=True).encode()).hexdigest()


def _save_connection(binding: Dict[str, str]) -> None:
    state = Path(os.environ["SCHOLION_HUB_STATE"])
    saved = _connection(state)
    if saved and _binding_id(saved) != _binding_id(binding):
        raise ValueError("This installation is already connected to a profile. Its connection was preserved.")
    _private_json(state / "connection.json", binding)


def _data_root() -> Optional[Path]:
    try:
        from scholion import core
        return core.repo_dir()
    except Exception:
        return None


def _profile_ready(root: Optional[Path]) -> bool:
    from scholion import core
    return (core.profile_dir() / "index.md").exists()


def _guide_path() -> Path:
    from scholion import core
    base = core.repo_dir() if core.profile_dir() == core.repo_dir() / "profile" else core.profile_dir()
    return base / "START-HERE.md"


def _prepare_setup_notes(result: Dict[str, Any]) -> None:
    """Only the explicit setup action writes notes; existing user files survive."""
    from scholion import core, container
    if not os.environ.get("SCHOLION_PROFILE_DIR"):
        # Name the newly prepared container before an isolated tool's first
        # write, so the identity it reads is the identity its answer carries.
        container.ensure()
    genome_note = (
        "# Genetic files\n\nKeep your original genetic files here. An indexed VCF uses "
        "a .vcf.gz file and its .tbi index; the reference build must be known. "
        "Consumer-array exports remain in their original format until explicitly imported.\n\n"
        "Ask the assistant to run genome_status and capabilities before connecting or preparing "
        "inputs. Copying a file does not perform an import or start genotyping. "
        "An array or exome does not establish whole-genome coverage. PGS requires suitable "
        "scoring-site data and verified calibration; it does not become available just "
        "because a VCF is present. Missing sites are not negative results.\n"
    )
    lab_note = (
        "# Laboratory results\n\nPut original PDF forms or supported CSV/TSV/TXT exports "
        "here. Open Labs in the Scholion panel, choose this folder, then refresh it, "
        "or explicitly ask the assistant to read this folder with ingest_labs. "
        "Copying files alone does not extract results. Originals remain unchanged. "
        "Use the folder form on the Scholion panel to select an existing folder instead.\n"
    )
    wearable_note = (
        "# Wearable exports\n\nKeep the original export archive from your device here. "
        "Copying it does not import measurements. Ask the assistant to check capabilities "
        "for the supported device, import operation and any missing dependencies.\n"
    )
    notes = {"genome/README.md": genome_note, "raw/lab/README.md": lab_note,
             "raw/wearables/README.md": wearable_note}
    # Replace only notes created by THIS init, never an existing README.
    for rel, text in notes.items():
        if rel in result.get("written", []):
            (core.repo_dir() / rel).write_text(text, encoding="utf-8")
    guide = (
        "# Start here — Scholion in Ouroboros\n\n"
        "Open Widgets > Scholion > Add laboratory results to begin. The panel and "
        "assistant use the same profile. Settings and data folders shows the current "
        "paths. Keep medical files on this computer.\n\n"
        f"1. Laboratory results: default folder `{core.raw_dir('lab')}`. Put original PDF "
        "forms or supported CSV/TSV/TXT exports there, then ask the assistant to run "
        "ingest_labs on that folder. The panel's selected laboratory folder takes precedence "
        "if you choose an existing folder; selecting it does not copy or alter its files.\n"
        f"2. Genetic files: `{core.genome_dir()}`. Keep originals and any VCF index. "
        "Ask genome_status and capabilities which connection/import or preparation is needed.\n"
        f"3. Wearable exports: `{core.raw_dir('wearables')}`. Keep the device's original "
        "archive, then check the supported import operation with the assistant.\n\n"
        f"`{core.profile_dir()}` contains the structured profile maintained by Scholion. "
        "Do not put source reports into it or replace its JSON templates by hand. "
        "The work and archive folders are for derived working files and retained originals, "
        "not the first destination for new reports.\n\n"
        "Copying files is not automatic analysis. A disconnected genome does not prevent "
        "laboratory or prescription analysis. Tool results enter the Ouroboros conversation "
        "and follow its model/provider settings.\n"
    )
    try:
        fd = os.open(_guide_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(guide)


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
        "subject_id": "not initialised",
        "data_kind": "not initialised",
        "hint": (
            "No data directory yet. Press «Create the data directory» below: it "
            "writes empty templates and a README into every folder, and touches "
            "nothing that already exists."
        ),
    }
    try:
        from scholion import core
        folders = core.source_config()
        out.update(profile_dir=str(core.profile_dir()),
                   guide_path=(str(_guide_path()) if _guide_path().is_file()
                               else "See 'Your first three files' below; a local guide is created during setup."),
                   labs_dir=str(folders.get("labs_docs") or core.raw_dir("lab")),
                   wearables_dir=str(core.raw_dir("wearables")),
                   genome_dir=str(core.genome_dir()))
        if not ready:
            return out
        from scholion import engine
        o = engine.overview()
        out["markers"] = o.get("markers_total", "—")
        out["medications"] = o.get("medications_count", "—")
        from scholion import container
        out["subject_id"] = container.identity().get("id") or "not assigned yet"
        out["data_kind"] = "synthetic demo" if o.get("synthetic") else "local profile"
        # `genome` is a report and is always present; the product reads its
        # `ready` flag, and so does this. Testing the dict itself said
        # «connected» over an empty directory — the exact shape of wrong answer
        # this product exists to refuse.
        connected = bool((o.get("genome") or {}).get("ready"))
        out["genome"] = "connected" if connected else "not connected"
        gaps = o.get("genome_gaps") or []
        if not connected:
            out["hint"] = (
                "The profile is available; no genome is connected. Existing "
                "laboratory, prescription and wearable records remain available. "
                "Add laboratory PDFs to the folder shown above, or choose an "
                "existing folder below. Ask genome_status about the supported "
                "genome inputs and the analyses each input can support."
            )
            out["needs_profile"] = False
        elif gaps:
            out["hint"] = "Genome connected. Loci still unread: " + ", ".join(str(g) for g in gaps[:6])
            out["needs_profile"] = False
        else:
            out["hint"] = ""
            out["needs_profile"] = False
    except Exception as exc:                     # a broken read is a fact too
        out["read_error"] = True
        out["hint"] = f"The profile could not be read: {type(exc).__name__}: {exc}"
    return out


def _render() -> Dict[str, Any]:
    return {"kind": "module", "entry": "widget.js", "start": "auto",
            "appearance": "host", "span": 2, "max_height": 1600}


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
    async def state(request):
        from scholion import i18n
        lang = getattr(request, "query_params", {}).get("lang", "en")
        i18n.set_lang(lang)
        out = _state_payload(_data_root())
        out["messages"] = {k[4:]: v for k, v in i18n.messages(lang).items() if k.startswith("hub.")}
        out["lang"] = lang if lang in i18n.available() else "en"
        return _json(out)

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
            if out.get("ok"):
                _prepare_setup_notes(out)
                _save_connection(_binding())
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


def _web_routes(api: Any):
    async def connect(request):
        """Preview first; only a matching explicit confirmation selects a profile."""
        payload = await _payload_of(request)
        from scholion import core, container
        try:
            path = Path(str(payload.get("path") or "")).expanduser()
            if not path.is_absolute():
                raise ValueError("Choose an absolute folder path.")
            path = path.resolve(strict=True)
            profile = path / "profile" if (path / "profile" / "index.md").is_file() else path
            if not (profile / "index.md").is_file() or not (profile / "labs.json").is_file():
                raise ValueError("No Scholion profile was found in this folder. The current profile was preserved.")
            # Reading a preview does not initialise or rewrite the chosen folder.
            binding = _binding()
            binding.update(SCHOLION_REPO_DIR=str(profile.parent), SCHOLION_PROFILE_DIR=str(profile))
            token = _binding_id(binding)
            if payload.get("confirm") != token:
                return _json({"ok": True, "preview": True, "profile": str(profile), "confirm": token})
            _save_connection(binding)
            os.environ.update(binding)
            core.reset_cache()
            container.ensure()
            return _json({"ok": True, **_state_payload(core.repo_dir())})
        except (OSError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)}, 400)

    async def launch(_request):
        try:
            if not _profile_ready(_data_root()):
                return _json({"ok": False, "error": "Create or connect a profile first."}, 409)
            _save_connection(_binding())
            return _json({"ok": True, "binding": _binding_id(_binding())})
        except (OSError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)}, 409)

    async def choose(_request):
        from scholion.folder_picker import choose as pick
        import asyncio
        return _json(await asyncio.to_thread(pick, "Scholion"))

    async def web(request):
        """One private loopback backend, reached only via the host's authenticated bridge."""
        import asyncio
        from http.client import HTTPConnection, HTTPException
        from urllib.parse import urlsplit

        path = request.query_params.get("path", "/")
        parsed = urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parsed.netloc or parsed.scheme
                or parsed.fragment or any(c in path for c in "\r\n\\") or ".." in parsed.path.split("/")):
            return _json({"error": "Invalid local route."}, 400)
        if parsed.path == "/api/use":
            return _json({"error": "This panel and the assistant are pinned to the same profile."}, 409)
        expected = _binding_id(_binding())
        supplied = request.headers.get("X-Scholion-Profile")
        if request.method == "GET":
            supplied = supplied or request.query_params.get("binding")
        if supplied != expected:
            return _json({"error": "The profile connection changed. Reopen Scholion before continuing."}, 409)
        conn = None
        try:
            info = json.loads((Path(api.get_state_dir()) / "web-runtime.json").read_text(encoding="utf-8"))
            if info.get("binding") != expected:
                raise ValueError("The web panel and assistant do not have the same profile.")
            port = int(info["port"])
            if not 0 < port < 65536:
                raise ValueError("Invalid local web port.")
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            body = await request.body()
            headers = {"X-Scholion-Bridge": info["token"], "Content-Type": request.headers.get("content-type", "application/json")}
            if request.headers.get("range"):
                headers["Range"] = request.headers["range"]
            await asyncio.to_thread(conn.request, request.method, path, body=body, headers=headers)
            # Bound connection setup, not an explicitly requested laboratory
            # import. The host owns request cancellation and child cleanup.
            if conn.sock is not None:
                conn.sock.settimeout(None)
            reply = await asyncio.to_thread(conn.getresponse)
        except (OSError, ValueError, KeyError, HTTPException) as exc:
            if conn is not None:
                conn.close()
            return _json({"error": "The local Scholion panel is starting or unavailable. Try again.",
                          "detail": type(exc).__name__}, 503)
        from starlette.responses import Response, StreamingResponse
        response_headers = {k: v for k, v in reply.getheaders() if k.lower() in
                            ("content-type", "content-disposition", "content-length", "content-range", "accept-ranges")}
        response_headers["Cache-Control"] = "no-store"
        if request.method == "HEAD":
            conn.close()
            return Response(status_code=reply.status, headers=response_headers)

        async def stream():
            try:
                while True:
                    chunk = await asyncio.to_thread(reply.read, 65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                conn.close()
        return StreamingResponse(stream(), status_code=reply.status, headers=response_headers)

    return connect, launch, choose, web


# --- registration ---------------------------------------------------------

def _register_tools(api: Any) -> int:
    import scholion
    from scholion.ouroboros_tools import get_tools  # installed via install_specs

    # The host adds isolated dependencies to sys.path for this call. Workers
    # launched with `sys.executable -m scholion` also need that same installed
    # package on PYTHONPATH; it is not installed into the host interpreter.
    installed_site = str(Path(scholion.__file__).resolve().parent.parent)
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    os.environ["PYTHONPATH"] = os.pathsep.join([installed_site] + [p for p in inherited if p != installed_site])

    count = 0
    for entry in get_tools():
        name = entry.name[len(_PREFIX):] if entry.name.startswith(_PREFIX) else entry.name
        schema: Dict[str, Any] = dict(entry.schema.get("parameters") or {})
        if name == "recompute" and "stop" in schema.get("properties", {}):
            # Published 0.7.0's parameter table omitted this boolean; keep
            # the host-facing schema aligned with the cancellation handler.
            schema["properties"] = {key: dict(value) for key, value in schema["properties"].items()}
            schema["properties"]["stop"]["type"] = "boolean"
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
        connect, launch, choose, web = _web_routes(api)
        api.register_route("connect", connect, methods=("POST",))
        api.register_route("launch", launch, methods=("POST",))
        api.register_route("choose", choose, methods=("POST",))
        api.register_route("web", web, methods=("GET", "POST", "HEAD"))
        api.register_companion_process("web")
        api.register_ui_tab(_TAB, "Scholion", icon="🧬", render=_render())
        api.log("info", f"scholion: {tools} tools and embedded web panel, data at {root}")
    except Exception as exc:
        api.log("warning", f"scholion: {tools} tools, no onboarding surface "
                           f"({type(exc).__name__}: {exc})")
