from __future__ import annotations

import pathlib

from .lib import client, links, storage
from starlette.responses import JSONResponse


# The keys the settings route stores (the server form's fields and the documented progress tunable).
_SETTINGS_KEYS = (
    "A2A_HOST",
    "A2A_PORT",
    "A2A_PUBLIC_URL",
    "A2A_AGENT_NAME",
    "A2A_AGENT_DESCRIPTION",
    "A2A_MAX_CONCURRENT",
    "A2A_RESPONSE_TIMEOUT_SEC",
    "A2A_STREAM_DEADLINE_SEC",
    "A2A_PROGRESS_POLL_SEC",
    "A2A_HEARTBEAT_SEC",
    links.SHARED_PASSWORD,
    "A2A_TLS_CERT_FILE",
    "A2A_TLS_KEY_FILE",
)
_SECRET_KEYS = frozenset({links.SHARED_PASSWORD})
_PLAIN_HTTP = ("Without a TLS certificate and key, a network bind sends passwords and every message unencrypted: "
               "use it only over a private link (a VPN, an SSH forward) or behind a TLS proxy.")


def _invalid(message: str):
    return JSONResponse({"ok": False, "message": message}, status_code=400)


def _make_settings_save(api):
    async def _settings_save(request):
        root = pathlib.Path(api.get_state_dir())
        if str(getattr(request, "method", "POST") or "POST").upper() == "GET":
            # Hydration read for the Settings form (CREATING_SKILLS): the stored non-secret values as strings, so the
            # form shows what is saved and its Save never rewrites values the owner did not see.
            try:
                stored = links.load_server_settings(root)
            except links.LinkError as exc:
                return _invalid(str(exc))
            return JSONResponse({key: str(stored[key]) for key in _SETTINGS_KEYS
                                 if key not in _SECRET_KEYS and stored.get(key) is not None})
        try:
            data = await request.json()
        except (TypeError, ValueError):
            return _invalid("Invalid A2A settings payload.")
        if not isinstance(data, dict):
            return _invalid("Invalid A2A settings payload.")
        try:
            with storage.locked(root / links.LOCK_FILE):
                stored = links.load_server_settings(root)
                merged = {key: value for key, value in stored.items()
                          if key in _SETTINGS_KEYS or key == links.PREVIOUS_SHARED}
                merged.update({key: data[key] for key in _SETTINGS_KEYS if key in data and key not in _SECRET_KEYS})
                if "A2A_PUBLIC_URL" in merged:
                    merged["A2A_PUBLIC_URL"] = links.public_url(merged["A2A_PUBLIC_URL"])
                password = links.shared_password(data.get(links.SHARED_PASSWORD))
                before = links.shared_password(stored.get(links.SHARED_PASSWORD))
                if password and password != before:  # the form always hydrates the password blank
                    if links.uses_secret(root, password):
                        return _invalid("That password already belongs to a link; the shared password must differ "
                                        "from every link's inbound password.")
                    merged[links.SHARED_PASSWORD] = password
                    if links.active_shared_digest(root) is None:
                        if before:
                            merged[links.PREVIOUS_SHARED] = before
                    else:
                        merged.pop(links.PREVIOUS_SHARED, None)  # the actual startup value has its own digest
                if bool(str(merged.get("A2A_TLS_CERT_FILE") or "").strip()) != bool(str(merged.get("A2A_TLS_KEY_FILE") or "").strip()):
                    return _invalid("Set both the TLS certificate and the key, or neither.")
                storage.write_json(root / links.SETTINGS_FILE, merged, private=True)
        except links.LinkError as exc:
            return _invalid(str(exc))
        return JSONResponse({"ok": True, "message": "A2A settings saved. Toggle the skill to restart the server."})
    return _settings_save


def _configured_endpoint(stored: dict) -> tuple[str, bool]:
    host = str(stored.get("A2A_HOST") or "127.0.0.1")
    public = links.public_url(stored.get("A2A_PUBLIC_URL"))
    tls = stored.get("A2A_TLS_CERT_FILE") and stored.get("A2A_TLS_KEY_FILE")
    return (links.advertised_url(host, stored.get("A2A_PORT") or 18800, public, bool(tls)),
            links.requires_inbound_auth(host, public))


