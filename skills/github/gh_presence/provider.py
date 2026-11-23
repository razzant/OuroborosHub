"""GitHub's REST and GraphQL operations. Reads and writes share quota custody."""
from __future__ import annotations

import time
import threading
import contextlib
from datetime import datetime
from urllib.parse import quote

from .http import Effect, TransportError, classify, next_link, retry_at, transient
from .store import digest, dump, now
from .http import Response

# Actor is an interface without a numeric id; each account type carries its own databaseId.
ACTOR = ("login __typename ... on User { databaseId } ... on Bot { databaseId } "
         "... on Mannequin { databaseId } ... on Organization { databaseId }")
AUTHOR = "author { " + ACTOR + " }"
DISCUSSION = "id databaseId number title body createdAt updatedAt lastEditedAt url locked closed " + AUTHOR
COMMENT = "id databaseId body createdAt updatedAt lastEditedAt url replyTo { id } " + AUTHOR
PAGE = "pageInfo { hasNextPage endCursor }"
RATE = "rateLimit { cost remaining resetAt }"
# Who changed an object: its content edits and the timeline events of its state, assignees and reviewers, each
# event with its own node id (one GitHub action, whatever its time or actor). GitHub lists content edits newest
# first (SOURCES.md): ``first`` is the newest window and ``hasNextPage`` says older edits remain outside it.
EDITS = ("lastEditedAt userContentEdits(first: 50) { pageInfo { hasNextPage } "
         "nodes { editedAt deletedAt editor { " + ACTOR + " } } }")
_EVENT = "id createdAt actor { " + ACTOR + " }"
_ASSIGNEE = ("__typename ... on User { databaseId login } ... on Bot { databaseId login } "
             "... on Mannequin { databaseId login } ... on Organization { databaseId login }")
_REVIEWER = ("__typename ... on User { databaseId login } ... on Bot { databaseId login } "
             "... on Mannequin { databaseId login } ... on Team { databaseId slug }")
_ISSUE_ITEMS = ("__typename ... on AssignedEvent { " + _EVENT + " assignee { " + _ASSIGNEE + " } } "
                "... on UnassignedEvent { " + _EVENT + " assignee { " + _ASSIGNEE + " } } "
                "... on ClosedEvent { " + _EVENT + " stateReason } ... on ReopenedEvent { " + _EVENT + " } "
                "... on RenamedTitleEvent { " + _EVENT + " previousTitle currentTitle } "
                "... on LockedEvent { " + _EVENT + " } ... on UnlockedEvent { " + _EVENT + " }")
_PR_ITEMS = (_ISSUE_ITEMS + " ... on MergedEvent { " + _EVENT + " } ... on ReadyForReviewEvent { " + _EVENT + " } "
             "... on ConvertToDraftEvent { " + _EVENT + " } "
             "... on ReviewRequestedEvent { " + _EVENT + " requestedReviewer { " + _REVIEWER + " } } "
             "... on ReviewRequestRemovedEvent { " + _EVENT + " requestedReviewer { " + _REVIEWER + " } }")
ISSUE_EVENT_TYPES = "ASSIGNED_EVENT,UNASSIGNED_EVENT,CLOSED_EVENT,REOPENED_EVENT,RENAMED_TITLE_EVENT,LOCKED_EVENT,UNLOCKED_EVENT"
PR_EVENT_TYPES = (ISSUE_EVENT_TYPES + ",MERGED_EVENT,READY_FOR_REVIEW_EVENT,CONVERT_TO_DRAFT_EVENT,"
                  "REVIEW_REQUESTED_EVENT,REVIEW_REQUEST_REMOVED_EVENT")
