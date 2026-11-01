"""Observed content revisions and evidence-based facts; no importance decisions."""
from __future__ import annotations

import re
from datetime import datetime

from .config import LOGIN_PATTERN
from .store import digest, dump, now

PARSER = "github-syntactic-mention/2"


def mentions(text, identities):
    """Mask code and HTML comments while retaining offsets into the exact source.

    Deliberately not a CommonMark renderer: reference links, escapes, indented
    code, malformed markup and GitHub's server mention rules may differ.
    """
    masked = list(text)
    ranges = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            ranges.append((offset, offset + len(line)))
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
        elif match:
            fence = match[1]
            ranges.append((offset, offset + len(line)))
        offset += len(line)
    ranges.extend(m.span() for m in re.finditer(r"<!--[\s\S]*?(?:-->|$)", text))
    for start, end in ranges:
        masked[start:end] = " " * (end - start)
    intermediate = "".join(masked)
    for match in re.finditer(r"(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", intermediate):
        masked[match.start():match.end()] = " " * len(match[0])
    names = {u["login"].lower(): u["id"] for u in identities}
    return [{"parser": PARSER, "span": [m.start(), m.end()], "login": m[1], "user_id": names[m[1].lower()]}
            for m in re.finditer(r"(?<![\w@/])@(" + LOGIN_PATTERN + r")(?![\w/-]|\[bot\])", "".join(masked), re.IGNORECASE)
            if m[1].lower() in names]


def created(raw):
    return raw.get("created_at") or raw.get("createdAt") or raw.get("submitted_at") or ""


