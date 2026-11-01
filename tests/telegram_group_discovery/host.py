"""The real current Ouroboros host around the Telegram bot plugin, fed by a controlled Bot API.

Everything between a raw Telegram update and the pixels is the product's own:

1. a byte-faithful, origin-proved copy of the core candidate (``tests/candidate_checkout.py``) runs ``server.py``
   in ``isolated_environment`` (HOME, data, caches, temp all under this run's directory) on loopback ports the OS
   hands out, with the core's ``ModelStub`` as its only model provider;
2. the plugin payload under test is installed into that data root only with synthetic admission records (fixture setup keyed to its exact bytes;
   no review is performed or claimed),
   and the host's own loader starts its runtime;
3. the plugin's poll loop, parser and custody run unchanged against ``fake_bot_api.FakeBotApi``: the one seam is
   ``shim/sitecustomize.py`` (PYTHONPATH of the host process), which moves the hard-coded
   ``https://api.telegram.org`` authority to that loopback server. A second fence sends any other HTTPS to a dead
   loopback proxy. No credential, no installed state, no real Telegram call.

Nothing here reads the plugin's tables. The test side sees what an owner's browser and the public routes see.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from fake_bot_api import BOT_ID, TOKEN, FakeBotApi

HERE = Path(__file__).resolve().parent
HUB = HERE.parents[1]
SKILL = "telegram-bot"
LEGACY_REF = os.environ.get("TG_DISCOVERY_LEGACY_REF", "089ea451515b2a3b847d06e60edcecc425bfa473")
SYNTHETIC = "Synthetic Telegram inputs and a controlled Bot API on loopback; no account, credential or real call."


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest_tree(root: Path) -> dict:
    files = {}
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": len(files), "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def legacy_skill(into: Path, ref: str = LEGACY_REF) -> Path:
    """The plugin as the Hub published it at ``ref`` (skill 0.8: no ``status.connect``), from Git objects."""
    into.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(HUB), "archive", ref, "skills/telegram-bot"], capture_output=True, timeout=120)
    if archive.returncode:
        raise RuntimeError(f"cannot read {ref} from {HUB}: {archive.stderr.decode()[-500:]}")
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True, timeout=120)
    return into / "skills" / "telegram-bot"


class GroupDiscoveryHost:
    def __init__(self, core: Path, root: Path, skill: Path, *, python: str = sys.executable, confirmed: bool = True):
        self.core, self.root, self.skill, self.python = Path(core).resolve(), Path(root), Path(skill).resolve(), python
        self.bot = FakeBotApi()
        self.bot.get_me_fails = not confirmed
        self.confirmed = confirmed
        self.url = ""
        self.provenance: dict = {}
        self._cm = self._server = self.model = self.candidate = None

    # --- start / stop ----------------------------------------------------------------------------------
    def start(self) -> "GroupDiscoveryHost":
        sys.path.insert(0, str(self.core))
        os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        cc = _load(self.core / "tests" / "candidate_checkout.py", "tg_candidate_checkout")
        ui = _load(self.core / "tests" / "separate_ui_support.py", "tg_separate_ui_support")
        from ouroboros.test_environment import isolated_environment

        self.root.mkdir(parents=True, exist_ok=True)
        self.cc = cc
        self._cm = cc.candidate_checkout(self.core, self.root / "repo", origin_proof=True)
        self.candidate = self._cm.__enter__()
        self.repo = self.candidate.path
        try:
            env = isolated_environment(self.root, self.repo)
            self.env, self.data = env, Path(env["OUROBOROS_DATA_DIR"])
            self.model = ui.ModelStub()
            settings = {"OUROBOROS_MODEL": "openai-compatible::mock-model", "OUROBOROS_MODEL_LIGHT": "openai-compatible::mock-model",
                        "OPENAI_COMPATIBLE_API_KEY": "fixture-only", "OPENAI_COMPATIBLE_BASE_URL": self.model.base_url,
                        "OUROBOROS_MAX_WORKERS": 1, "OUROBOROS_RUNTIME_MODE": "advanced", "OUROBOROS_STARTER_SKILLS": "[]",
                        "TELEGRAM_PUBLIC_BOT_TOKEN": TOKEN}
            (self.data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            self.install(enable=True)
            self._serve(ui)
            self._wait_runtime()
        except BaseException:
            self.stop()
            raise
        return self

    def _port(self) -> int:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _run(self, *args: str) -> dict:
        out = subprocess.run([self.python, "-I", "-B", str(HERE / "install_skill.py"), "--core", str(self.repo),
                              "--data", str(self.data), *args], env=self.env, capture_output=True, text=True, timeout=300)
        (self.root / "install.log").open("a", encoding="utf-8").write(out.stdout + out.stderr)
        if out.returncode:
            raise RuntimeError("skill install failed: " + out.stderr[-3000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def install(self, *, enable: bool = False, skill: Path | None = None) -> dict:
        skill = Path(skill or self.skill)
        facts = self._run("--skill", str(skill), *(["--enable"] if enable else []))
        facts["payload"] = digest_tree(skill)
        self.provenance["installed"] = facts
        return facts

    def _serve(self, ui) -> None:
        port, service = self._port(), self._port()
        self.proof = self.root / "shim-proof"
        env = dict(self.env, OUROBOROS_SERVER_HOST="127.0.0.1", OUROBOROS_SERVER_PORT=str(port),
                   OUROBOROS_HOST_SERVICE_PORT=str(service), OUROBOROS_DISABLE_MANAGED_UPDATES="1",
                   PYTHONPATH=str(HERE / "shim"), TG_FAKE_BOT_API=self.bot.base_url, TG_SHIM_PROOF=str(self.proof),
                   # the second fence: any HTTPS the shim does not move goes to a dead loopback port, never out
                   HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        env.pop("OUROBOROS_NETWORK_PASSWORD", None)
        self.cc.require_candidate_interpreter(self.python, env, self.repo)
        self.candidate.hold()
        self._server = ui.OwnedProcess("tg-discovery-host", [self.python, "server.py"], cwd=self.repo, env=env,
                                       log_path=self.root / "server.log")
        self._server.start()
        self.url = f"http://127.0.0.1:{port}"
        (self.root / "owner.json").write_text(json.dumps({"runner_pid": os.getpid(), "server_pids": self._server.pids,
                                                          "url": self.url, "root": str(self.root)}), encoding="utf-8")
        deadline = time.monotonic() + 120
        while True:
            try:
                state = self.get("/api/state")
                if state.get("supervisor_ready") and state.get("workers_total"):
                    break
            except OSError:
                pass
            if time.monotonic() > deadline or not self._server.running:
                raise RuntimeError("the isolated host did not become ready:\n" + self._server.log_text()[-4000:])
            time.sleep(0.3)
        self.cc.assert_served_candidate(self.url, self.repo, self.data, self._server.pids[-1], self.candidate)
        proofs = sorted(self.root.glob("shim-proof.*"))
        assert proofs, "the Bot API redirect never loaded in the host process"
        self.provenance.update(
            url=self.url, server_pids=list(self._server.pids), python=self.python, core_source=str(self.core),
            candidate_identity=self.candidate.identity, candidate_version=self.candidate.version_text,
            shim_proof=[json.loads(p.read_text()) for p in proofs], synthetic=SYNTHETIC)

    def _wait_runtime(self) -> None:
        """The plugin registered with the host and its runtime reached the controlled Bot API (getMe answered)."""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            connectors = self.get("/api/places").get("connectors") or []
            if any(c.get("connector") == SKILL for c in connectors):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("the host never listed the Telegram connector: " + self._server.log_text()[-3000:])
        self.bot.wait_call("getMe")
        if self.confirmed:
            self.bot.wait_call("getUpdates")

    def stop(self) -> None:
        errors = []
        if self._server is not None:
            try:
                self._server.stop()
                self.candidate.release()  # only a proven teardown releases the copy
            except Exception as exc:  # noqa: BLE001 - reported below, retained copy stays
                errors.append(exc)
            self._server = None
        if self.model is not None:
            self.model.close()
            self.model = None
        self.bot.close()
        if self._cm is not None:
            cm, self._cm = self._cm, None
            try:
                cm.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise RuntimeError(f"host teardown: {errors}")

    # --- update (the skill replaced under a running host) --------------------------------------------
    def update_skill(self, skill: Path) -> dict:
        """Replace the installed payload as the owner's update does: off, new bytes with synthetic admission records, on."""
        self.post_status(f"/api/skills/{SKILL}/toggle", {"enabled": False})
        facts = self.install(skill=skill)
        calls = self.bot.count("getUpdates")  # before enabling: polling proves getMe was consumed
        status, body = self.post_status(f"/api/skills/{SKILL}/toggle", {"enabled": True})
        if status != 200:
            raise RuntimeError(f"turning the updated skill on answered {status}: {body}")
        with self.bot.cond:  # a recorded getMe request alone does not prove confirmed identity
            if not self.bot.cond.wait_for(lambda: sum(c["method"] == "getUpdates" for c in self.bot.calls) > calls, timeout=120):
                raise RuntimeError("the updated runtime never began polling: " + self._server.log_text()[-3000:])
        return facts

    # --- requests (the public routes the owner's browser uses) ---------------------------------------
    def get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def post_status(self, path: str, body: dict) -> tuple:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode("utf-8", "replace")[:2000]

    def status(self) -> dict:
        return self.get(f"/api/extensions/{SKILL}/status")

    def places(self) -> list:
        return self.get("/api/places").get("places") or []

    def connect_place(self, conversation_id: str, label: str, *, active: bool = False) -> dict:
        status, body = self.post_status("/api/places", {"connector": SKILL, "account_id": str(BOT_ID),
                                                        "conversation_id": conversation_id, "label": label})
        assert status == 200 and body.get("ok"), body
        place = body["place"]
        if active:
            status, body = self.post_status(f"/api/places/{place['place_id']}", {"state": "active"})
            assert status == 200 and body.get("ok"), body
            place = body["place"]
        return place
