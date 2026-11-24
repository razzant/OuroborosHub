"""What this install says about itself: the Agent Card, from the host's identity and tool schemas.

The card describes what this Ouroboros instance actually does, capability-first: the name is the stable
product name "Ouroboros" unless the operator sets one (the /identity heading is a section title, never a
name); the description leads with a curated capability summary and appends the /identity first line as
flavor; the skill list always starts with five curated capability categories, followed by live per-tool
entries. A discovery GET answers in seconds from the last known-good tool list while a background
refresher keeps it warm. One ``CardSource`` per daemon holds that state.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger("a2a_daemon")

# Bounded retry for the host tool-schema fetch: the companion routinely starts
# before the host chat-agent is built, so GET /tools/schemas answers 200 with an
# EMPTY tools list for the first few seconds. An empty 200 must be retried like a
# transient failure (not accepted as final), or the card collapses to the
# identity-only entry. The window is generous enough to survive that startup race.
TOOLS_FETCH_ATTEMPTS = 8
TOOLS_FETCH_BACKOFF_SEC = 0.5
HOST_FETCH_TIMEOUT_SEC = 5
# In-request fetch is SHORT: a discovery GET must answer in seconds, not ride the
# full warmup ladder (a cold host once held /.well-known/agent-card.json for ~57s).
# The full ladder lives in the background refresher started with the app.
TOOLS_FETCH_ATTEMPTS_REQUEST = 2
TOOLS_REFRESH_INTERVAL_SEC = 300
TOOLS_RETRY_INTERVAL_SEC = 15
REFRESHER_JOIN_TIMEOUT_SEC = 10

# Curated capability categories for the card's skill list. These are STABLE core
# capabilities of every Ouroboros install, so the card stays honest and USEFUL for
# integrators even while the host tool registry is warming up or unavailable —
# the two prior regressions were a contentless "General" stub and an identity-only
# persona entry, both useless to a consumer that reads capabilities off the card.
CORE_CAPABILITY_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "code-and-files",
        "name": "Code and files",
        "description": (
            "Read, search, edit and run code; structural code queries (definitions, "
            "callers, impact), git operations, cherry-picks and code review."
        ),
        "tags": ["code", "files", "git"],
    },
    {
        "id": "web-and-media",
        "name": "Web and media",
        "description": (
            "Web search, open pages in a real browser, screenshots, PDFs, images, "
            "video frame extraction and YouTube subtitles."
        ),
        "tags": ["web", "media", "research"],
    },
    {
        "id": "long-running-tasks",
        "name": "Long-running tasks and projects",
        "description": (
            "Background tasks with live progress across multiple rounds, and named "
            "projects with a durable journal and memory."
        ),
        "tags": ["tasks", "projects", "background"],
    },
    {
        "id": "self-modification",
        "name": "Self-modification",
        "description": (
            "Edits its own code, prompts and configuration, landing changes through a "
            "mandatory multi-model review and restart cycle."
        ),
        "tags": ["self-modification", "evolution"],
    },
    {
        "id": "skills-extensions",
        "name": "Skills (extensions)",
        "description": (
            "Installable skills extend the agent (A2A bridge, desktop observation and "
            "control, and more); per-tool entries below reflect what is enabled live."
        ),
        "tags": ["skills", "extensions"],
    },
]

# Capability-first top-level description. The identity file's own first line is
# appended as flavor — personality belongs on the card, but never INSTEAD of the
# capability summary an integrator consumes.
CAPABILITY_LEAD = (
    "General-purpose autonomous AI agent: code and files (including git and code "
    "review), web research and media, long-running background tasks and projects, "
    "reviewed self-modification, and installable skills."
)


class CardSource:
    """The card's inputs and the last known-good tool list of one daemon."""

    def __init__(self, *, host_url: str, headers: Callable[[], Dict[str, str]], http: Callable[[], Any],
                 facts: Dict[str, Any], name: str, description: str, url: Callable[[], str], version: str,
                 auth_required: bool) -> None:
        self.host_url, self.headers, self.http, self.facts = host_url, headers, http, facts
        self.name, self.description, self.url, self.version = name, description, url, version
        self.auth_required = auth_required
        # Last non-empty tool list seen. Once the card has populated, a later transient
        # empty fetch must never regress it back to a tool-less card.
        self.last_good_tools: List[Dict[str, Any]] = []
        self._refresher_started = False
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def fetch_identity(self) -> Dict[str, str]:
        """Best-effort read of the host's real name/description from GET /identity.

        Returns {} when the endpoint is unavailable so the caller can fall back to the
        configured name and description. A successful read also refreshes the host's
        feature facts.
        """
        try:
            response = self.http().get(f"{self.host_url}/identity", headers=self.headers(),
                                       timeout=HOST_FETCH_TIMEOUT_SEC)
            if response.status_code == 200:
                data = response.json() or {}
                if isinstance(data, dict):
                    self.facts.clear()
                    self.facts.update(data)
                return {
                    "name": str(data.get("name") or "").strip(),
                    "description": str(data.get("description") or "").strip(),
                }
            logger.warning("a2a agent-card: /identity returned status %s", response.status_code)
        except Exception as exc:
            logger.warning("a2a agent-card: /identity unavailable (%s)", exc)
        return {}

    def fetch_tool_schemas(self, attempts_override: "int | None" = None, *,
                           stop_event: "threading.Event | None" = None) -> List[Dict[str, Any]]:
        """Fetch the host tool schemas, treating an empty list as "not ready yet".

        A single empty 200 is not accepted as final; the last non-empty result is
        kept and a populated card is never regressed back to empty. If every attempt
        yields empty, the last known-good list is served when there is one.
        ``stop_event`` (passed only by the background refresher) aborts the ladder and
        its backoff waits as soon as companion shutdown is requested.
        """
        attempts = TOOLS_FETCH_ATTEMPTS if attempts_override is None else max(1, int(attempts_override))
        last_error = "no attempt made"
        for attempt in range(attempts):
            if stop_event is not None and stop_event.is_set():
                last_error = "shutdown requested"
                break
            try:
                response = self.http().get(f"{self.host_url}/tools/schemas", headers=self.headers(),
                                           timeout=HOST_FETCH_TIMEOUT_SEC)
                if response.status_code == 200:
                    tools = response.json().get("tools") or []
                    if tools:
                        self.last_good_tools = tools
                        return tools
                    last_error = "host returned an empty tool list (chat-agent not ready yet)"
                else:
                    last_error = f"status {response.status_code}"
            except Exception as exc:
                last_error = str(exc)
            if attempt + 1 < attempts:
                delay = TOOLS_FETCH_BACKOFF_SEC * (attempt + 1)
                # Shutdown must interrupt the ladder, not merely the interval between
                # runs: the full backoff chain sleeps longer than the refresher's join
                # timeout, so a plain sleep here made a graceful stop leak the thread.
                if stop_event is not None:
                    if stop_event.wait(delay):
                        last_error = "shutdown requested"
                        break
                else:
                    time.sleep(delay)
        if self.last_good_tools:
            logger.warning(
                "a2a agent-card: /tools/schemas empty after %d attempts (%s); "
                "serving last known-good tool list (%d tools)",
                attempts, last_error, len(self.last_good_tools),
            )
            return self.last_good_tools
        logger.warning(
            "a2a agent-card: /tools/schemas unavailable after %d attempts (%s); "
            "card will advertise the curated capability categories only",
            attempts, last_error,
        )
        return []

    def resolve_identity(self) -> Dict[str, str]:
        """Operator-set name/description always win; otherwise "Ouroboros" and the capability lead with the
        live /identity first line appended as flavor."""
        identity = self.fetch_identity()
        if self.description:
            description = self.description
        else:
            flavor = str(identity.get("description") or "").strip()
            description = CAPABILITY_LEAD + ((" " + flavor) if flavor else "")
        return {"name": self.name or "Ouroboros", "description": description}

    def agent_card(self, *, live: bool = True) -> Dict[str, Any]:
        ident = (self.resolve_identity() if live else
                 {"name": self.name or "Ouroboros", "description": self.description or CAPABILITY_LEAD})
        # Curated categories come FIRST and are ALWAYS present; live per-tool entries
        # follow, from a SHORT in-request fetch (the refresher keeps the list warm).
        skills = [dict(entry) for entry in CORE_CAPABILITY_SKILLS]
        seen_ids = {entry["id"] for entry in skills}
        tools = self.last_good_tools or (self.fetch_tool_schemas(attempts_override=TOOLS_FETCH_ATTEMPTS_REQUEST)
                                         if live else [])
        for schema in tools:
            func = schema.get("function", schema) if isinstance(schema, dict) else {}
            tool_name = str(func.get("name") or "")
            if tool_name and tool_name not in seen_ids:
                seen_ids.add(tool_name)
                skills.append({
                    "id": tool_name,
                    "name": tool_name,
                    "description": str(func.get("description") or "")[:200],
                    "tags": [tool_name.split("_", 1)[0] if "_" in tool_name else "tool"],
                })
        base_url = self.url()
        card = {
            "name": ident["name"],
            "description": ident["description"],
            "url": base_url,
            "version": self.version,
            # A2: advertise the A2A v0.3 transport interface so v0.3-aware clients can negotiate.
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "additionalInterfaces": [{"url": base_url, "transport": "JSONRPC"}],
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            # The password of HTTP Basic names the calling link; the user name is not used.
            "securitySchemes": {"basic": {"type": "http", "scheme": "basic",
                                          "description": "HTTP Basic; the password identifies the calling agent."}},
            "skills": skills,
        }
        if self.auth_required:
            card["security"] = [{"basic": []}]
        return card

    def start_refresher(self) -> None:
        """Keep the tool list warm from a daemon thread (the host chat-agent can take minutes to build).

        ``A2A_TOOLS_REFRESHER=0`` disables the thread (tests and embedders that build the app repeatedly must
        not accumulate pollers); repeated calls are no-ops; ``stop_refresher`` ends it on companion shutdown."""
        if self._refresher_started or os.environ.get("A2A_TOOLS_REFRESHER", "1") == "0":
            return
        self._refresher_started = True
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    self.fetch_tool_schemas(stop_event=self._stop)
                except Exception as exc:
                    # A poll error must not end the refresher permanently: the thread body has no
                    # other recovery path.
                    logger.warning("a2a tools refresher poll failed: %s", exc)
                if self._stop.is_set():
                    break
                if self._stop.wait(TOOLS_REFRESH_INTERVAL_SEC if self.last_good_tools else TOOLS_RETRY_INTERVAL_SEC):
                    break

        self._thread = threading.Thread(target=_loop, name="a2a-tools-refresher", daemon=True)
        self._thread.start()

    def stop_refresher(self) -> None:
        """Terminate the refresher thread through a real code path on companion stop (idempotent)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=REFRESHER_JOIN_TIMEOUT_SEC)
            if thread.is_alive():
                logger.warning("a2a tools refresher did not exit within %ss; leaving it as a daemon thread",
                               REFRESHER_JOIN_TIMEOUT_SEC)
        self._thread = None
        self._refresher_started = False
