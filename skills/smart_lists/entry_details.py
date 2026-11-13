"""Optional agent-maintained context; no task execution or source fetching."""
import json
from urllib.parse import urlsplit

FIELDS = {"tags", "turn", "next_step", "references", "note", "capture_key"}
TURNS = ("unassigned", "owner", "agent", "other")
MAX_DETAILS_CHARS = 128000


def text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"expected text of at most {maximum} characters")
    value.encode("utf-8")
    return value


def tags(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("tags must be a list of at most 20 names")
    result, seen = [], set()
    for item in value:
        item = text(item, 60).strip()
        if not item:
            raise ValueError("tag names cannot be blank")
        if item.casefold() not in seen:
            result.append(item)
            seen.add(item.casefold())
    return result


def clean(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("unknown entry details fields")
    if len(json.dumps(value, ensure_ascii=False)) > MAX_DETAILS_CHARS:
        raise ValueError(f"entry details exceed {MAX_DETAILS_CHARS} characters in total")
    result = {}
    for key, item in value.items():
        if key == "tags":
            result[key] = tags(item)
        elif key == "turn":
            if item not in TURNS:
                raise ValueError("turn must be unassigned, owner, agent or other")
            result[key] = item
        elif key == "references":
            if not isinstance(item, list) or len(item) > 100:
                raise ValueError("references must contain at most 100 objects")
            refs = []
            for ref in item:
                if not isinstance(ref, dict) or set(ref) != {"label", "target"}:
                    raise ValueError("each reference needs label and target")
                label, target = text(ref["label"], 120), text(ref["target"], 1000)
                if not label.strip() or not target.strip() or any(ord(c) < 32 for c in target):
                    raise ValueError("reference label/target must be nonblank, without target control characters")
                scheme = target.split("://", 1)[0] if "://" in target else ""
                if scheme.lower() in ("http", "https"):
                    # The scheme is case-insensitive (RFC 3986); store its canonical
                    # lowercase form. Host, path, query and fragment stay exact.
                    target = scheme.lower() + target[len(scheme):]
                    parsed = urlsplit(target)
                    if not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError("reference must be an HTTP(S) URL without credentials")
                elif not (target.startswith(("task:", "row:")) and target.split(":", 1)[1].strip()):
                    raise ValueError("reference target must be HTTP(S), task: or row:")
                refs.append({"label": label, "target": target})
            result[key] = refs
        else:
            result[key] = text(item, 200 if key == "capture_key" else 1000)
    return result


def matches(entry, selected_tags, tag_mode, turn, query):
    detail = entry.get("details", {})
    have = {t.casefold() for t in detail.get("tags", [])}
    wanted = {t.casefold() for t in selected_tags}
    if wanted and not (wanted <= have if tag_mode == "all" else wanted & have):
        return False
    if turn and detail.get("turn", "unassigned") != turn:
        return False
    return not query or query.casefold() in (entry["text"] + " " + entry.get("description", "") + " " + detail.get("next_step", "") + " " + detail.get("note", "")).casefold()