def before(a, b):
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) < datetime.fromisoformat(b.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False


def comparison(raw):
    fields = ("id", "node_id", "databaseId", "title", "body", "state", "state_reason", "locked",
              "closed", "draft", "merged", "merged_at", "path", "line", "side", "in_reply_to_id", "replyTo",
              "commit_id", "original_commit_id", "assignees", "users", "teams")
    value = {k: raw[k] for k in fields if k in raw}
    for key in ("assignees", "users", "teams"):
        if key in value:
            value[key] = sorted((str(x["id"]) for x in value[key]))
    value["author_id"] = (raw.get("user") or raw.get("author") or {}).get("id")
    return value


def context_fingerprint(rows):
    return digest(sorted([comparison(r) for r in rows], key=dump))


def route_identity(route):
    """Repository names are labels; a rename does not change a numeric route."""
    return {**route, "repo": {"id": route["repo"]["id"]}}


def is_echo(store, route, raw, actor):
    """Receipt (or locally persisted marker) AND numeric author AND exact route/body."""
    if not actor.get("platform_actor_id"):
        return False
    if route["kind"] == "discussion" and "number" not in raw:
        # The observation's future reply target is this comment. Its physical
        # publication route is its actual parent (None for a top-level comment).
        route = {**route, "reply_to": (raw.get("replyTo") or {}).get("id")}
    for row in store.rows("SELECT p.*,o.intent FROM parts p JOIN outbox o ON o.id=p.delivery_id "
                          "WHERE p.body_hash=? AND p.state IN ('delivered','uncertain','sending')", (digest(raw.get("body", "")),)):
        import json
        intent = json.loads(row["intent"])
        if route_identity(intent["route"]) != route_identity(route) or str(intent["writer_id"]) != actor["platform_actor_id"]:
            continue
        if raw.get("body") != row["body"]:
            continue  # manual edit of our published comment is a new observed revision
        receipt = json.loads(row["receipt"]) if row["receipt"] else {}
        if (receipt.get("provider_id") is not None and str(receipt["provider_id"]) == str(raw.get("databaseId", raw.get("id")))) or row["marker"] in raw.get("body", ""):
            return True
    return False


def make_event(event_id, revision, old, *, account, repo, thread, kind, raw, actor, route,
               watched, history, history_gap=None, notification=None, first_observed_update=False):
    text = (raw.get("title", "") + "\n\n" if raw.get("title") else "") + (raw.get("body") or "")
    action = "edited" if old else "updated_before_first_observation" if first_observed_update else "observed"
    evidence, facts = {}, ["any_message"]
    gaps = []
    # A REST original author does not identify the editor/assigner/state actor.
    event_actor = actor if not old and not first_observed_update and kind != "review_request" else {"platform": "github", "attribution": "unknown"}
    if event_actor.get("is_bot"):
        facts.append("from_bot")
    if not old and not first_observed_update and kind in ("issue", "pr"):
        facts.append("new_issue" if kind == "issue" else "new_pull_request")
    old_text = (old or {}).get("text", "")
    if text and text != old_text and not first_observed_update:
        spans = mentions(text, watched)
        if spans:
            facts.append("mention")
            evidence["mention"] = spans
    ids = {str(x["id"]) for x in watched}
    prior = []
    target_time = (raw.get("updated_at") or raw.get("updatedAt")) if old or first_observed_update else created(raw)
    for entry in history:
        # Numeric user identity is attached by collection, never guessed from a login.
        opened = entry.get("number") == route["number"] and kind in ("issue", "pr", "discussion")
        if str(entry.get("_actor_id", "")) in ids and (opened or before(created(entry), target_time or "")):
            prior.append({"id": entry.get("id"), "created_at": created(entry), "actor_id": entry["_actor_id"]})
    if prior:
        facts.append("my_thread")
        evidence["my_thread"] = prior
    if history_gap:
        gaps.append({"scope": "history", "code": history_gap})
    parent_id = raw.get("in_reply_to_id") or (raw.get("replyTo") or {}).get("id")
    parents = [r for r in history if str(r.get("id")) == str(parent_id) and str(r.get("_actor_id", "")) in ids]
    if parent_id and parents:
        facts.append("reply_to_me")
        evidence["reply_to_me"] = {"parent_id": parent_id, "actor_id": parents[0]["_actor_id"]}
    previous = (old or {}).get("comparison", {})
    if first_observed_update:
        gaps.append({"scope": "revision", "code": "prior_revision_unavailable"})
    for field, fact in (("assignees", "assigned"), ("users", "review_requested")):
        added = set(comparison(raw).get(field, [])) - set(previous.get(field, []))
        addressed = sorted(added & ids)
        if addressed and not first_observed_update:
            facts.append(fact)
            evidence[fact] = {"added_user_ids": addressed, "previous": previous.get(field, []),
                              "current": comparison(raw).get(field, [])}
    if raw.get("teams"):
        gaps.append({"scope": "review_request", "code": "team_membership_not_proven",
                     "team_ids": comparison(raw)["teams"]})
    if not text:
        text = "[GitHub structured observation: " + kind + "]\n" + dump(comparison(raw))
    source_url = raw.get("html_url") or raw.get("url")
    thread_url = next((entry.get("html_url") or entry.get("url") for entry in history
                       if entry.get("number") == route["number"] and (entry.get("html_url") or entry.get("url"))), None)
    key = ":".join(("github", account, "repo." + str(repo["id"]), thread or "0"))
    return {"source_event_id": event_id, "provider": "github", "account_id": account,
            "conversation_id": "repo." + str(repo["id"]), "thread_id": thread, "conversation_key": key,
            "actor": event_actor, "text": text, "facts": facts,
            "conversation": {"repository_id": repo["id"], "full_name": repo["full_name"],
                             "visibility": repo.get("visibility", "public"), "server": route.get("server", "https://api.github.com"), "url": thread_url,
                             "history": {"observed_at": now(), "complete": not bool(history_gap), "rows": history}},
            "message": {"kind": kind, "action": action, "revision": revision, "url": source_url,
                        "previous_source_event_id": event_id.rsplit(":", 1)[0] + ":" + str(revision - 1) if old else None,
                        "raw": raw, "original_author": actor, "reply_route": route,
                        "fact_evidence": evidence, "coverage_gaps": gaps, "notification": notification,
                        "context_fingerprint": context_fingerprint(history), "observed_at": now()}}
