"""Incremental overlap plus independent complete reconciliation of old parents.

Checkpoints move only after every page in that stream commits. Child jobs have
separate custody so an old failing parent cannot delay new repository intake.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .config import connection_account
from .discovery import discovery_event, public_subject
from .events import before, comparison, created, is_echo, make_event
from .http import TransportError, next_link
from .store import digest, dump, now


class Collector:
    def __init__(self, store, provider, config, notifications=None, *, unverified_notifications=()):
        self.store, self.provider, self.config = store, provider, config
        self.notifications = notifications or {}
        self.unverified_notifications = set(unverified_notifications)
        self.barred_notifications = set()
        self.account = connection_account(config)

    def verify_notification_identity(self, recipient):
        if recipient not in self.unverified_notifications:
            return
        if recipient in self.barred_notifications:
            raise TransportError("credential_identity_mismatch")
        try:
            self.notifications[recipient].identity(recipient)
        except TransportError as exc:
            if exc.code == "credential_identity_mismatch":
                self.barred_notifications.add(recipient)
            self.store.gap("notifications_identity", exc.code)
            raise
        self.unverified_notifications.remove(recipient)
        self.store.resolve("notifications_identity")

    def seed(self):
        for selected in self.config["repositories"]:
            prefix = "repo:" + str(selected["id"])
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='pending',due=0 WHERE stream LIKE ? AND state='removed'", (prefix + ":%",))
            kinds = ("issues", "comments", "inline", "discussions")
            due = []
            for audit in (False, True):
                for kind in kinds:
                    stream = prefix + (":audit:" if audit else ":") + kind
                    scan = self.store.meta("scan:" + stream, {})
                    if not audit and self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending'", (stream,)):
                        continue
                    if audit and not scan:
                        # The first normal traversal is already complete. Start
                        # the independent periodic audit after that initial window.
                        self.store.set_meta("scan:" + stream, {"next_at": time.time() + self.config.get("reconcile_seconds", 21600)})
                        continue
                    if self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending'", (stream,)):
                        continue
                    if scan.get("next_at", 0) <= time.time():
                        due.append((kind, stream, scan, audit))
            if not due:
                continue
            try:
                repo = self.provider.repository(selected)
            except TransportError as exc:
                self.store.gap(prefix, exc.code)
                continue
            label = {"id": repo["id"], "full_name": repo["full_name"],
                     "visibility": "private" if repo.get("private") else "public"}
            base = "/repos/" + repo["full_name"]
            if not self.store.meta("bootstrap:" + prefix):
                self.store.set_meta("bootstrap:" + prefix, self.config.get("initial_since") or
                                    datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            for kind, stream, scan, audit in due:
                interval = self.config.get("reconcile_seconds", 21600) if audit else self.config["poll_seconds"]
                if kind == "discussions" and not repo.get("has_discussions"):
                    self.store.set_meta("scan:" + stream, {"state": "provider_disabled", "checked_at": now(),
                                                          "next_at": time.time() + interval})
                    continue
                started = now()
                cutoff = None if audit else scan.get("watermark")
                params = {"sort": "updated", "direction": "asc", "per_page": 100}
                if cutoff:
                    params["since"] = cutoff
                suffix = {"issues": "/issues", "comments": "/issues/comments", "inline": "/pulls/comments"}
                if kind == "issues":
                    params["state"] = "all"
                spec = {"kind": kind, "repo": label, "audit": audit, "cutoff": cutoff}
                if kind in suffix:
                    spec["path"] = base + suffix[kind] + "?" + urlencode(params)
                with self.store.transaction() as db:
                    db.execute("DELETE FROM jobs WHERE stream=? AND state='done'", (stream,))
                    self.store.job(db, stream, spec)
                    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("scan:" + stream,
                               dump({**scan, "started_at": started, "next_at": 0, "audit": audit})))
            self.store.resolve(prefix)
        for recipient in self.notifications:
            stream = "notifications:" + str(recipient)
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='pending',due=0 WHERE (stream=? OR stream LIKE ?) AND state='removed'",
                           (stream, stream + ":%"))
            scan = self.store.meta("scan:" + stream, {})
            if self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending'", (stream,)) or scan.get("next_at", 0) > time.time():
                continue
            params = {"all": "true", "per_page": 50}
            if scan.get("watermark"):
                params["since"] = scan["watermark"]
            with self.store.transaction() as db:
                db.execute("DELETE FROM jobs WHERE stream=? AND state='done'", (stream,))
                self.store.job(db, stream, {"kind": "notifications", "recipient": recipient,
                                           "path": "/notifications?" + urlencode(params)})
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("scan:" + stream, dump({**scan, "started_at": now()})))

    def history(self, route):
        try:
            _, _, rows = self.provider.thread_snapshot(route)
            gap = None
            for row in rows:
                actor = self.provider.actor(row)
                if actor.get("platform_actor_id"):
                    row["_actor_id"] = actor["platform_actor_id"]
                elif "user" in row or "author" in row:
                    gap = "history_actor_unavailable"
            return rows, gap
        except (TransportError, KeyError, TypeError) as exc:
            return [], getattr(exc, "code", "history_shape_unavailable")

    def prepare(self, spec, rows):
        repo, kind = spec["repo"], spec["kind"]
        observations, children, history_cache, parent_cache = [], [], {}, {}
        base = "/repos/" + repo["full_name"]
        for raw in rows:
            raw = dict(raw)
            # Stable provider object keys do not depend on a display name or a
            # thread fetched again for every unchanged comment.
            fast_kind = {"comments": "issue_comment", "inline": "inline_comment"}.get(kind)
            if fast_kind:
                fast_key = self.account + ":" + str(repo["id"]) + ":" + fast_kind + ":" + str(raw["id"])
                existing = self.store.one("SELECT fingerprint FROM snapshots WHERE key=?", (fast_key,))
                if existing and existing["fingerprint"] == digest(comparison(raw)):
                    continue
            if kind == "issues":
                thread_kind = "pr" if "pull_request" in raw else "issue"
                number = raw["number"]
                object_kind = thread_kind
                self.store.set_meta("thread_kind:" + str(repo["id"]) + ":" + str(number), thread_kind)
                if thread_kind == "pr":
                    for child_kind, suffix in (("reviews", "reviews?per_page=100"), ("requests", "requested_reviewers"),
                                               ("pull", "")):
                        children.append({"kind": child_kind, "repo": repo, "number": number,
                                         "created_at": created(raw),
                                         "path": base + "/pulls/" + str(number) + ("/" + suffix if suffix else "")})
            elif kind in ("comments", "inline"):
                target = raw.get("issue_url") if kind == "comments" else raw.get("pull_request_url")
                match = re.search(r"/(?:issues|pulls)/(\d+)$", target or "")
                if not match:
                    raise TransportError("comment_thread_missing")
                number = int(match[1])
                key = "thread_kind:" + str(repo["id"]) + ":" + str(number)
                thread_kind = self.store.meta(key)
                if not thread_kind:
                    if number not in parent_cache:
                        parent_cache[number] = self.provider.get(base + "/issues/" + str(number)).data
                    thread_kind = "pr" if "pull_request" in parent_cache[number] else "issue"
                    self.store.set_meta(key, thread_kind)
                object_kind = "issue_comment" if kind == "comments" else "inline_comment"
            elif kind in ("reviews", "requests", "pull"):
                number, thread_kind = spec["number"], "pr"
                object_kind = {"reviews": "review", "requests": "review_request", "pull": "pr_details"}[kind]
                if kind == "requests":
                    raw["id"] = number
                    raw["created_at"] = spec.get("created_at", "")
            else:
                thread_kind = "discussion"
                number = raw["number"] if kind == "discussions" else spec["number"]
                object_kind = "discussion" if kind == "discussions" else "discussion_comment"
            thread = thread_kind + "." + str(number)
            route = {"repo": repo, "thread_kind": thread_kind, "number": number, "kind": "comment"}
            if self.config["api_base_url"] != "https://api.github.com":
                route["server"] = self.config["api_base_url"]
            if kind == "inline":
                route.update(kind="inline", root_comment_id=raw.get("in_reply_to_id") or raw["id"])
            if thread_kind == "discussion":
                discussion_id = raw["id"] if kind == "discussions" else spec["discussion_id"]
                route.update(kind="discussion", discussion_id=discussion_id,
                             reply_to=None if kind == "discussions" else (raw.get("replyTo") or {}).get("id") or raw["id"])
                if kind == "discussions":
                    children.append({"kind": "discussion_comments", "repo": repo, "number": number,
                                     "node_id": discussion_id, "discussion_id": discussion_id})
                elif kind == "discussion_comments":
                    children.append({"kind": "discussion_replies", "repo": repo, "number": number,
                                     "node_id": raw["id"], "discussion_id": discussion_id})
            key = self.account + ":" + str(repo["id"]) + ":" + object_kind + ":" + str(raw["id"])
            cmp = comparison(raw)
            existing = self.store.one("SELECT fingerprint FROM snapshots WHERE key=?", (key,))
            if existing and existing["fingerprint"] == digest(cmp):
                observations.append((key, {"comparison": cmp}, None, False))
                continue
            actor = self.provider.actor(raw)
            echo = not existing and is_echo(self.store, route, raw, actor)
            text = (raw.get("title", "") + "\n\n" if raw.get("title") else "") + (raw.get("body") or "")
            snapshot = {"comparison": cmp, "text": text, "raw": raw}
            bootstrap = self.store.meta("bootstrap:repo:" + str(repo["id"]), "")
            # An old object may be edited while its first scan is in progress.
            # Preserve that observation as live, without inventing its prior text
            # or attributing the edit to the original author.
            older = bool(bootstrap) and (not created(raw) or before(created(raw), bootstrap))
            updated = raw.get("updated_at") or raw.get("updatedAt")
            first_observed_update = not existing and older and bool(updated) and not before(updated, bootstrap)
            baseline = older and not first_observed_update
            if baseline and not existing or echo:
                history, gap = [], "baseline_history_not_fetched" if not echo else "self_echo_retained_without_history_fetch"
            else:
                if thread not in history_cache:
                    history_cache[thread] = self.history(route)
                history, gap = history_cache[thread]
            if echo:
                baseline = "echo"
            kwargs = dict(account=self.account, repo=repo, thread=thread, kind=object_kind, raw=raw,
                          actor=actor, route=route, watched=self.config["watched_identities"],
                          history=history, history_gap=gap, first_observed_update=first_observed_update)
            notification_evidence = []
            for record in self.store.rows("SELECT value FROM notifications WHERE repo_id=? AND number=?", (repo["id"], number)):
                item = json.loads(record["value"])
                match = re.search(r"/(?:issues|pulls|discussions)/(\d+)$", (item.get("subject") or {}).get("url") or "")
                if item.get("repo_id") == repo["id"] and match and int(match[1]) == number:
                    notification_evidence.append(item)
            kwargs["notification"] = notification_evidence
            observations.append((key, snapshot, kwargs, baseline))
        return observations, children

    def tick(self):
        # Rotate streams, then take one page: fast feeds, two notification
        # identities and old-parent audits make progress independently.
        streams = self.store.rows("SELECT stream,MIN(id) first FROM jobs WHERE state='pending' AND due<=? GROUP BY stream ORDER BY stream", (time.time(),))
        if not streams:
            return False
        previous = self.store.meta("collection_last_stream", "")
        selected_stream = next((r["stream"] for r in streams if r["stream"] > previous), streams[0]["stream"])
        job = self.store.one("SELECT * FROM jobs WHERE stream=? AND state='pending' AND due<=? ORDER BY id LIMIT 1", (selected_stream, time.time()))
        self.store.set_meta("collection_last_stream", selected_stream)
        if not job:
            return False
        spec, stream = json.loads(job["spec"]), job["stream"]
        scope = "page:" + str(job["id"])
        notification_updates = []
        try:
            if spec["kind"] in ("notifications", "notification_discovery") and spec["recipient"] not in self.notifications:
                self.store.gap(scope, "notification_recipient_removed")
                with self.store.transaction() as db:
                    db.execute("UPDATE jobs SET state='removed' WHERE id=?", (job["id"],))
                return False
            if spec["kind"] in ("notifications", "notification_discovery"):
                # Verification uses this job's retry/backoff and never delays
                # repository feeds or the other verified Notifications reader.
                self.verify_notification_identity(spec["recipient"])
            if spec["kind"] == "notification_discovery":
                notification, recipient = spec["notification"], spec["recipient"]
                repo, target, route, history = public_subject(self.provider, notification)
                key = self.account + ":notification:" + str(recipient) + ":" + str(notification["id"])
                with self.store.transaction() as db:
                    self.store.observe(db, key, {"comparison": notification},
                                       lambda eid, rev, old: discovery_event(eid, rev, old, config=self.config,
                                       notification=notification, recipient=recipient, repo=repo, target=target,
                                       route=route, history=history))
                    db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
                self.store.resolve(scope)
                self.store.resolve("notification:" + str(recipient) + ":" + str(notification["id"]))
                return True
            if spec.get("repo") and spec["repo"]["id"] not in {r["id"] for r in self.config["repositories"]}:
                self.store.gap(scope, "repository_removed_from_selection")
                with self.store.transaction() as db:
                    db.execute("UPDATE jobs SET state='removed' WHERE id=?", (job["id"],))
                return False
            client = self.notifications[spec["recipient"]] if spec["kind"] == "notifications" else self.provider
            if spec["kind"].startswith("discussion"):
                page = client.discussion_page(spec)
                rows = page["nodes"]
                next_spec = {**spec, "cursor": page["pageInfo"]["endCursor"]} if page["pageInfo"]["hasNextPage"] else None
                if next_spec and (not next_spec["cursor"] or next_spec["cursor"] == spec.get("cursor")):
                    raise TransportError("pagination_cycle")
                if spec["kind"] == "discussions" and spec.get("cutoff"):
                    # The fast header connection is newest first. Independent
                    # audits still visit every old parent regardless of updatedAt.
                    recent = [r for r in rows if not before(r.get("updatedAt", ""), spec["cutoff"])]
                    if len(recent) < len(rows):
                        next_spec = None
                    rows = recent
            else:
                response = client.get(spec["path"])
                rows = response.data if isinstance(response.data, list) else [response.data]
                following = next_link(response.headers)
                next_spec = {**spec, "path": following} if following else None
            if spec["kind"] == "notifications":
                observations, children = [], []
                allowed = {r["id"]: r for r in self.config["repositories"]}
                for item in rows:
                    repo_id = (item.get("repository") or {}).get("id")
                    notification_key = "notification:" + str(spec["recipient"]) + ":" + str(item["id"])
                    prior = self.store.meta(notification_key)
                    # Preserve reason/recipient as evidence; it proves no current mention.
                    notification_updates.append((notification_key,
                                        {"id": item["id"], "recipient": spec["recipient"], "reason": item.get("reason"),
                                         "updated_at": item.get("updated_at"), "repo_id": repo_id,
                                         "subject": {key: (item.get("subject") or {}).get(key) for key in ("type", "url", "latest_comment_url")},
                                         "selected": repo_id in allowed}))
                    if repo_id not in allowed:
                        scope_id = "notification:" + str(spec["recipient"]) + ":" + str(item["id"])
                        if (item.get("repository") or {}).get("private") is not False:
                            self.store.gap(scope_id, "notification_public_visibility_unproven",
                                           {"repo_id": repo_id, "recipient": spec["recipient"]})
                        elif not prior or prior.get("updated_at") != item.get("updated_at"):
                            children.append({"kind": "notification_discovery", "recipient": spec["recipient"],
                                             "notification": item})
                    else:
                        # Repo streams resolve actual content. Notifications never stand
                        # in for an author's words or make sticky reasons into facts.
                        self.store.set_meta("notification_pending:" + str(repo_id), now())
                        if not prior or prior.get("updated_at") != item.get("updated_at"):
                            for kind in ("issues", "comments", "inline", "discussions"):
                                key = "scan:repo:" + str(repo_id) + ":" + kind
                                self.store.set_meta(key, {**self.store.meta(key, {}), "next_at": 0,
                                                         "notification_recipient": spec["recipient"]})
            else:
                observations, children = self.prepare(spec, rows)
            with self.store.transaction() as db:
                for key, value in notification_updates:
                    self.store.notification(db, key, value)
                for key, snapshot, kwargs, baseline in observations:
                    self.store.observe(db, key, snapshot, lambda eid, rev, old, kw=kwargs:
                                       make_event(eid, rev, old, **kw) if kw is not None else None, baseline)
                child_stream = stream if ":children" in stream else stream + ":children"
                for child in children:
                    # A traversal includes every cursor/URL for this child.
                    # Leave unfinished pages and their backoff intact. Only a
                    # complete traversal may be replaced by the next sweep.
                    family = "stream=? AND json_remove(spec,'$.path','$.cursor')=json_remove(?,'$.path','$.cursor')"
                    args = (child_stream, dump(child))
                    unfinished = db.execute("SELECT spec FROM jobs WHERE " + family + " AND state!='done'", args).fetchall()
                    if unfinished:
                        # Older versions could leave a new initial page stalled
                        # against completed pages from the previous traversal.
                        # An unfinished continuation still owns those pages for
                        # cycle detection; an initial page alone does not.
                        if len(unfinished) == 1 and unfinished[0]["spec"] == dump(child):
                            db.execute("DELETE FROM jobs WHERE " + family + " AND state='done'", args)
                        continue
                    db.execute("DELETE FROM jobs WHERE " + family + " AND state='done'", args)
                    self.store.job(db, child_stream, child)
                if next_spec:
                    prior = db.execute("SELECT id,state FROM jobs WHERE stream=? AND spec=?", (stream, dump(next_spec))).fetchone()
                    if prior and (prior["state"] == "done" or prior["id"] == job["id"]):
                        raise TransportError("pagination_cycle")
                    self.store.job(db, stream, next_spec)
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
            self.store.resolve(scope)
            if not self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending'", (stream,)):
                delay = max(self.config["poll_seconds"], int(self.store.meta("quota:" + client.credential_name, {}).get("x-poll-interval", 0)))
                previous_scan = self.store.meta("scan:" + stream, {})
                if previous_scan.get("audit"):
                    delay = self.config.get("reconcile_seconds", 21600)
                started = previous_scan.get("started_at", now())
                watermark = (datetime.fromisoformat(started.replace("Z", "+00:00")) -
                             timedelta(seconds=self.config.get("overlap_seconds", 120))).isoformat()
                self.store.set_meta("scan:" + stream, {**previous_scan, "completed_at": now(), "next_at": time.time() + delay,
                                                       "watermark": watermark,
                                                       "coverage": "surviving objects; independent child reconciliation; snapshot history only"})
                self.store.resolve(stream)
            return True
        except (TransportError, KeyError, TypeError, ValueError) as exc:
            detail = {"job": spec, "observed_at": now()}
            if isinstance(exc, TransportError) and exc.code == "graphql_partial_errors":
                # Whole partial response stays private local evidence, not public status text.
                self.store.set_meta("partial_page:" + str(job["id"]), exc.response.data)
                detail["partial_evidence_key"] = "partial_page:" + str(job["id"])
            self.store.gap(scope, getattr(exc, "code", "provider_shape_invalid"), detail)
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET due=? WHERE id=?", (getattr(exc, "retry_at", 0) or time.time() + 60, job["id"]))
            return False
