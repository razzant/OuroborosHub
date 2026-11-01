"""The real Ouroboros host around Memory Atlas, on synthetic memory, for consumer checks. Development only.

Everything on the path from memory to pixels is the product's own:

1. a copy of a core checkout (``--core``, read-only source; the copy runs) and its ``server.py``, started in
   ``ouroboros.test_environment.isolated_environment`` (every writable root under this run's directory:
   HOME, data, app root, caches, temp) on loopback ports the OS hands out, with the core's own
   ``ModelStub`` as its only model provider (no network, no account, no key);
2. a data root filled by Ouroboros's producers (``dev/seed_memory.py``), never the owner's memory;
3. this payload installed into that data root only, enabled with a PASS review state for its exact
   content hash (as the core's own widget browser fixture does), so the real loader, PluginAPI 2.1
   ``read_memory``, gateway routes, Widgets page, card, opaque frame and bridge serve it.

Nothing touches the installed application, its data, its settings or its ports. ``stop()`` ends the server
and the stub and keeps the run directory (server log, seed summary) as evidence.

CLI: python3 -I -B dev/realhost.py --core <checkout> --root <short empty dir> [--no-stress]
prints ``READY <url>`` and serves until interrupted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]   # the hub checkout: tests/memory_atlas/dev
PAYLOAD = REPO / "skills" / "memory-atlas"
SKILL = "memory-atlas"
SYNTHETIC = "Synthetic test memory, written by Ouroboros's producers for this check; not anyone's memory."


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RealHost:
    def __init__(self, core: Path, root: Path, *, stress: bool = True, python: str = sys.executable) -> None:
        self.core, self.root, self.stress, self.python = Path(core).resolve(), Path(root).resolve(), stress, python
        self.repo = self.root / "repo"
        self.server = self.model = None
        self.url = ""
        self.ids: dict = {}

    # --- setup ---------------------------------------------------------------------------------------
    def _copy_core(self) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise SystemExit(f"{self.root} is not empty: every run starts from an empty directory")
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.core, self.repo, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
        sys.path.insert(0, str(self.repo))

    def _seed(self, env: dict) -> None:
        out = subprocess.run([self.python, "-I", "-B", str(HERE / "seed_memory.py"), "--core", str(self.repo),
                              "--data", env["OUROBOROS_DATA_DIR"], *(["--stress"] if self.stress else [])],
                             env=env, capture_output=True, text=True, timeout=600)
        (self.root / "seed.log").write_text(out.stdout + out.stderr, encoding="utf-8")
        if out.returncode:
            raise SystemExit("seeding failed: " + out.stderr[-2000:])
        self.ids = json.loads(out.stdout.strip().splitlines()[-1])

    def install(self, data: Path) -> Path:
        """The payload into this run's data root only, enabled and reviewed for its exact bytes."""
        from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_enabled, save_review_state

        target = data / "skills" / "external" / SKILL
        shutil.copytree(PAYLOAD, target, ignore=shutil.ignore_patterns("__pycache__"))
        save_enabled(data, SKILL, True)
        save_review_state(data, SKILL, SkillReviewState(status="pass", content_hash=compute_content_hash(target)))
        return target

    def _serve(self, env: dict, log: str) -> None:
        from tests.separate_ui_support import OwnedProcess

        port, service = _port(), _port()
        env = dict(env, OUROBOROS_SERVER_HOST="127.0.0.1", OUROBOROS_SERVER_PORT=str(port),
                   OUROBOROS_HOST_SERVICE_PORT=str(service), OUROBOROS_DISABLE_MANAGED_UPDATES="1")
        env.pop("OUROBOROS_NETWORK_PASSWORD", None)
        self.server = OwnedProcess("atlas-host", [self.python, "server.py"], cwd=self.repo, env=env,
                                   log_path=self.root / log)
        self.server.start()
        self.url = f"http://127.0.0.1:{port}"
        # Who owns what, written before anything can go wrong: a later cleanup stops exactly these.
        (self.root / "owner.json").write_text(json.dumps({"runner_pid": os.getpid(), "server_pids": self.server.pids,
                                                          "url": self.url, "root": str(self.root)}), encoding="utf-8")
        deadline = time.monotonic() + 120
        while True:
            try:
                with urllib.request.urlopen(self.url + "/api/health", timeout=3) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            if time.monotonic() > deadline or not self.server.running:
                tail = self.server.log_text()[-4000:]
                self.stop()
                raise SystemExit("the isolated host did not answer:\n" + tail)
            time.sleep(0.3)

    def start(self) -> "RealHost":
        self._copy_core()
        from ouroboros.test_environment import isolated_environment
        from tests.separate_ui_support import ModelStub

        env = isolated_environment(self.root, self.repo)
        self.env = env
        data = Path(env["OUROBOROS_DATA_DIR"])
        self.model = ModelStub()
        settings = {"OUROBOROS_MODEL": "openai-compatible::mock-model", "OUROBOROS_MODEL_LIGHT": "openai-compatible::mock-model",
                    "OPENAI_COMPATIBLE_API_KEY": "fixture-only", "OPENAI_COMPATIBLE_BASE_URL": self.model.base_url,
                    "OUROBOROS_MAX_WORKERS": 1, "OUROBOROS_RUNTIME_MODE": "advanced", "OUROBOROS_STARTER_SKILLS": "[]"}
        (data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        # A fresh installation first: the host initializes its own state on an empty data root and keeps
        # running; the producers then write memory into it as a running installation's work would.
        self._serve(env, "server.log")
        self._seed(env)
        self.skill_dir = self.install(data)
        self.tab = self._register()
        self._record()
        return self

    def _register(self) -> dict:
        """The card as the Widgets page lists it. An enabled, reviewed install is picked up by the host's own
        reconcile; a refusal is kept word for word (``registration.log``) and ends the run."""
        said = []
        deadline = time.monotonic() + 90
        for attempt in range(1000):
            tabs = self.get("/api/widgets").get("ui_tabs") or []
            tab = next((tab for tab in tabs if tab.get("skill") == SKILL), None)
            if tab is not None:
                return tab
            if attempt == 0:
                said.append(("reconcile",) + self.post_status(f"/api/skills/{SKILL}/reconcile", {}))
            elif attempt == 5:
                said.append(("toggle",) + self.post_status(f"/api/skills/{SKILL}/toggle", {"enabled": True}))
            if time.monotonic() > deadline:
                break
            time.sleep(1)
        (self.root / "registration.log").write_text(json.dumps(said, indent=2), encoding="utf-8")
        tail = self.server.log_text()[-4000:]
        self.stop()
        raise SystemExit("the host did not register the Memory Atlas card: " + json.dumps(said)[:3000] + "\n" + tail)

    def _record(self) -> None:
        (self.root / "host.json").write_text(json.dumps({
            "url": self.url, "pid": self.server.pids if self.server else [], "tab": self.tab, "ids": self.ids,
            "core": str(self.core), "skill_dir": str(self.skill_dir), "synthetic": SYNTHETIC}, indent=2), encoding="utf-8")

    def reinstall(self) -> dict:
        """The payload as it is now, for a host that is already running (development iteration): the card is
        turned off, its files replaced and reviewed for their new bytes, and turned on again."""
        from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state

        data = Path(self.env["OUROBOROS_DATA_DIR"])
        self.post_status(f"/api/skills/{SKILL}/toggle", {"enabled": False})
        shutil.rmtree(self.skill_dir)
        shutil.copytree(PAYLOAD, self.skill_dir, ignore=shutil.ignore_patterns("__pycache__"))
        save_review_state(data, SKILL, SkillReviewState(status="pass", content_hash=compute_content_hash(self.skill_dir)))
        status, body = self.post_status(f"/api/skills/{SKILL}/toggle", {"enabled": True})
        if status != 200:
            raise RuntimeError(f"turning the reinstalled card on answered {status}: {body}")
        self.tab = self._register()
        self._record()
        return self.tab

    @classmethod
    def attach(cls, root: Path, python: str = sys.executable) -> "RealHost":
        """A host another process started with this module (``host.json`` in ``root``), for tests and captures
        against a running instance. ``stop()`` on an attached host stops nothing: its runner owns it."""
        root = Path(root).resolve()
        facts = json.loads((root / "host.json").read_text(encoding="utf-8"))
        host = cls(Path(facts["core"]), root, python=python)
        if str(host.repo) not in sys.path:
            sys.path.insert(0, str(host.repo))
        from ouroboros.test_environment import isolated_environment

        host.env = isolated_environment(root, host.repo, create=False)
        host.url, host.ids, host.tab, host.skill_dir = facts["url"], facts["ids"], facts["tab"], Path(facts["skill_dir"])
        host.attached = True
        return host

    # --- requests from the test side ------------------------------------------------------------------
    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.url + path, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def post_status(self, path: str, body: dict) -> tuple:
        """``(status, answer)`` of one POST; a refusal is an answer, never an exception."""
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

    def producer(self, script: str) -> str:
        """Run producer code against this host's data root (a new record, a changed note) in a child."""
        code = ("import sys, json, pathlib; sys.path.insert(0, %r)\nroot = pathlib.Path(%r)\nids = json.loads(%r)\n"
                % (str(self.repo), self.env["OUROBOROS_DATA_DIR"], json.dumps(self.ids))) + script
        out = subprocess.run([self.python, "-I", "-B", "-c", code], env=self.env, capture_output=True, text=True, timeout=120)
        with (self.root / "producers.log").open("a", encoding="utf-8") as log:
            log.write(script + "\n--- out\n" + out.stdout + out.stderr + "\n")
        if out.returncode:
            raise RuntimeError("producer failed: " + out.stderr[-2000:])
        return out.stdout

    def stop(self) -> None:
        if getattr(self, "attached", False):
            return
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.model is not None:
            self.model.close()
            self.model = None

    def __enter__(self) -> "RealHost":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True, type=Path)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--no-stress", action="store_true")
    args = ap.parse_args()
    host = RealHost(args.core, args.root, stress=not args.no_stress)
    import signal

    def interrupted(*_args):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:  # an interruption while seeding still stops the server this run started
        host.start()
        print("READY " + host.url, flush=True)
        while host.server and host.server.running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        host.stop()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
