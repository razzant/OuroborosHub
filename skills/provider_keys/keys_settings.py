"""Project the granted Settings into enabled, issuer-bound metadata reads."""
import hmac
import json
import re
from secrets import token_bytes
from dataclasses import dataclass, field
from urllib.parse import urlsplit

BUILTINS = {
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY"),
    "openai": ("OpenAI", "OPENAI_API_KEY"),
    "anthropic": ("Anthropic", "ANTHROPIC_API_KEY"),
    "deepseek": ("DeepSeek", "DEEPSEEK_API_KEY"),
    "zai": ("Z.ai", "ZAI_API_KEY"),
    "minimax": ("MiniMax", "MINIMAX_API_KEY"),
    "cloudru": ("Cloud.ru", "CLOUDRU_FOUNDATION_MODELS_API_KEY"),
    "gigachat": ("GigaChat", "GIGACHAT_CREDENTIALS"),
    "openai-compatible": ("OpenAI-compatible endpoint", "OPENAI_COMPATIBLE_API_KEY"),
}
OTHER = {"xai": ("xAI", "XAI_API_KEY"), "gemini": ("Google Gemini", "GEMINI_API_KEY")}
LABELS = {k: v[0] for k, v in {**BUILTINS, **OTHER}.items()}
CONNECTIONS = "OUROBOROS_CONNECTIONS"
LEGACY_BASE, COMPAT_BASE = "OPENAI_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL"
UNPROBED = ("zai", "minimax", "cloudru", "gigachat")
KEYS = tuple(v[1] for k, v in {**BUILTINS, **OTHER}.items() if k not in UNPROBED) + (
    LEGACY_BASE, COMPAT_BASE, "GOOGLE_API_KEY", CONNECTIONS)
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
IDENTITY_KEY = token_bytes(32)
OPAQUE = re.compile(r"sk-|xai-|AIza|Bearer\s|[A-Za-z0-9_+/=]{20,}", re.I)


def safe_text(value, secrets, fallback):
    if (not isinstance(value, str) or not value or len(value) > 80 or not value.isprintable()
            or OPAQUE.search(value) or any(contains_secret(value, secret) for secret in secrets)):
        return fallback
    return value


def contains_secret(value, secret):
    secret = text(secret)
    return bool(secret) and any(secret[i:i+6] in value for i in range(max(1, len(secret)-5)))


def issuer_of(base):
    """Only recognize canonical public HTTPS bases. The base itself is never fetched."""
    try:
        if not base.isascii() or any(c.isspace() or ord(c) < 32 for c in base) or any(c in base for c in "?#\\"):
            return None
        u = urlsplit(base)
        if u.scheme == "https" and "@" not in u.netloc and u.port in (443, None):
            if u.hostname == "api.openai.com" and u.path in ("", "/", "/v1", "/v1/"):
                return "openai"
            if u.hostname == "openrouter.ai" and u.path in ("", "/", "/api/v1", "/api/v1/"):
                return "openrouter"
    except ValueError:
        pass
    return None


def text(value):
    return value.strip() if isinstance(value, str) else ""


def enabled(row):
    return str(row.get("enabled", True)).strip().lower() not in {"false", "0", "no", "off"}


@dataclass(repr=False)
class Credential:
    kind: str
    identity: tuple
    reason: str
    uses: list = field(default_factory=list)