THREAD_CHANGES = ("query($owner:String!,$name:String!,$number:Int!,$since:DateTime,$before:String){repository(owner:$owner,name:$name){"
                  "issueOrPullRequest(number:$number){__typename "
                  "... on Issue { " + EDITS + " issueTimeline: timelineItems(since:$since,last:100,before:$before,itemTypes:["
                  + ISSUE_EVENT_TYPES + "]) { pageInfo { hasPreviousPage startCursor } nodes { " + _ISSUE_ITEMS + " } } } "
                  "... on PullRequest { " + EDITS + " pullTimeline: timelineItems(since:$since,last:100,before:$before,itemTypes:["
                  + PR_EVENT_TYPES + "]) { pageInfo { hasPreviousPage startCursor } nodes { " + _PR_ITEMS + " } } }}} " + RATE + "}")
NODE_CHANGES = "query($id:ID!){node(id:$id){__typename ... on Comment { " + EDITS + " }} " + RATE + "}"


def _target(value):
    if not isinstance(value, dict):
        return None
    return {"typename": value.get("__typename"), "id": value.get("databaseId"), "login": value.get("login") or value.get("slug")}


def can_close_issue(provider, repo, target, writer_id):
    """Use actual installation grants; never synthesize a user's push permission."""
    auth = getattr(provider.http, "auth", None)
    if auth:
        auth.token_for(provider.http)
        return (writer_id == auth.writer_id and repo.get("id") in auth.repositories
                and auth.permissions.get("issues") == "write")
    return bool(repo.get("permissions", {}).get("push") or (target.get("user") or {}).get("id") == writer_id)


