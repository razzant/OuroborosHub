"""Public notification subjects are observations, never repository enrollment."""
from __future__ import annotations

import re

from .config import connection_account
from .events import created, make_event, provider_time, thread_history
from .http import TransportError
from .provider import DISCUSSION, RATE
from .store import dump


def public_subject(provider, notification):
    supplied = notification.get("repository") or {}
    # Do not fetch an unselected private subject even with a broad token.
    if supplied.get("private") is not False:
        raise TransportError("notification_public_visibility_unproven")
    selected = {"id": supplied["id"], "full_name": supplied["full_name"]}
    repo = provider.repository(selected)
    if repo.get("private") is not False:
        raise TransportError("notification_public_visibility_unproven")
    label = {"id": repo["id"], "full_name": repo["full_name"], "visibility": "public"}
    subject = notification.get("subject") or {}
    url = subject.get("url")
    if not isinstance(url, str):
        raise TransportError("notification_subject_unavailable")
    base = provider.http.base + "/repos/" + repo["full_name"]
    if subject.get("type") in ("Issue", "PullRequest"):
        # Use the provider's subject URL; verify its returned numeric route.
        if not url.startswith(base + "/"):
            raise TransportError("notification_subject_origin_mismatch")
        target = provider.get(url).data
        number = target["number"]
        kind = "pr" if subject["type"] == "PullRequest" else "issue"
        route = {"repo": label, "thread_kind": kind, "number": number, "kind": "comment"}
    elif subject.get("type") == "Discussion":
        # Notifications expose a REST-shaped Discussion URL but Discussion
        # content is GraphQL. This shape is checked, never an arbitrary URL read.
        match = re.fullmatch(re.escape(base) + r"/discussions/([1-9][0-9]*)", url)
        if not match:
            raise TransportError("notification_discussion_address_unavailable")
        owner, name = repo["full_name"].split("/")
        query = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
                 "discussion(number:$number){" + DISCUSSION + "}} " + RATE + "}")
        target = provider.graphql(query, {"owner": owner, "name": name, "number": int(match[1])})["repository"]["discussion"]
        if not target:
            raise TransportError("notification_subject_unavailable")
        route = {"repo": label, "thread_kind": "discussion", "number": target["number"],
                 "kind": "discussion", "discussion_id": target["id"], "reply_to": None}
    else:
        raise TransportError("notification_subject_type_unsupported")
    _, target, history = provider.thread_snapshot(route)
    for row in history:
        row["_actor_id"] = provider.actor(row).get("platform_actor_id", "")
    return label, target, route, history


def notification_words(notification, recipient, login, repo, target, route):
    subject = notification.get("subject") or {}
    title = target.get("title") or subject.get("title") or ""
    return ("GitHub notification for @" + str(login or recipient) + " (id " + str(recipient) + "), reason "
            + str(notification.get("reason") or "unknown") + ": " + str(subject.get("type") or route["thread_kind"]) + " "
            + repo["full_name"] + "#" + str(route["number"]) + (" " + dump(title) if title else "")
            + ". Outside the selected repositories; read-only here.")


def _history_gap(history):
    return "history_actor_unavailable" if any(
        not row.get("_actor_id") and ("user" in row or "author" in row) for row in history if isinstance(row, dict)) else None


def _outside(eid, revision, old, *, config, notification, recipient, repo, route, history, raw, actor,
             observed_at=None, history_at=None):
    """What every outside observation shares: a read-only view of an unselected public thread, never a reply route."""
    route = {**route, "server": config["api_base_url"]}
    value = make_event(eid, revision, old, account=connection_account(config),
                       repo=repo, thread=route["thread_kind"] + "." + str(route["number"]),
                       kind="notification", raw=raw, actor=actor, route=route, watched=[], history=history,
                       history_gap=_history_gap(history), writer_id=config.get("expected_writer_id"),
                       observed_at=observed_at, history_at=history_at)
    value["message"].update(discovery_only=True, notification=notification, recipient_id=recipient)
    return value


