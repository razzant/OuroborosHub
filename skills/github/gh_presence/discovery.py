"""Public notification subjects are observations, never repository enrollment."""
from __future__ import annotations

import re

from .config import connection_account
from .events import make_event
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


def discovery_event(eid, revision, old, *, config, notification, recipient, repo, target, route, history):
    route = {**route, "server": config["api_base_url"]}
    # The speaker is the notification service, not the subject's opener/editor.
    raw = {"id": notification["id"], "body": "GitHub notification for watched identity " + str(recipient) +
           ". Public subject discovered outside the selected repositories.\n\n" +
           dump({"notification": notification, "subject": target}),
           "html_url": target.get("html_url") or target.get("url")}
    history_gap = "history_actor_unavailable" if any(
        not row.get("_actor_id") and ("user" in row or "author" in row) for row in history
    ) else None
    value = make_event(eid, revision, old, account=connection_account(config),
                       repo=repo, thread=route["thread_kind"] + "." + str(route["number"]),
                       kind="notification", raw=raw, actor={"platform": "github", "attribution": "notification_service"},
                       route=route, watched=[], history=history, history_gap=history_gap)
    # A sticky notification reason is never a fresh mention, assignment or
    # review request. Setup uses any_message on the owner-created Silent place.
    value["facts"] = ["any_message"]
    value["message"].update(discovery_only=True, notification=notification, recipient_id=recipient)
    value["message"]["coverage_gaps"].append(
        {"scope": "permission", "code": "repository_not_selected_for_writes"})
    return value