def inventory(api):
    result = api.get_settings_state(KEYS)
    state = result["status"]
    if state == "access_closed":
        return [], [], {"state": state, "not_granted": [], "disabled": []}, {}
    if state != "ok":
        raise ValueError("settings_unreadable")
    values = result["values"]
    missing = set(result["missing"])
    denied = [k for k in KEYS if k not in values and k not in missing]
    named = values.get(CONNECTIONS, [])
    if isinstance(named, str):
        named = json.loads(named)
    if not isinstance(named, (list, tuple)) or any(not isinstance(n, dict) for n in named):
        raise ValueError("settings_unreadable")
    rows = {}
    for n in named:
        kind, name = text(n.get("kind")), text(n.get("name"))
        if kind in BUILTINS and NAME.fullmatch(name):
            rows.setdefault((kind, name), n)  # The host uses the first matching row.
    for base_setting in (LEGACY_BASE, COMPAT_BASE):
        if base_setting in values and not isinstance(values[base_setting], str):
            denied.append(base_setting)
    credentials, keys, present, disabled = {}, {}, set(), []

    def secrets():
        yield from (v for k, v in values.items() if k.endswith("_KEY"))
        for n in named:
            yield from (n.get(k) for k in ("api_key", "credentials", "password", "user"))

    def add(kind, key, setting, label, *, source="builtin", name="", base="", reason=""):
        issuer = (issuer_of(base) or kind) if kind == "openai-compatible" else kind
        present.update((kind, issuer))
        if not reason and kind == "openai-compatible":
            reason = "keyless" if not key else "no_metadata_endpoint" if issuer == kind else ""
        # Disabled/unknown uses never attach to an enabled row, even if the key matches.
        if reason:
            key = ""
        token = hmac.digest(IDENTITY_KEY, key.encode(), "sha256") if key else (kind, setting, name)
        identity = (issuer, token, reason)
        if key:
            keys[identity] = key
        cred = credentials.setdefault(identity, Credential(issuer, identity, reason))
        cred.uses.append(dict(source=source, setting=setting, label=label))

    def builtin(kind, key, setting, label, deps, base=""):
        if not enabled(rows.get((kind, "default"), {})):
            present.add(kind)
            disabled.append({"kind": kind, "label": LABELS[kind]})
        elif key or base or any(d in denied for d in deps):
            add(kind, key, setting, label, base=base,
                reason="not_granted" if any(d in denied for d in deps) else "")

    for kind, (label, setting) in BUILTINS.items():
        if kind in UNPROBED:
            continue
        key, base = text(values.get(setting)), ""
        deps = [CONNECTIONS, setting]
        if kind == "openai":
            # A non-canonical legacy base binds this flat key to that other issuer.
            # A dedicated pair supersedes the legacy compatible use, not its issuer evidence.
            legacy = text(values.get(LEGACY_BASE))
            deps.append(LEGACY_BASE)
            if legacy and issuer_of(legacy) != "openai":
                if key:
                    present.add(kind)
                    legacy_unused = text(values.get(COMPAT_BASE)) or not enabled(rows.get(("openai-compatible", "default"), {}))
                    if legacy_unused and enabled(rows.get((kind, "default"), {})):
                        reason = "not_granted" if any(d in denied for d in deps) else "unverified_issuer"
                        add(kind, "", setting, "Built-in key", reason=reason)
                key = ""
        if kind == "openai-compatible":
            # Dedicated base takes its own key (possibly empty); never cross the pairs.
            dedicated = text(values.get(COMPAT_BASE))
            deps = [CONNECTIONS, COMPAT_BASE]
            if dedicated:
                base = dedicated
                deps.append(setting)
            else:
                setting = BUILTINS["openai"][1]
                base, key = text(values.get(LEGACY_BASE)), text(values.get(setting))
                deps.append(LEGACY_BASE)
                if base:
                    deps.append(setting)
                if not key and setting not in denied:
                    base = ""
                if not base:
                    key = ""
        builtin(kind, key, setting, label if kind == "openai-compatible" else "Built-in key", deps, base)

    for (kind, name), n in rows.items():
        if name == "default":
            continue
        label = safe_text(text(n.get("label")), secrets(), "") or safe_text(name, secrets(), "Named connection")
        if not enabled(n):
            present.add(kind)
            disabled.append({"kind": kind, "label": f"{LABELS[kind]} · {label}"})
            continue
        if kind in UNPROBED:
            ready = (bool(text(n.get("credentials")) or text(n.get("user")) and text(n.get("password")))
                     if kind == "gigachat" else bool(text(n.get("api_key"))))
            if ready:
                add(kind, "", CONNECTIONS, label, source="connection", name=name, reason="unsupported")
            continue
        key = text(n.get("api_key"))
        base = text(n.get("base_url")) if kind == "openai-compatible" else ""
        if (kind == "openai-compatible" and not base) or not (key or base):
            continue
        add(kind, key, CONNECTIONS, label, source="connection", name=name, base=base)

    for kind, (_, setting) in OTHER.items():
        label = "Voice key"
        if kind == "gemini" and setting in missing:
            setting, label = "GOOGLE_API_KEY", "Google API key"
        if text(values.get(setting)) or setting in denied:
            add(kind, text(values.get(setting)), setting, label, reason="not_granted" if setting in denied else "")
    absent = [{"kind": k, "label": v} for k, v in LABELS.items() if k not in UNPROBED and k not in present]
    return list(credentials.values()), absent, {
        "state": "not_granted" if denied else "ok", "not_granted": denied, "disabled": disabled,
    }, keys