def discovery_event(eid, revision, old, *, config, notification, recipient, repo, target, route, history,
                    change_context=False, proof=None, items=(), previous=None, observed_at=None, history_at=None):
    """One reader's notification update. Under the change capability it is that reader's evidence: no words and
    no facts of its own (each proven message or action is its own event, ``items``), a background change by the
    notification service, linked to its previous update as evidence; without it, the 0.1.6 form."""
    # The speaker is the notification service, not the subject's opener/editor.
    raw = {"id": notification["id"], "body": "GitHub notification for watched identity " + str(recipient) +
           ". Public subject discovered outside the selected repositories.\n\n" +
           dump({"notification": notification, "subject": target}),
           "html_url": target.get("html_url") or target.get("url")}
    value = _outside(eid, revision, old, config=config, notification=notification, recipient=recipient, repo=repo,
                     route=route, history=history, raw=raw, actor={"platform": "github", "attribution": "notification_service"},
                     observed_at=observed_at, history_at=history_at)
    message = value["message"]
    if not change_context:
        # A sticky notification reason is never a fresh mention, assignment or
        # review request. Setup uses any_message on the owner-created Silent place.
        value["facts"] = ["any_message"]
    else:
        # A notification is no one's words, and its updates are not revisions of the words it points to: each is
        # observed, built at once or after a retried read alike.
        proof = proof or {}
        value["actor"] = {"platform": "github", "attribution": "notification_service"}
        value["text"], value["facts"] = "", []
        message["previous_source_event_id"], message["action"] = None, "observed"
        message["non_text"] = [
            {"kind": "notification", "description": notification_words(notification, recipient, proof.get("recipient_login"),
                                                                        repo, target, route)},
            {"kind": "state_change", "description": "GitHub updated this notification" + (
                " at " + str(notification["updated_at"]) if notification.get("updated_at") else "") + ". " + (
                "What it proves for a watched account is reported as its own event." if items else
                "No new message or action addressing a watched account is proven from it.")}]
        message["fact_evidence"] = {"window": proof.get("window"), "items": list(items),
                                    **({"previous_notification_event_id": previous} if previous else {})}
        message["coverage_gaps"] += list(proof.get("gaps") or [])
    message["coverage_gaps"].append({"scope": "permission", "code": "repository_not_selected_for_writes"})
    return value


def outside_item_event(eid, revision, old, *, config, notification, recipient, repo, target, route, history, item,
                       window, login=None, history_at=None):
    """One GitHub message or action an outside notification proves addresses a watched account: new words naming it
    (their author), words an edit added (that edit's actor) or an assignment/review request (its timeline actor).
    Keyed by that message or action, so every reader's notification of it is the same event."""
    value = _outside(eid, revision, old, config=config, notification=notification, recipient=recipient, repo=repo,
                     route=route, history=history, raw=item["raw"], actor=dict(item["actor"]), history_at=history_at)
    message = value["message"]
    value["actor"] = dict(item["actor"])
    value["text"] = ""  # outside words stay in the thread snapshot, marked as this message
    value["facts"] = [item["fact"]] + (["from_bot"] if item["actor"].get("is_bot") else [])
    message["action"] = "edited" if item.get("change") and item["fact"] == "mention" else "observed"
    message["url"] = message.get("url") or target.get("html_url") or target.get("url")
    message["original_author"] = dict(item.get("author") or item["actor"])
    message["non_text"] = [{"kind": "notification", "description": notification_words(notification, recipient, login,
                                                                                       repo, target, route)}]
    message["non_text"] += [{"kind": "state_change", "description": line} for line in item.get("lines") or []]
    if item.get("attribution"):
        message["non_text"].append({"kind": "change_attribution", "description": item["attribution"]})
    message["fact_evidence"] = {**item.get("evidence", {}), "window": window,
                                "reader": {"recipient_id": recipient, "notification_id": notification.get("id"),
                                           "reason": notification.get("reason"), "updated_at": notification.get("updated_at")}}
    message["coverage_gaps"] = list(message.get("coverage_gaps") or []) + list(item.get("gaps") or [])
    message["coverage_gaps"].append({"scope": "permission", "code": "repository_not_selected_for_writes"})
    for key in ("sent_at", "edited_at"):
        message.pop(key, None)
    sent = provider_time(created(item["raw"])) if item["fact"] == "mention" else provider_time(item["raw"].get("created_at"))
    if sent:
        message["sent_at"] = sent
    if item.get("edited_at"):
        message["edited_at"] = item["edited_at"]
    if item.get("message_id"):
        message["message_id"] = item["message_id"]
        snapshot = thread_history(history, _history_gap(history), config.get("expected_writer_id"), waking=item["message_id"],
                                  observed=history_at)
        if snapshot:  # the message is always shown, marked as this message
            value["conversation"]["thread_history"] = snapshot
    if item["fact"] == "mention":
        message["mentions"] = list(item["mentions"])
    if item.get("forward_origin"):
        message["forward_origin"] = item["forward_origin"]
    return value