class GitHub:
    def __init__(self, http, store, credential_name="writer", *, config=None):
        self.http, self.store, self.credential_name = http, store, credential_name
        self.config = config or {}
        self.users = {}
        self.network_lock = threading.RLock()
        self.local = threading.local()

    @contextlib.contextmanager
    def collection(self):
        old = getattr(self.local, "collecting", False)
        self.local.collecting = True
        try:
            yield
        finally:
            self.local.collecting = old

    def request(self, method, path, data=None, headers=None):
        with self.network_lock:
            return self._request(method, path, data, headers)

    def _request(self, method, path, data=None, headers=None):
        quota = self.store.meta("quota:" + self.credential_name, {})
        if quota.get("retry_at", 0) > time.time():
            raise TransportError("provider_backoff", retry_at=quota["retry_at"])
        if getattr(self.local, "collecting", False):
            resource = "graphql" if path == "/graphql" else "core"
            remaining = self.store.meta("quota_resource:" + self.credential_name + ":" + resource, {})
            reserve = self.config.get("quota_reserve", 100)
            if remaining.get("reset", 0) > time.time() and remaining.get("remaining", reserve + 1) <= reserve:
                raise TransportError("provider_quota_reserve", retry_at=remaining["reset"])
        auth = getattr(self.http, "auth", None)
        try:
            response = self.http.request(method, path, data, headers=headers) if headers else self.http.request(method, path, data)
        except TransportError as exc:
            if auth and exc.code.startswith("app_"):
                self.store.set_meta("auth:" + self.credential_name, {**auth.status(), "checked_at": now(), "code": exc.code})
            raise
        if auth:
            self.store.set_meta("auth:" + self.credential_name, {**auth.status(), "checked_at": now()})
        facts = {k: response.headers[k] for k in ("x-ratelimit-limit", "x-ratelimit-remaining",
                 "x-ratelimit-reset", "x-ratelimit-resource", "x-poll-interval") if k in response.headers}
        facts["observed_at"] = now()
        if "x-poll-interval" not in facts and "x-poll-interval" in quota:
            facts["x-poll-interval"] = quota["x-poll-interval"]
        if "x-ratelimit-remaining" in facts and "x-ratelimit-reset" in facts:
            self.store.set_meta("quota_resource:" + self.credential_name + ":" + facts.get("x-ratelimit-resource", "core"),
                                {"remaining": int(facts["x-ratelimit-remaining"]), "reset": float(facts["x-ratelimit-reset"])})
        if classify(response) == Effect.RATE_REFUSED:
            attempts = quota.get("refusals", 0) + 1
            facts["refusals"] = attempts
            facts["retry_at"] = max(retry_at(response), time.time() + min(3600, 60 * 2 ** min(attempts - 1, 6)))
        elif response.status >= 400:
            facts["refusals"] = quota.get("refusals", 0)
        self.store.set_meta("quota:" + self.credential_name, facts)
        if classify(response) == Effect.RATE_REFUSED:
            raise TransportError("provider_rate_refusal", response, facts["retry_at"])
        if response.status == 401:
            self.store.set_meta("auth:" + self.credential_name, {"state": "invalid", "checked_at": now()})
        return response

    def get(self, path):
        # Cache is bound to the credential's principal/scope and origin. Every reuse is
        # conditional on the provider's current response, never a local TTL.
        key = digest([getattr(self.http, "cache_identity", self.credential_name), path])
        row = self.store.one("SELECT value FROM http_cache WHERE key=?", (key,))
        import json
        saved = json.loads(row["value"]) if row else None
        conditional = {}
        if saved:
            if saved["headers"].get("etag"):
                conditional["If-None-Match"] = saved["headers"]["etag"]
            elif saved["headers"].get("last-modified"):
                conditional["If-Modified-Since"] = saved["headers"]["last-modified"]
        response = self.request("GET", path, headers=conditional)
        # Follow read redirects only inside the credential's exact origin.
        visited = set()
        while response.status in (301, 302, 307, 308) and response.headers.get("location"):
            target = self.http.url(response.headers["location"])
            if target in visited:
                raise TransportError("redirect_cycle")
            visited.add(target)
            response = self.request("GET", target)
        if response.status == 304 and saved:
            response = Response(200, saved["data"], {**saved["headers"], **response.headers})
        if response.status != 200 or response.data is None:
            code = "no_longer_visible" if response.status in (404, 410) else "provider_http_" + str(response.status)
            raise TransportError(code, response, retry_at(response))
        if response.headers.get("etag") or response.headers.get("last-modified"):
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO http_cache(key,value,used) VALUES(?,?,?)",
                           (key, dump({"data": response.data, "headers": response.headers}), time.time()))
        if self.store.meta("cache_pruned_at", 0) + 3600 < time.time():
            # Only rebuildable HTTP cache expires. Inbox, jobs, source snapshots,
            # receipts and obligations keep their independent durable custody.
            with self.store.transaction() as db:
                db.execute("DELETE FROM http_cache WHERE used<?", (time.time() - 86400,))
            self.store.set_meta("cache_pruned_at", time.time())
        return response

    def all(self, path):
        seen = set()
        while path:
            if path in seen:
                raise TransportError("pagination_cycle")
            seen.add(path)
            response = self.get(path)
            if not isinstance(response.data, list):
                raise TransportError("provider_list_shape")
            yield from response.data
            path = next_link(response.headers)

    def identity(self, expected):
        auth = getattr(self.http, "auth", None)
        if auth:
            try:
                auth.token_for(self.http)
                user = self.get("/users/" + quote(auth.slug + "[bot]", safe="")).data
                auth.check_bot(user, auth.slug, expected)
            except (TransportError, ValueError):
                self.store.set_meta("auth:" + self.credential_name,
                                    {"state": "invalid", "auth_mode": "app", "checked_at": now()})
                raise
            self.store.set_meta("auth:" + self.credential_name,
                                {"state": "valid", "auth_mode": "app", "id": user["id"], "login": user["login"],
                                 "app_id": auth.app_id, "installation_id": auth.installation_id, "slug": auth.slug,
                                 "expires_at": auth.expires_at, "permissions": auth.permissions, "checked_at": now()})
            return user
        try:
            user = self.get("/user").data
            if user.get("id") != expected:
                raise TransportError("credential_identity_mismatch")
        except TransportError:
            self.store.set_meta("auth:" + self.credential_name,
                                {"state": "invalid", "auth_mode": "pat", "checked_at": now()})
            raise
        self.store.set_meta("auth:" + self.credential_name,
                            {"state": "valid", "auth_mode": "pat", "id": user["id"], "login": user.get("login"),
                             "expires_at": "unknown", "checked_at": now()})
        return user

    def watched_identity(self, selected):
        user = self.get("/users/" + quote(selected["login"], safe="")).data
        if (user.get("id") != selected["id"] or (selected["login"].endswith("[bot]") and
                (user.get("type") != "Bot" or user.get("login", "").lower() != selected["login"].lower()))):
            raise TransportError("watched_identity_mismatch")
        return user

    def repository(self, spec):
        repo = self.get("/repos/" + spec["full_name"]).data
        if repo.get("id") != spec["id"]:
            raise TransportError("repository_identity_mismatch")
        if repo.get("private") is not False or repo.get("visibility", "public") != "public":
            if repo.get("id") not in self.config.get("private_repository_ids", []):
                raise TransportError("private_repository_not_selected")
        return repo

    def actor(self, raw, *, strict=False):
        """The numeric account of an object's author. ``strict`` (a read that decides attention): a login lookup
        that failed transiently raises, carrying what is known meanwhile as ``exc.actor``, instead of settling unknown."""
        user = raw.get("user") or raw.get("author")
        if not user:
            return {"platform": "github", "attribution": "unknown"}
        if not isinstance(user.get("id"), int):
            login = user.get("login", "")
            actor_type = user.get("__typename") or user.get("type")
            # A known Bot stays a bot when its number is unknown; an unknown account is never a bot.
            bot = {"is_bot": True} if actor_type == "Bot" else {}
            if not isinstance(login, str) or not login:
                return {"platform": "github", "username": login, "attribution": "unknown", **bot}
            # GraphQL Bot.login is the bare slug; REST uses the [bot] account.
            # A User with that same slug is a separate numeric identity.
            lookup = login + "[bot]" if user.get("__typename") == "Bot" and not login.endswith("[bot]") else login
            unknown = {"platform": "github", "username": lookup if bot else login, "attribution": "unknown", **bot}
            database_id = user.get("databaseId")
            if type(database_id) is int and database_id > 0:
                # The primary response names the numeric account: no lossy login lookup.
                return {"platform": "github", "platform_actor_id": str(database_id), "username": lookup,
                        "display_name": lookup, "is_bot": actor_type == "Bot"}
            key = (actor_type, lookup.lower())
            if key not in self.users:
                try:
                    resolved = self.get("/users/" + quote(lookup, safe="")).data
                except TransportError as exc:
                    if strict and transient(exc):
                        exc.actor = unknown
                        raise
                    return unknown
                if (not isinstance(resolved, dict) or type(resolved.get("id")) is not int or resolved["id"] <= 0
                        or not isinstance(resolved.get("login"), str) or resolved["login"].lower() != lookup.lower()
                        or actor_type and resolved.get("type") != actor_type):
                    return unknown
                self.users[key] = resolved
            user = self.users[key]
        return {"platform": "github", "platform_actor_id": str(user["id"]),
                "username": user.get("login", ""), "display_name": user.get("name") or user.get("login", ""),
                "is_bot": user.get("type") == "Bot"}

    def graphql(self, query, variables, *, mutation=False):
        response = self.request("POST", "/graphql", {"query": query, "variables": variables})
        if response.status != 200 or not isinstance(response.data, dict):
            raise TransportError("graphql_http_" + str(response.status), response, retry_at(response))
        data = response.data.get("data") or {}
        if "rateLimit" in data:
            self.store.set_meta("graphql_quota", {**data["rateLimit"], "observed_at": now()})
            limit = data["rateLimit"]
            if "remaining" in limit and "resetAt" in limit:
                self.store.set_meta("quota_resource:" + self.credential_name + ":graphql",
                                    {"remaining": limit["remaining"], "reset": datetime.fromisoformat(limit["resetAt"].replace("Z", "+00:00")).timestamp()})
        if response.data.get("errors"):
            # Partial data are retained as evidence, never mistaken for a complete page.
            raise TransportError("graphql_partial_errors", response, time.time() + 60)
        return data

    def discussion_page(self, spec):
        kind, cursor = spec["kind"], spec.get("cursor")
        if kind == "discussions":
            query = ("query($owner:String!,$name:String!,$after:String){repository(owner:$owner,name:$name){"
                     "discussions(first:100,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{"
                     + DISCUSSION + "} " + PAGE + "}} " + RATE + "}")
            owner, name = spec["repo"]["full_name"].split("/")
            data = self.graphql(query, {"owner": owner, "name": name, "after": cursor})
            return data["repository"]["discussions"]
        field = "comments" if kind == "discussion_comments" else "replies"
        node_type = "Discussion" if kind == "discussion_comments" else "DiscussionComment"
        query = ("query($id:ID!,$after:String){node(id:$id){... on " + node_type + "{"
                 + field + "(first:100,after:$after){nodes{" + COMMENT + "} " + PAGE + "}}} " + RATE + "}")
        data = self.graphql(query, {"id": spec["node_id"], "after": cursor})
        if not data.get("node"):
            raise TransportError("no_longer_visible")
        return data["node"][field]

    def thread_snapshot(self, route):
        """Read the complete target conversation for a pre-send context fingerprint."""
        repo = self.repository(route["repo"])
        base = "/repos/" + repo["full_name"]
        if route["kind"] == "discussion":
            query = "query($id:ID!){node(id:$id){... on Discussion{" + DISCUSSION + " repository { databaseId }}} " + RATE + "}"
            item = self.graphql(query, {"id": route["discussion_id"]}).get("node")
            if not item:
                raise TransportError("no_longer_visible")
            if (item.get("repository") or {}).get("databaseId") != repo["id"] or item.get("number") != route["number"]:
                raise TransportError("discussion_moved_or_identity_mismatch")
            all_rows = [item]
            spec = {"kind": "discussion_comments", "node_id": route["discussion_id"]}
            for row in self.discussion_all(spec):
                all_rows.append(row)
                all_rows.extend(self.discussion_all({"kind": "discussion_replies", "node_id": row["id"]}))
            if route.get("reply_to") and not any(r.get("id") == route["reply_to"] for r in all_rows[1:]):
                raise TransportError("reply_target_unavailable")
            return repo, item, all_rows
        item = self.get(base + "/issues/" + str(route["number"])).data
        if item.get("number") != route["number"] or item.get("url") != self.http.base + base + "/issues/" + str(route["number"]):
            raise TransportError("issue_moved_or_identity_mismatch")
        is_pr = "pull_request" in item
        if is_pr != (route["thread_kind"] == "pr"):
            raise TransportError("thread_kind_mismatch")
        all_rows = [item] + list(self.all(base + "/issues/" + str(route["number"]) + "/comments?per_page=100"))
        if is_pr:
            inline = list(self.all(base + "/pulls/" + str(route["number"]) + "/comments?per_page=100"))
            all_rows.extend(inline)
            all_rows.extend(self.all(base + "/pulls/" + str(route["number"]) + "/reviews?per_page=100"))
            all_rows.append(self.get(base + "/pulls/" + str(route["number"]) + "/requested_reviewers").data)
            if route["kind"] == "inline" and not any(r.get("id") == route["root_comment_id"]
                                                       and not r.get("in_reply_to_id") for r in inline):
                raise TransportError("reply_target_unavailable")
        return repo, item, all_rows

    def change_record(self, route, *, since=None, node=None, before=None):
        """GitHub's own record of who changed an object: its content edits (newest 50) and, for an issue or pull
        request, its state/assignee/reviewer timeline events since ``since`` (newest 100, or the 100 before the
        ``before`` cursor of an earlier page). Read-only."""
        if node:
            item = self.graphql(NODE_CHANGES, {"id": node}).get("node")
        else:
            owner, name = route["repo"]["full_name"].split("/")
            item = (self.graphql(THREAD_CHANGES, {"owner": owner, "name": name, "number": route["number"], "since": since,
                                                  "before": before})
                    .get("repository") or {}).get("issueOrPullRequest")
        if not isinstance(item, dict):
            raise TransportError("no_longer_visible")
        edits = item.get("userContentEdits") or {}
        timeline = item.get("issueTimeline") or item.get("pullTimeline") or {}
        return {"typename": item.get("__typename"), "last_edited_at": item.get("lastEditedAt"),
                "edits": [{"edited_at": e.get("editedAt"), "deleted": bool(e.get("deletedAt")), "editor": e.get("editor")}
                          for e in edits.get("nodes") or [] if isinstance(e, dict)],
                "edits_incomplete": bool((edits.get("pageInfo") or {}).get("hasNextPage")),
                "events": [{"type": n["__typename"], "id": n.get("id"), "created_at": n.get("createdAt"), "actor": n.get("actor"),
                            "assignee": _target(n.get("assignee")), "reviewer": _target(n.get("requestedReviewer")),
                            "current_title": n.get("currentTitle"), "previous_title": n.get("previousTitle"),
                            "state_reason": n.get("stateReason")}
                           for n in timeline.get("nodes") or [] if isinstance(n, dict) and n.get("__typename")],
                "events_incomplete": bool((timeline.get("pageInfo") or {}).get("hasPreviousPage")),
                "events_cursor": (timeline.get("pageInfo") or {}).get("startCursor")}

    def actor_of(self, graph_actor, *, strict=False):
        """A GraphQL Actor (login, __typename, databaseId) as the event actor."""
        return (self.actor({"author": graph_actor}, strict=strict) if isinstance(graph_actor, dict)
                else {"platform": "github", "attribution": "unknown"})

    def discussion_all(self, spec):
        visited = set()
        spec = dict(spec)
        while True:
            page = self.discussion_page(spec)
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]
            if not cursor or cursor in visited:
                raise TransportError("pagination_cycle")
            visited.add(cursor)
            spec["cursor"] = cursor

    def send(self, route, body):
        base = "/repos/" + route["repo"]["full_name"]
        if route["kind"] == "discussion":
            query = "mutation($input:AddDiscussionCommentInput!){addDiscussionComment(input:$input){comment{" + COMMENT + "}}}"
            args = {"discussionId": route["discussion_id"], "body": body}
            if route.get("reply_to"):
                args["replyToId"] = route["reply_to"]
            return self.graphql(query, {"input": args}, mutation=True)["addDiscussionComment"]["comment"]
        if route["kind"] == "inline":
            path = base + "/pulls/{}/comments/{}/replies".format(route["number"], route["root_comment_id"])
        else:
            path = base + "/issues/{}/comments".format(route["number"])
        response = self.request("POST", path, {"body": body})
        if response.status != 201 or not isinstance(response.data, dict):
            raise TransportError("comment_effect_unconfirmed", response, retry_at(response))
        return response.data

    def close_issue(self, route, reason):
        response = self.request("PATCH", "/repos/{}/issues/{}".format(route["repo"]["full_name"], route["number"]),
                                {"state": "closed", "state_reason": reason})
        if response.status != 200 or not isinstance(response.data, dict) or response.data.get("state") != "closed":
            raise TransportError("closure_effect_unconfirmed", response, retry_at(response))
        return response.data

    def readback(self, route, created):
        if route["kind"] == "discussion":
            query = "query($id:ID!){node(id:$id){... on DiscussionComment{" + COMMENT + " discussion { id }}}}"
            return self.graphql(query, {"id": created["id"]}).get("node")
        part = "pulls" if route["kind"] == "inline" else "issues"
        return self.get("/repos/{}/{}/comments/{}".format(route["repo"]["full_name"], part, int(created["id"]))).data