def _links_text(api) -> str:
    try:
        rows = links.load(api.get_state_dir())
        address, auth = _configured_endpoint(links.load_server_settings(api.get_state_dir()))
    except links.LinkError as exc:
        return f"A2A configuration: {exc}"
    policy = "HTTP Basic required" if auth else "local loopback; authentication optional"
    lines = [f"Configured address: {address} · {policy}"]
    for link in rows:
        lines.append(" · ".join([
            f"{link.id} ({link.label})", link.url or "receives only",
            "sends a password" if link.send_secret else "sends no password",
            "accepts its messages" if link.inbound_hash else "does not accept its messages",
        ] + ([f"CA file {link.ca_file}"] if link.ca_file else [])))
    if not rows:
        lines.append("No links yet.")
    return "\n".join(lines)


def _make_links_status(api):
    async def _links_status(_request):
        return JSONResponse({"links": _links_text(api)})
    return _links_status


def _make_links_save(api):
    async def _links_save(request):
        if str(getattr(request, "method", "POST") or "POST").upper() == "GET":
            return JSONResponse({})  # one form edits one link of many: nothing to pre-fill, never a password
        try:
            data = await request.json()
        except (TypeError, ValueError):
            return _invalid("Invalid link form.")
        form = data if isinstance(data, dict) else None
        try:
            before = links.get(api.get_state_dir(), str(form.get("id") or "").strip()) if form else None
            link = links.save(api.get_state_dir(), form)
        except links.LinkError as exc:
            return _invalid(str(exc))
        message = f"Link {link.id} saved."
        if form.get("accept_secret"):
            message += " Its password for writing here is set; it is never shown. Give that peer the same words."
        if before and ((before.send_secret and not link.send_secret) or (before.ca_file and not link.ca_file)):
            message += (" Its address changed, so the password and CA file saved for the old address were dropped; "
                        "type them again for the new one.")
        return JSONResponse({"ok": True, "message": message})
    return _links_save


def _make_links_remove(api):
    async def _links_remove(request):
        if str(getattr(request, "method", "POST") or "POST").upper() == "GET":
            return JSONResponse({})
        try:
            data = await request.json()
        except (TypeError, ValueError):
            return _invalid("Invalid remove form.")
        link_id = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
        try:
            removed = links.remove(api.get_state_dir(), link_id)
        except links.LinkError as exc:
            return _invalid(str(exc))
        if not removed:
            return _invalid(f"No link is named {link_id!r}.")
        return JSONResponse({"ok": True, "message": f"Link {link_id} removed; its password no longer works."})
    return _links_remove


def _make_place_status(api):
    async def _place_status(request=None):
        selected = str(request.query_params.get("account_id") or "") if request is not None else ""
        if selected and selected != "server":
            return JSONResponse({"ok": False, "code": "account_not_found", "error": "no such A2A account"},
                                status_code=404)
        try:
            rows = links.load(api.get_state_dir())
            address, auth = _configured_endpoint(links.load_server_settings(api.get_state_dir()))
            problem = ""
        except links.LinkError as exc:
            rows, address, auth, problem = [], "", False, str(exc)
        plain = address.startswith("http://") and auth
        policy = "HTTP Basic required" if auth else "local loopback; authentication optional"
        # One whole-account place: each link has its own room in it (a conversation per link, a thread per remote
        # context), but no place of its own, since the host could not attribute a send to a narrower place.
        payload = {
            "title": "A2A server",
            "text": problem or f"Configured address: {address} · {policy}{' (plain HTTP)' if plain else ''} · {len(rows)} link(s)",
            "tone": "warn" if problem or plain else "muted",
        }
        if selected:
            payload["account_id"] = "server"
        return JSONResponse(payload)
    return _place_status


def _origin(ctx) -> dict:
    """Who asked (kept on the letter as a fact): the tool call's task, chat and project, and the host's own
    reference to this call (``ctx.call_ref``) when the host gives one; only the host resolves it to a room."""
    def get(key):
        return ctx.get(key) if isinstance(ctx, dict) else getattr(ctx, key, None)
    chat = get("current_chat_id")
    origin = {"task_id": str(get("task_id") or ""),
              "chat_id": chat if isinstance(chat, int) and not isinstance(chat, bool) else None,
              "project_id": str(get("project_id") or "")}
    if isinstance(get("call_ref"), str) and get("call_ref"):
        origin["call_ref"] = get("call_ref")  # validated again where the letter is written (lib/client.py)
    return origin


def _flag(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def register(api):
    def discover(ctx, url=""):
        return client.discover(api.get_state_dir(), url)

    def send(ctx, url="", message="", task_id="", context_id="", message_id="", wait=True):
        return client.send(api.get_state_dir(), url, message, task_id=task_id, context_id=context_id,
                           message_id=message_id, wait=_flag(wait, True), asked_by=_origin(ctx))

    def status(ctx, url="", task_id="", message_id="", wait=False):
        return client.status(api.get_state_dir(), url, task_id, message_id, wait=_flag(wait, False),
                             asked_by=_origin(ctx))

    api.register_tool(
        "discover",
        handler=discover,
        description=("Fetch another A2A agent's Agent Card. url is an http(s) address or the id of a saved link; "
                     "use the saved link's id for a card that needs its password; a bare URL is anonymous. "
                     "With no url, list the saved links."),
        schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    api.register_tool(
        "send",
        handler=send,
        description=("Send a message to another A2A agent and return its letter handle at once or when the answer "
                     "comes within this call. A slower answer is brought back by a host that offers it, else read "
                     "later with the status call the handle shows. A saved or definitely unaccepted letter can be "
                     "retried with its message_id, the same url and exact message. An uncertain send is not repeated."),
        timeout_sec=client.TOOL_TIMEOUT_SEC,
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "An http(s) address or the id of a saved link."},
                "message": {"type": "string"},
                "task_id": {"type": "string", "description": "The peer's task this message continues."},
                "context_id": {"type": "string", "description": "The peer's context this message continues."},
                "message_id": {"type": "string", "description": "Retry a definitely unaccepted letter (same url and text)."},
                "wait": {"type": "boolean", "description": "Wait for the answer within this call (default true)."},
            },
            "required": ["url", "message"],
        },
    )
    api.register_tool(
        "status",
        handler=status,
        description=("Read a letter by message_id (local; add its url to ask the peer for news), list the letters "
                     "asked from this task or chat (no arguments), or read a peer's task by url and task_id. "
                     "A status read never sends a message."),
        timeout_sec=client.TOOL_TIMEOUT_SEC,
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "task_id": {"type": "string"},
                "message_id": {"type": "string"},
                "wait": {"type": "boolean", "description": "Keep asking until the answer or the end of this call."},
            },
        },
    )
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("GET", "POST"))
    api.register_route("links/save", handler=_make_links_save(api), methods=("GET", "POST"))
    api.register_route("links/remove", handler=_make_links_remove(api), methods=("GET", "POST"))
    api.register_route("links/status", handler=_make_links_status(api), methods=("GET", "POST"))
    api.register_route("status", handler=_make_place_status(api), methods=("GET",))
    api.register_settings_section(
        "a2a",
        title="A2A",
        schema={
            "components": [
                {"type": "markdown", "text": (
                    "**Server** — how other agents reach this install. " + _PLAIN_HTTP + "\n\n"
                    "**Links** — one per agent this install talks to: its address and the password sent there "
                    "(kept only for that exact address), and the password it presents here (typed by you, at "
                    f"least {links.MIN_INBOUND_SECRET} characters, stored only as a keyed hash, never shown "
                    "back). Each linked agent writes as itself and sees only its own tasks; removing a link "
                    "revokes it, and a link made again under the same id is a new agent. "
                    "Without settings pages, edit `settings.json` in the skill's state folder; links live in "
                    "`links.json` beside it.")},
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "A2A_HOST", "label": "Host", "type": "text", "placeholder": "127.0.0.1"},
                        {"name": "A2A_PORT", "label": "Port", "type": "number", "placeholder": "18800"},
                        {"name": "A2A_PUBLIC_URL", "label": "Public address in the Agent Card", "type": "text",
                         "placeholder": "https://agent.example.org/a2a",
                         "help": "The address other agents dial (a proxy or tunnel). Setting it requires HTTP Basic "
                                 "even when the server binds to loopback."},
                        {"name": "A2A_AGENT_NAME", "label": "Agent name", "type": "text"},
                        {"name": "A2A_AGENT_DESCRIPTION", "label": "Description", "type": "textarea"},
                        {"name": "A2A_MAX_CONCURRENT", "label": "Max concurrent inbound requests", "type": "number", "placeholder": "5"},
                        {"name": "A2A_RESPONSE_TIMEOUT_SEC", "label": "Host response wait seconds (max 1740)", "type": "number", "placeholder": "600"},
                        {"name": "A2A_STREAM_DEADLINE_SEC", "label": "Total per-message deadline seconds", "type": "number", "placeholder": "3600"},
                        {"name": "A2A_HEARTBEAT_SEC", "label": "Working-status heartbeat seconds", "type": "number", "placeholder": "25"},
                        {"name": links.SHARED_PASSWORD, "label": "Shared server password (optional)",
                         "type": "password", "help": "One unnamed agent for callers using this password. Give a "
                         "peer its own inbound password below to identify it separately. " + _PLAIN_HTTP},
                        {"name": "A2A_TLS_CERT_FILE", "label": "TLS certificate file (PEM)", "type": "text"},
                        {"name": "A2A_TLS_KEY_FILE", "label": "TLS key file (PEM)", "type": "text"},
                    ],
                    "submit_label": "Save A2A settings",
                },
                {
                    "type": "form",
                    "id": "links",
                    "route": "links/status",
                    "method": "POST",
                    "fields": [{"name": "links", "label": "Links", "type": "textarea", "disabled": True}],
                    "submit_label": "Refresh links",
                    "busy_label": "Reading…",
                },
                {
                    "type": "form",
                    "route": "links/save",
                    "method": "POST",
                    "fields": [
                        {"name": "id", "label": "Link id", "type": "text", "placeholder": "researcher",
                         "help": "Lowercase letters, digits, '-' or '_'. It never changes; a blank field below "
                                 "keeps what is saved for this id."},
                        {"name": "label", "label": "Name shown as the author", "type": "text"},
                        {"name": "url", "label": "Its A2A address", "type": "text",
                         "placeholder": "https://peer.example.org:18800/",
                         "help": "Exactly as its Agent Card gives it (the path too). Leave empty for an agent that "
                                 "only writes here. " + _PLAIN_HTTP},
                        {"name": "send_username", "label": "User name sent there", "type": "text",
                         "placeholder": "ouroboros"},
                        {"name": "send_secret", "label": "Password sent there", "type": "password",
                         "help": "Kept while the address stays the same; a new address needs it typed again."},
                        {"name": "inbound", "label": "Whether it writes here", "type": "select",
                         "options": [
                             {"value": "keep", "label": "Keep what is saved"},
                             {"value": "off", "label": "It does not write here"},
                         ], "placeholder": "keep"},
                        {"name": "accept_secret", "label": "Its password for writing here", "type": "password",
                         "help": f"At least {links.MIN_INBOUND_SECRET} characters; typing one sets or replaces "
                                 "it. Give the peer the same words."},
                        {"name": "ca_file", "label": "CA file for its self-signed HTTPS", "type": "text"},
                    ],
                    "submit_label": "Save link",
                },
                {
                    "type": "form",
                    "route": "links/remove",
                    "method": "POST",
                    "fields": [{"name": "id", "label": "Link id to remove", "type": "text"}],
                    "submit_label": "Remove link",
                },
            ]
        },
    )
    _token = api.get_skill_token()  # noqa: F841 -- retained for companion token provisioning
    api.register_companion_process("a2a_server")
    api.register_companion_process("a2a_letters")  # late answers; runs without the server and its SDK
