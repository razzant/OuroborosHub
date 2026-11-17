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
from .discovery import discovery_event, outside_item_event, public_subject
from .events import (_who, attribute, before, comparison, created, foreign_words, instant, is_echo, login_of,
                     make_event, mention_identities, mentions, merged_since, node_id, provider_time, same_account,
                     unknown_actor, word_editors, word_origin, words)
from .http import TransportError, next_link, transient
from .provider import GitHub
from .store import digest, dump, later as newer, now

# A transiently failed read that decides an observation's attention facts (new words' author, its thread history,
# who made a change, an outside notification's addressed action) is retried through the job queue before the event
# is first offered to the host; after this many attempts or seconds the event goes out with the failure as an
# explicit gap. A change record GitHub keeps but has not yet returned is reread at most ENRICH_RECORD_ATTEMPTS times.
ENRICH_ATTEMPTS = 6
ENRICH_RECORD_ATTEMPTS = 3
ENRICH_SECONDS = 1800
NEW_WORDS_COMMENTS = ("issue_comment", "inline_comment", "review", "discussion_comment")
# One outside thread's timeline is read back at most this many pages (100 events each) per attempt; a record still
# incomplete past them is an explicit gap, never an empty proof.
OUTSIDE_TIMELINE_PAGES = 5
# Rereads of a record that answered without yet showing what GitHub's notification points to (not failures).
REREADS = ("change_record_missing", "addressed_action_not_yet_visible", "latest_comment_not_yet_visible")
_SLACK = timedelta(seconds=1)


class _Pending(Exception):
    """A read one outside proof item depends on failed transiently or has not shown GitHub's record yet."""

    def __init__(self, need, code):
        super().__init__(code)
        self.need, self.code = need, code


class _Unread:
    """``Collector.release_waiting``'s provider: every read answers as an unavailable one, so a released event
    carries only what was already read."""
    credential_name = "writer"
    actor, actor_of = GitHub.actor, GitHub.actor_of

    def __init__(self):
        self.users = {}

    def get(self, *args, **kwargs):
        raise TransportError("read_unavailable")

    change_record = thread_snapshot = repository = get


def word_state(row):
    """What a row's own fields say of its words since they were written: ``unedited`` (GraphQL ``lastEditedAt`` null,
    or a REST comment updated exactly when created), ``changed`` (an edit time, or a REST comment updated later), or
    ``unknown`` (a REST issue or pull request moves its update time with any activity; a review has none): then only
    GitHub's edit record answers, and an empty record never stands in for words a row says changed."""
    if "lastEditedAt" in row:
        return "unedited" if row["lastEditedAt"] is None else "changed"
    if "number" in row:
        return "unknown"
    written, updated = instant(created(row)), instant(row.get("updated_at") or row.get("updatedAt"))
    if not written or not updated:
        return "unknown"
    return "unedited" if written == updated else "changed"


def untouched(kind, row, history=()):
    """Whether a row's own fields prove nobody but its author can have changed its words since it was written: never
    edited (GraphQL ``lastEditedAt`` null), no later update time (a REST review row carries none: only its record
    can say), or an inline comment last touched no later than its review was published, until when only its author
    saw it. A Discussion also has a title, and ``lastEditedAt`` is not shown to cover it: only one nothing has
    happened to since it was opened (its update time is its creation time) is proven."""
    written, updated = instant(created(row)), instant(row.get("updated_at") or row.get("updatedAt"))
    if kind == "discussion":
        return bool(written) and updated == written
    if word_state(row) == "unedited" or ("lastEditedAt" not in row and kind != "review" and (not updated or updated == written)):
        return True
    if kind == "inline_comment" and updated:
        review = next((old for old in history if isinstance(old, dict) and "submitted_at" in old
                       and old.get("id") == row.get("pull_request_review_id")), None)
        published = instant((review or {}).get("submitted_at"))
        return bool(published) and updated <= published + _SLACK
    return False


def need_gap(need, reason=None):
    """What a need still open after its bounded rereads leaves on the notification: an explicit coverage gap."""
    code = {"addressed_action_not_yet_visible": "notification_reason_unconfirmed",
            "latest_comment_not_yet_visible": "latest_comment_unavailable",
            "change_record_missing": "mention_revision_unproven"}.get(need["code"], "addressed_action_unverified")
    gap = {"scope": "notification", "code": code, "need": need["need"]}
    if code == "addressed_action_unverified":
        gap["error"] = need["code"]
    if code == "notification_reason_unconfirmed" and reason:
        gap["reason"] = reason
    return gap


def numeric_author(raw):
    user = raw.get("user") or raw.get("author") or {}
    ident = user.get("id") if isinstance(user.get("id"), int) else user.get("databaseId")
    return str(ident) if type(ident) is int and ident > 0 else ""


def kept_author(row, actor):
    """``actor``, an outside thread row's author as read now, or (when that leaves it unnumbered) the account the read
    of this same row numbered: its ``_actor_id``, which ``public_subject`` writes on every row it reads (a provider
    field of that name never survives it). Never a number another row, login or editor carries."""
    ident = row.get("_actor_id")
    if actor.get("platform_actor_id") or not isinstance(ident, str) or not re.fullmatch(r"[1-9][0-9]*", ident):
        return actor
    name = actor.get("username") or ""
    return {"platform": "github", "platform_actor_id": ident, "username": name, "display_name": name,
            "is_bot": bool(actor.get("is_bot"))}


def kept_numbers(rows, earlier):
    """A later copy of an outside thread keeps what the waiting proof's earlier copy numbered where its own lookup
    failed: the same message (its node id, else its id) by the same login, never another message or login."""
    def same(row):
        ident = node_id(row) or row.get("id")
        return (ident, login_of(row.get("user") or row.get("author"))) if ident else None
    known = {same(row): row["_actor_id"] for row in earlier
             if isinstance(row, dict) and row.get("_actor_id") and same(row)}
    for row in rows:
        if isinstance(row, dict) and not row.get("_actor_id") and same(row) in known:
            row["_actor_id"] = known[same(row)]


class Collector:
    def __init__(self, store, provider, config, notifications=None, *, unverified_notifications=()):
        self.store, self.provider, self.config = store, provider, config
        self.notifications = notifications or {}
        self.unverified_notifications = set(unverified_notifications)
        self.barred_notifications = set()
        self.account = connection_account(config)
        self.pending, self.merged = {}, set()

    def capabilities(self):
        """What the negotiated host accepts: change observations (``presence_change_context: 1``) and signals."""
        host = self.store.meta("host_capabilities", {}) or {}
        return {"change": type(host.get("presence_change_context")) is int and host["presence_change_context"] == 1,
                "signals": type(host.get("presence_wake_signals")) is int and host["presence_wake_signals"] == 1}

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
            if not self.store.meta("bootstrap:" + stream):
                # An outside notification's addressed action is proven only inside a window this reader observed.
                self.store.set_meta("bootstrap:" + stream, datetime.now(timezone.utc).replace(microsecond=0).isoformat())
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

    def history(self, route, earlier=()):
        """(rows, gap, retryable, read at): the complete thread read; a failed read is a gap, never proof of absence.
        An author lookup that failed transiently is retryable only where its login could be a watched account: only
        those decide a relation fact. ``earlier``: the rows a waiting event already holds of this thread; an author
        that read resolved stays resolved when this lookup fails."""
        watched = {u["login"].lower() for u in self.config["watched_identities"]}

        def identity(row):
            return node_id(row) or row.get("id"), login_of(row.get("user") or row.get("author"))
        known = {identity(row): row["_actor_id"] for row in earlier if isinstance(row, dict) and row.get("_actor_id")}
        try:
            _, _, rows = self.provider.thread_snapshot(route)
            read_at, gap, retryable = now(), None, False
            for row in rows:
                try:
                    actor, failed = self.provider.actor(row, strict=True), False
                except TransportError as exc:
                    actor, failed = getattr(exc, "actor", None) or {}, True
                ident = actor.get("platform_actor_id") or known.get(identity(row))
                if ident:
                    row["_actor_id"] = ident
                    continue
                retryable = retryable or (failed and (identity(row)[1] or "").lower() in watched)
                if "user" in row or "author" in row:
                    gap = "history_actor_unavailable"
            return rows, gap, retryable, read_at
        except (TransportError, KeyError, TypeError) as exc:
            return [], getattr(exc, "code", "history_shape_unavailable"), transient(exc), now()

    def remember_root(self, repo, number, kind, raw):
        """The thread opening's numeric author, kept so a reply proves my_thread even when its history read fails."""
        value = {"id": raw.get("id"), "kind": kind, "created_at": created(raw), "actor_id": numeric_author(raw)}
        key = "thread_root:" + str(repo["id"]) + ":" + str(number)
        if value["actor_id"] and self.store.meta(key) != value:
            self.store.set_meta(key, value)

    def change_attribution(self, route, kind, old, raw, observed_at=None, author=None):
        """(attribution, transient code): GitHub's own record of who made an observed revision.

        Its interval runs from the previous snapshot's provider time (a review-request list has none: the connector's
        previous observation, with slack) to this one's. The content author never stands in for the change's actor.
        An issue or pull request retained before the origin of its title was ever read (a baseline, an earlier
        version's snapshot) has its renames read once since it was opened: ``title_origin`` is ``own`` only when
        every one of them is ``author``'s and that record is complete. A Discussion retained that way has no such
        record to read: its title's origin stays unestablished (``other``).
        """
        whole = False
        if kind == "review_request":
            seen = instant(observed_at)
            lower = (seen - timedelta(seconds=600)).isoformat() if seen and old else None
            upper = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            since = lower or provider_time(raw.get("created_at")) or None
        else:
            old_raw = (old or {}).get("raw") or {}
            lower = provider_time(old_raw.get("updated_at") or old_raw.get("updatedAt") or created(old_raw) or created(raw)) or None
            upper = provider_time(raw.get("updated_at") or raw.get("updatedAt")) or None
            since = (instant(lower) - timedelta(seconds=60)).isoformat() if lower else None
            whole = kind in ("issue", "pr") and bool(old) and not old.get("title_read") and bool(instant(created(raw)))
            if whole:
                since = (instant(created(raw)) - timedelta(seconds=60)).isoformat()
        try:
            if kind in ("issue", "pr", "pr_details", "review_request"):
                record = self.provider.change_record(route, since=since)
            elif node_id(raw):
                record = self.provider.change_record(route, node=node_id(raw))
            else:
                return {"status": "unresolved", "code": "change_record_unaddressable", "records": []}, None
            result = attribute(kind, old, raw, record, lambda actor: self.provider.actor_of(actor, strict=True),
                               lower=lower, upper=upper)
            if whole:
                renamed = [self.provider.actor_of(event.get("actor"), strict=True) if event.get("actor") else unknown_actor()
                           for event in record.get("events") or [] if event.get("type") == "RenamedTitleEvent"]
                ident = (author or {}).get("platform_actor_id")
                result["title_origin"] = "own" if ident and not record.get("events_incomplete") and all(
                    a.get("platform_actor_id") == ident for a in renamed) else "other"
            elif kind == "discussion" and old and not old.get("title_read"):
                result["title_origin"] = "other"
            # A record GitHub keeps for this change but has not returned yet is reread (ENRICH_RECORD_ATTEMPTS).
            return result, ("change_record_missing" if result.pop("reread", False) else None)
        except (TransportError, KeyError, TypeError, ValueError) as exc:
            code = getattr(exc, "code", "change_record_shape_unavailable")
            return {"status": "unavailable", "code": code, "records": []}, (code if transient(exc) else None)

    def first_origin(self, route, kind, raw, actor, history=()):
        """(origin, transient code) for an object first observed after it was written, by an author GitHub numbers:
        whether GitHub's own records prove its words that author's alone. None: nothing happened to it since it
        was written, its record says it was never edited, or that author alone edited and renamed it. ``foreign``:
        the record names another account for its text or title. ``unknown``: the record is incomplete, names an
        editor without a number, lacks an edit the object itself reports, or could not be read. The author having
        made the latest edit proves nothing. A Discussion's title has no record at all."""
        if untouched(kind, raw, history):
            return None, None
        if kind == "discussion":
            # Nothing records who set a Discussion's title: once anything has happened to it since it was opened, its
            # words are not proven its author's alone, whatever the edit record of its text says. No read.
            return {"status": "unknown", "code": "title_record_unavailable", "marks": {"title": True}}, None
        written, state, author = instant(created(raw)), word_state(raw), actor["platform_actor_id"]
        try:
            if kind in ("issue", "pr"):
                record = self.provider.change_record(route, since=(written - timedelta(seconds=60)).isoformat() if written else None)
            elif node_id(raw):
                record = self.provider.change_record(route, node=node_id(raw))
            else:
                raise TransportError("change_record_unaddressable")
            renamed = [self.provider.actor_of(event.get("actor"), strict=True) if event.get("actor") else unknown_actor()
                       for event in record.get("events") or [] if event.get("type") == "RenamedTitleEvent"]
        except (TransportError, KeyError, TypeError, ValueError) as exc:
            code = getattr(exc, "code", "change_record_shape_unavailable")
            return ({"status": "unknown", "code": "edit_record_unavailable", "error": code, "marks": {"body": True}},
                    code if transient(exc) else None)
        editors = word_editors(record)
        edited = bool(record.get("edits")) or bool(record.get("last_edited_at"))
        if not edited and not renamed and not record.get("events_incomplete"):
            if raw.get("lastEditedAt"):
                # The object itself reports an edit its record does not show yet: read again, never taken as unedited.
                return {"status": "unknown", "code": "edit_record_missing", "marks": {"body": True}}, "change_record_missing"
            # GitHub's record says it was never edited. A REST comment's update time moved all the same: the record
            # is read again (it may not show a fresh edit yet) before those words go out as their author's.
            return None, ("change_record_missing" if state == "changed" else None)
        people = {str(e["editor"]["databaseId"]): e["editor"] for e in record.get("edits") or []
                  if isinstance(e.get("editor"), dict) and type(e["editor"].get("databaseId")) is int}
        people.update({a["platform_actor_id"]: {"login": a.get("username")} for a in renamed if a.get("platform_actor_id")})
        others = sorted(set(people) - {author})
        marks = {**({"body": True} if set(editors["ids"]) - {author} or (edited and not (editors["complete"] and record.get("edits")))
                    else {}),
                 **({"title": True} if not all(a.get("platform_actor_id") == author for a in renamed)
                    or record.get("events_incomplete") else {})}
        if others:
            return {"status": "foreign", "code": "other_editor_recorded", "editors": others, "marks": marks or {"body": True},
                    "named": ["@" + str(login_of(people[i]) or "?") + " (id " + i + ")" for i in others]}, None
        if edited and not record.get("edits"):
            # Its record gives an edit time and no edit: a record GitHub has not returned yet is read again.
            return {"status": "unknown", "code": "edit_record_missing", "marks": marks}, "change_record_missing"
        if marks:
            return {"status": "unknown", "code": "edit_record_incomplete", "marks": marks}, None
        return None, None

    def recipient_login(self, recipient):
        for name, user_id in (("auth:writer", self.config["expected_writer_id"]), ("auth:notifications", self.config["notifications_user_id"])):
            saved = self.store.meta(name, {}) or {}
            if recipient == user_id and saved.get("state") == "valid" and saved.get("id") == recipient and saved.get("login"):
                return saved["login"]
        return next((u["login"] for u in self.config["watched_identities"] if u["id"] == recipient), None)

    def outside_accounts(self, recipient):
        """The accounts an outside thread can address: every watched account and this reader, with current logins."""
        accounts = [{"id": u["id"], "login": self.recipient_login(u["id"]) or u["login"]} for u in self.config["watched_identities"]]
        if recipient not in {a["id"] for a in accounts} and self.recipient_login(recipient):
            accounts.append({"id": recipient, "login": self.recipient_login(recipient)})
        return accounts

    def outside_key(self, route, identity):
        """One GitHub message or action in an unselected thread, by GitHub's own node id (a comment's, a timeline
        event's), whichever reader's notification revealed it; never by its time or actor."""
        return (self.account + ":outside:" + str(route["repo"]["id"]) + ":" + route["thread_kind"] + "."
                + str(route["number"]) + ":" + identity)

    @staticmethod
    def thread_of(route):
        return str(route["repo"]["id"]) + ":" + route["thread_kind"] + "." + str(route["number"])

    def supersede(self, db, route, at, holder):
        """A read of an outside thread tells every notification still waiting on an older copy of it that its copy
        is no longer the newest: when the thread was read, and which notification observation (``holder``) keeps
        those rows. Written into the waiting records themselves, in the transaction that commits what the read
        observed: the mark exists exactly as long as the older copy does (through any restart, never expiring by
        time) and goes only when that notification reads the thread itself or is completed."""
        db.execute("UPDATE meta SET value=json_set(value,'$.superseded',json(?)) WHERE key>='enrich:' AND key<'enrich;' "
                   "AND json_extract(value,'$.thread')=? AND key!=?",
                   (dump({"at": at, "holder": holder}), self.thread_of(route), "enrich:" + str(holder)))

    def retained_read(self, newest):
        """(rows, read at): the read a waiting notification was told of (``supersede``), or a later one, as the
        notification observation that made it keeps it (its waiting record, else its event); None when none is."""
        holder = newest.get("holder")
        waiting = self.store.meta("enrich:" + holder) if holder else None
        if waiting:
            rows, at = (waiting.get("kwargs") or {}).get("history"), (waiting.get("kwargs") or {}).get("history_at")
        else:
            found = self.store.one("SELECT event FROM inbox WHERE id=?", (holder,)) if holder else None
            history = ((json.loads(found["event"]).get("conversation") or {}).get("history") or {}) if found else {}
            rows, at = history.get("rows"), history.get("observed_at")
        return (rows, at) if isinstance(rows, list) and rows and at and not newer(newest.get("at"), at) else None

    @staticmethod
    def reworded(cached, fresh):
        """The messages an older copy of a thread showed that a newer one shows with other words, with another edit
        time of their own, or not at all: what was read of them for the older copy no longer describes them."""
        def shown(rows):
            return {node_id(row) or "row:" + str(row.get("id") or ""): (
                words(row) if "number" in row else row.get("body") or "",
                row.get("lastEditedAt") if "lastEditedAt" in row else None if "number" in row else row.get("updated_at"))
                for row in rows if isinstance(row, dict) and ("body" in row or "number" in row)}
        before, after = shown(cached), shown(fresh)
        return {ident for ident in before if after.get(ident) != before[ident]}

    def withdrawn(self, needs, cached, fresh, recipient):
        """Explicit gaps for what only an older copy of a thread showed: a message whose own read (its author, its
        edit record, its editor) a waiting notification was still retrying, and which the newer copy no longer shows
        naming every watched account the older one did. Never reported, never silently dropped."""
        accounts, writer = self.outside_accounts(recipient), self.config["expected_writer_id"]

        def names(rows):
            return {node_id(row) or "row:" + str(row.get("id") or ""): {m["id"] for m in mention_identities(
                words(row) if "number" in row else (row.get("body") or ""), accounts, writer)}
                for row in rows if isinstance(row, dict) and ("body" in row or "number" in row)}
        before, after = names(cached), names(fresh)
        waited = {need.split(":", 1)[1] for need in needs if need.startswith(("author:", "editor:", "edits:"))}
        return [{"scope": "notification", "code": "mention_withdrawn_unreported", "message_id": ident}
                for ident in sorted(waited) if before.get(ident, set()) - after.get(ident, set())]

    def unreached(self, needs, proof, rows, recipient):
        """The reads a released notification still had open that its last pass no longer reaches: a message whose
        words, or an action of a timeline that pass read, name no account Settings name now (a removed reader that is
        no watched account). They stay open, each an explicit gap, never dropped silently."""
        accounts = self.outside_accounts(recipient)
        named = {need["need"] for need in proof["needs"]} | {gap.get("need") for gap in proof["gaps"]} | {
            gap.get("need") for item in proof["items"] for gap in item.get("gaps") or []}
        withdrawn = {gap.get("message_id") for gap in proof["gaps"] if gap.get("code") == "mention_withdrawn_unreported"}
        shown = {node_id(row) or "row:" + str(row.get("id") or ""): words(row) if "number" in row else row.get("body") or ""
                 for row in rows or () if isinstance(row, dict) and ("body" in row or "number" in row)}

        def gone(need):
            kind, _, ident = need.partition(":")
            if kind == "actor":
                return "thread_record" not in named
            return kind in ("author", "editor", "edits") and ident not in withdrawn and not mentions(shown.get(ident, ""), accounts)
        return [{"need": need, "code": code} for need, code in needs.items() if need not in named and gone(need)]

    def thread_record(self, route, since):
        """The thread's change record, its timeline read back page by page within OUTSIDE_TIMELINE_PAGES. A later page
        that fails keeps what the newer pages already returned (``page_failure``); only the first page is required."""
        record = self.provider.change_record(route, since=since)
        for _ in range(OUTSIDE_TIMELINE_PAGES - 1):
            if not record.get("events_incomplete") or not record.get("events_cursor"):
                break
            try:
                earlier = self.provider.change_record(route, since=since, before=record["events_cursor"])
            except (TransportError, KeyError, TypeError, ValueError) as exc:
                return {**record, "page_failure": {"code": getattr(exc, "code", "change_record_shape_unavailable"),
                                                   "transient": transient(exc)}}
            record = {**record, "events": (earlier.get("events") or []) + (record.get("events") or []),
                      "events_incomplete": earlier.get("events_incomplete"), "events_cursor": earlier.get("events_cursor")}
        return record

    def prior_words(self, previous, row):
        """(words, read at): this row as my previous observation of the thread kept it, else (None, None)."""
        found = self.store.one("SELECT event FROM inbox WHERE id=?", (previous,)) if previous else None
        history = ((json.loads(found["event"]).get("conversation") or {}).get("history") or {}) if found else {}
        ident = node_id(row)
        for old in history.get("rows") or []:
            if isinstance(old, dict) and (node_id(old) == ident if ident else old.get("id") == row.get("id")):
                return (words(old) if "number" in old else (old.get("body") or "")), history.get("observed_at")
        return None, None

    def prove(self, notification, recipient, route, history, lower, previous=None, settle=False, last=False,
              records=None, reopen=()):
        """An outside notification's newly addressed GitHub messages and actions, inside the window since this reader
        last observed the thread (``lower``) up to the notification's own time. A reason alone proves nothing.

        Each proven item is one GitHub message or action (``outside_key``): another reader's notification of it adds
        evidence, never a second event. New words name a watched account when written inside the window, their
        author attributed; words an edit added need the edit's own record and the words before it, the editor
        attributed (several or none: unknown). An assignment or review request is its own timeline event. A read
        that fails, or answers without what GitHub points to yet, delays only what depends on it (``needs``);
        an incomplete record is an explicit gap, never an empty proof. ``settle``: the bounded rereads are spent, so
        what GitHub still has not shown settles as unattributed or as a gap, never as proof of its absence. ``last``:
        the bounded retries are spent, so an account lookup that still fails leaves that account unknown (an author
        the thread read numbered stays so), with the failure as its item's actor gap, instead of holding the item
        back. ``records``: what an earlier attempt of this same proof already read; a record is read again only when
        that attempt left it (or what it is read for, ``reopen``) open. ``history`` is the newest read of the thread,
        never a copy a later read superseded (``enrich_discovery``); ``None``: there is none to prove from, so nothing
        is proven. ``proof["seen"]``: an already reported message as this read shows it, when its words name no
        account its events have not: its retained words follow (a mention added back later is new again), or only
        their time when they are the same."""
        upper = (instant(notification.get("updated_at")) or datetime.now(timezone.utc)) + timedelta(seconds=5)
        low = instant(lower)
        accounts = self.outside_accounts(recipient)
        writer = self.config["expected_writer_id"]
        login = next((a["login"] for a in accounts if a["id"] == recipient), None)
        proof = {"items": [], "needs": [], "gaps": [], "seen": [], "window": [lower, upper.isoformat()],
                 "recipient_login": login}
        if not login:
            proof["gaps"].append({"scope": "notification", "code": "recipient_login_unavailable"})
        if history is None:
            return {**proof, "records": dict(records or {})}
        kept, records, reopen = dict(records or {}), {}, set(reopen)

        def record(need, read, also=()):
            """One read per attempt: a transient failure is this need, a definite one an unavailable record."""
            if need not in records:
                if need in kept and not ({need, *also} & reopen):
                    records[need] = kept[need]
                else:
                    try:
                        records[need] = read()
                    except (TransportError, KeyError, TypeError, ValueError) as exc:
                        code = getattr(exc, "code", "change_record_shape_unavailable")
                        records[need] = _Pending(need, code) if transient(exc) else {"unavailable": code, "events": [], "edits": []}
            if isinstance(records[need], _Pending):
                raise records[need]
            return records[need]

        opening = next((row for row in history if isinstance(row, dict) and "number" in row), None)
        # The thread's record also answers for a named action not yet visible and for its opening's own edits.
        asked_of_thread = ("action_record",) + (
            ("edits:" + (node_id(opening) or "row:" + str(opening.get("id") or "")),) if opening else ())

        def thread():
            return record("thread_record", lambda: self.thread_record(route, lower), asked_of_thread)

        def seen(row, current):
            ident = node_id(row) or ("row:" + str(row.get("id") or ""))
            key = self.outside_key(route, ident)
            stored = self.store.one("SELECT value FROM snapshots WHERE key=?", (key,))
            if not stored:
                return
            reported = {m["id"] for m in mention_identities(json.loads(stored["value"]).get("text") or "", accounts, writer)}
            # Words naming an account no event of this message reported stay out of what is retained: a pass that
            # can prove them (another reader's window, a later attempt) still finds them new.
            if {m["id"] for m in mention_identities(current, accounts, writer)} <= reported:
                proof["seen"].append({"key": key, "snapshot": {"comparison": comparison(row), "text": current, "raw": row}})

        def resolve(need, read, gaps):
            try:
                return read()
            except TransportError as exc:  # strict lookups raise only what may succeed later
                if not last:
                    raise _Pending(need, exc.code) from None
                gaps.append({"scope": "actor", "code": exc.code, "need": need})
                return dict(getattr(exc, "actor", None) or unknown_actor())  # a known Bot stays a bot

        for row in history:
            if not isinstance(row, dict) or not ("body" in row or "number" in row):
                continue
            current = words(row) if "number" in row else (row.get("body") or "")
            if not mentions(current, accounts):
                seen(row, current)
                continue
            try:
                item = self.mention_item(row, current, accounts, writer, route, previous, low, upper, record, thread, resolve,
                                         settle)
            except _Pending as pending:
                proof["needs"].append({"need": pending.need, "code": pending.code})
                continue
            if item and item.get("gap"):
                proof["gaps"].append(item["gap"])
            elif item:
                proof["items"].append(item)
            if not item or item.get("gap") or item.get("reported"):
                seen(row, current)  # settled without a new event: nothing of it is still being read
        if route["thread_kind"] in ("issue", "pr"):
            try:
                self.action_items(proof, notification, recipient, accounts, thread(), low, upper, resolve, route, settle)
            except _Pending as pending:
                proof["needs"].append({"need": pending.need, "code": pending.code})
        pointer = re.search(r"/(?:issues|pulls)/comments/(\d+)$", str((notification.get("subject") or {}).get("latest_comment_url") or ""))
        if pointer and not any(isinstance(row, dict) and str(row.get("id")) == pointer[1] for row in history):
            # The notification names a comment this thread read does not show yet: read the thread again.
            need = {"need": "history", "code": "latest_comment_not_yet_visible"}
            (proof["gaps"] if settle else proof["needs"]).append(need_gap(need) if settle else need)
        proof["needs"] = list({need["need"]: need for need in proof["needs"]}.values())
        proof["records"] = {need: value for need, value in records.items()
                            if isinstance(value, dict) and not value.get("unavailable") and not value.get("page_failure")}
        return proof

    def thread_author(self, row, strict=False):
        """An outside thread row's author (``strict``: a read that decides attention, as ``GitHub.actor``). A lookup
        that cannot number it now leaves the number the read of this row gave it (``kept_author``); a transient
        failure is raised only when there is none. A newly resolved number stays on this row for the next attempt."""
        try:
            actor = kept_author(row, self.provider.actor(row, strict=strict))
            if actor.get("platform_actor_id"):
                row["_actor_id"] = actor["platform_actor_id"]
            return actor
        except TransportError as exc:
            kept = kept_author(row, getattr(exc, "actor", None) or unknown_actor())
            if not kept.get("platform_actor_id"):
                raise
            return kept

    def mention_item(self, row, current, accounts, writer, route, previous, low, upper, record, thread, resolve,
                     settle=False):
        """The proof item for one row whose words name a watched account now, a gap, or None (not new here)."""
        written = instant(created(row))
        if written and written > upper:
            return None  # written after this notification: the next update's window
        ident = node_id(row) or ("row:" + str(row.get("id") or ""))
        key = self.outside_key(route, ident)
        named = mention_identities(current, accounts, writer)
        written_here = bool(written) and (not low or written > low)
        # The words before: this message's own earlier event, else (written before this window) my previous
        # observation of the thread. Words written inside the window are new words whatever an earlier read saw.
        stored = self.store.one("SELECT value,observed FROM snapshots WHERE key=?", (key,))
        prior, read_at = ((json.loads(stored["value"]).get("text") or ""), stored["observed"]) if stored \
            else (None, None) if written_here else self.prior_words(previous, row)

        def edits():
            """(edited, edits made after the row was written, record complete, record missing what the row shows)."""
            state = word_state(row)
            if state == "unedited":
                return False, [], True, False
            if "number" in row and route["thread_kind"] in ("issue", "pr"):
                found = thread()
            elif node_id(row):
                found = record("edits:" + ident, lambda: self.provider.change_record(route, node=node_id(row)))
            else:
                found = {"unavailable": "change_record_unaddressable", "edits": []}
            made = [e for e in found.get("edits") or [] if not e.get("deleted") and instant(e.get("edited_at"))
                    and (not written or instant(e["edited_at"]) > written + _SLACK)]
            last = instant(found.get("last_edited_at"))
            shown = state == "changed" or bool(last and (not written or last > written + _SLACK))
            edited = bool(made) or shown or bool(found.get("unavailable"))
            # A row (or its record's edit time) saying the words changed while no edit came back: a record GitHub
            # keeps but has not returned, or an incomplete one; never an unedited row.
            missing = shown and not made and not found.get("unavailable")
            return edited, made, not (found.get("unavailable") or found.get("edits_incomplete") or (edited and not made)), missing

        def editor(edit):
            return resolve("editor:" + ident, lambda: self.provider.actor_of(edit.get("editor"), strict=True), item["gaps"]) \
                if edit.get("editor") else unknown_actor()
        spans = mentions(current, accounts)
        item = {"fact": "mention", "key": key, "message_id": ident if node_id(row) else None, "raw": row, "mentions": named,
                "snapshot": {"comparison": comparison(row), "text": current, "raw": row}, "evidence": {"mention": spans},
                "lines": [], "gaps": []}
        if prior is not None:
            before_ids = {m["id"] for m in mention_identities(prior, accounts, writer)}
            added = [m for m in named if m["id"] not in before_ids]
            if not added:
                # Already reported as this message (its words then named them), or named before this window.
                return {"fact": "mention", "key": key, "reported": True} if stored and written_here else None
            edited, made, complete, _ = edits()
            seen = instant(read_at)
            later = [e for e in made if not seen or instant(e["edited_at"]) > seen]
            if not later:
                if not edited:  # its words are as written: the earlier copy is not of these words
                    return None
                if settle:
                    return {"gap": {"scope": "notification", "code": "mention_revision_unproven", "message_id": ident}}
                raise _Pending("edits:" + ident, "change_record_missing")
            if low and any(instant(e["edited_at"]) <= low for e in later):
                return {"gap": {"scope": "notification", "code": "mention_revision_unproven", "message_id": ident}}
            actors = [editor(e) for e in later]
            item.update(change=True, author=self.thread_author(row), edited_at=provider_time(max(
                (e["edited_at"] for e in later), key=instant)))
            item["evidence"] = {"mention": [s for s in spans if str(s["user_id"]) in {m["id"] for m in added}],
                                "change_actor": {"records": [{"change": "body", "source": "UserContentEdit", "at": e["edited_at"],
                                                              "actor_id": a.get("platform_actor_id"), "login": a.get("username")}
                                                             for e, a in zip(later, actors)]}}
            item["lines"] = ["Edited this comment to mention " + ", ".join(
                "@" + str(m["name"] or "?") + " (id " + m["id"] + ")" for m in added) + "."]
            return self.settle_actor(item, actors, complete)
        if written_here:
            author = resolve("author:" + ident, lambda: self.thread_author(row, strict=True), item["gaps"])
            edited, made, complete, missing = edits()
            if missing and not settle:
                raise _Pending("edits:" + ident, "change_record_missing")  # reread before deciding whose words
            actors = [editor(e) for e in made]
            if not edited or (complete and all(same_account(a, author) for a in actors)):
                # Written in this interval by its author alone: new words, whoever the author is.
                item.update(change=False, actor=author)
                return item
            # Written and edited in this interval by different (or unknown) accounts: who added the mention is not
            # recorded. Kept as an unattributed change, never as the author's or an editor's words.
            item.update(change=True, author=author, actor=unknown_actor(), edited_at=provider_time(max(
                (e["edited_at"] for e in made), key=instant)) if made else "")
            item["lines"] = ["This comment was written by " + _who(author) + " and edited by "
                             + (", ".join(sorted({_who(a) for a in actors})) or "an unidentified account")
                             + " in this interval; who added the mention is not recorded."]
            item["attribution"] = "Several accounts wrote this text in this interval; no single actor is attributed."
            item["forward_origin"] = word_origin(author)
            item["gaps"].append({"scope": "actor", "code": "change_actor_unresolved" if any(
                not a.get("platform_actor_id") for a in actors) else "multiple_change_actors" if actors else
                "change_record_missing" if missing else "change_record_incomplete"})
            item["evidence"]["change_actor"] = {"status": "ambiguous", "records": [
                {"change": "body", "source": "UserContentEdit", "at": e["edited_at"], "actor_id": a.get("platform_actor_id"),
                 "login": a.get("username")} for e, a in zip(made, actors)]}
            return item
        edited, made, _, _ = edits()
        if edited and (not made or not low or any(instant(e["edited_at"]) > low for e in made)):
            # Written earlier and edited since, without an earlier copy of its words: a mention may be old.
            return {"gap": {"scope": "notification", "code": "mention_revision_unproven", "message_id": ident}}
        return None

    @staticmethod
    def settle_actor(item, actors, complete):
        """An edit item's actor: the one account GitHub's records name for every edit that added its words."""
        identities = {a.get("platform_actor_id") or ("unresolved", a.get("username"), bool(a.get("is_bot"))) for a in actors}
        if complete and len(identities) == 1 and actors[0].get("platform_actor_id"):
            item["actor"] = actors[0]
            item["attribution"] = ("GitHub records " + _who(actors[0]) + " as having made this change." if same_account(
                actors[0], item["author"]) else "Edited by " + _who(actors[0]) + "; the text was first written by "
                + _who(item["author"]) + ". Who wrote which part is not recorded separately.")
        else:
            code = "change_record_incomplete" if not complete else "multiple_change_actors" if len(identities) > 1 \
                else "change_actor_unresolved"
            item["actor"] = actors[0] if len(identities) == 1 and actors[0].get("is_bot") else unknown_actor()
            item["attribution"] = "Who made this change is not established (" + code + ")."
            item["gaps"].append({"scope": "actor", "code": code})
        if not same_account(item["actor"], item["author"]):
            item["forward_origin"] = word_origin(item["author"])
        return item

    def action_items(self, proof, notification, recipient, accounts, record, low, upper, resolve, route, settle=False):
        """Assignment and review-request timeline events naming a watched account inside the window: one item per
        timeline event, keyed by its own node id. What newer pages proved stays when a later page fails."""
        if record.get("unavailable"):
            proof["gaps"].append({"scope": "notification", "code": "addressed_action_unverified", "error": record["unavailable"]})
            return
        ids = {str(a["id"]) for a in accounts}
        logins = {str(a["id"]): a["login"] for a in accounts}
        waiting = set()  # (fact, target) of actions whose actor is still being read
        for event in record.get("events") or []:
            target = (event.get("assignee") if event.get("type") == "AssignedEvent" else
                      event.get("reviewer") if event.get("type") == "ReviewRequestedEvent" else None)
            moment = instant(event.get("created_at"))
            if (not target or target.get("typename") == "Team" or str(target.get("id")) not in ids or not moment
                    or moment > upper or (low and moment <= low)):
                continue
            ident = event.get("id")
            if not isinstance(ident, str) or not ident:  # no identity of its own: never merged with another by time
                proof["gaps"].append({"scope": "notification", "code": "addressed_action_unaddressable",
                                      "type": event.get("type"), "at": event.get("created_at")})
                continue
            fact = "assigned" if event["type"] == "AssignedEvent" else "review_requested"
            who, at, gaps = str(target.get("id")), moment.isoformat(), []
            try:
                actor = resolve("actor:" + ident, lambda: self.provider.actor_of(event.get("actor"), strict=True), gaps)
            except _Pending as pending:  # only this action waits for its actor; the other proven ones go out now
                proof["needs"].append({"need": pending.need, "code": pending.code})
                waiting.add((fact, who))
                continue
            proof["items"].append({
                "fact": fact, "key": self.outside_key(route, ident), "message_id": None, "raw": event, "change": True,
                "actor": actor, "author": actor,
                "snapshot": {"comparison": {"id": ident, "type": event["type"], "created_at": at, "targets": [who]}, "raw": event},
                "evidence": {fact: [{"event_id": ident, "at": at, "actor_id": actor.get("platform_actor_id"), "target_id": who}]},
                "lines": [("Assigned " if fact == "assigned" else "Requested a review from ") + "@" + str(target.get("login")
                          or logins.get(who) or "?") + " (id " + who + ")"
                          + (", by " + _who(actor) if actor.get("platform_actor_id") or actor.get("username") else "") + "."],
                "gaps": gaps or ([] if actor.get("platform_actor_id") else [{"scope": "actor", "code": "change_actor_unresolved"}])})
        failure = record.get("page_failure")
        if failure and failure["transient"]:
            raise _Pending("thread_record", failure["code"])  # the rest of the timeline is read again
        if failure:
            proof["gaps"].append({"scope": "notification", "code": "addressed_action_unverified", "error": failure["code"]})
        elif record.get("events_incomplete"):
            proof["gaps"].append({"scope": "notification", "code": "addressed_action_record_incomplete",
                                  "pages": OUTSIDE_TIMELINE_PAGES})
        named = {"assign": "assigned", "review_requested": "review_requested"}.get(notification.get("reason"))
        if named and not failure and not record.get("events_incomplete") and (named, str(recipient)) not in waiting and not any(
                item["fact"] == named and str(recipient) in item["snapshot"]["comparison"]["targets"] for item in proof["items"]):
            # GitHub's reason names this kind of action for this reader; its timeline event may not be visible yet.
            need = {"need": "action_record", "code": "addressed_action_not_yet_visible"}
            if not settle:
                raise _Pending(need["need"], need["code"])
            proof["gaps"].append(need_gap(need, notification.get("reason")))

    def emit_outside(self, db, proof, discovery):
        """Each proven item as its own immutable event, keyed by its GitHub message or action, before the
        notification's own observation; one already recorded is evidence only. Its source ids, new or kept."""
        ids, read_at = [], discovery.get("history_at")
        for seen in proof.get("seen") or []:
            self.store.reobserve(db, seen["key"], seen["snapshot"], read_at)
        for item in proof["items"]:
            row = db.execute("SELECT revision FROM snapshots WHERE key=?", (item["key"],)).fetchone()
            if row and (item.get("reported") or not item.get("change") or item["fact"] != "mention"):
                ids.append("ghobs:" + digest(item["key"]) + ":" + str(row["revision"]))
                continue
            if item.get("reported"):
                continue
            eid = self.store.observe(db, item["key"], item["snapshot"], lambda eid, rev, old, item=item: outside_item_event(
                eid, rev, old, config=self.config, item=item, window=proof["window"], login=proof.get("recipient_login"),
                **{k: discovery.get(k) for k in ("notification", "recipient", "repo", "target", "route", "history",
                                                 "history_at")}), observed=read_at)
            if eid:
                ids.append(eid)
            elif row:
                ids.append("ghobs:" + digest(item["key"]) + ":" + str(row["revision"]))
        return ids

    def prepare(self, spec, rows):
        repo, kind = spec["repo"], spec["kind"]
        self.pending, self.merged = {}, set()
        if kind == "pull":
            # A pull request is one semantic object, observed through the Issues feed. A details page an older
            # version queued is retired without a second event for the same change.
            return [], []
        observations, children, history_cache, parent_cache = [], [], {}, {}
        context = self.capabilities()
        watched_ids = {str(u["id"]) for u in self.config["watched_identities"]}
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
                self.remember_root(repo, number, thread_kind, raw)
                if thread_kind == "pr":
                    for child_kind, suffix in (("reviews", "reviews?per_page=100"), ("requests", "requested_reviewers")):
                        children.append({"kind": child_kind, "repo": repo, "number": number,
                                         "created_at": created(raw),
                                         "path": base + "/pulls/" + str(number) + "/" + suffix})
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
                    self.remember_root(repo, number, thread_kind, parent_cache[number])
                object_kind = "issue_comment" if kind == "comments" else "inline_comment"
            elif kind in ("reviews", "requests"):
                number, thread_kind = spec["number"], "pr"
                object_kind = {"reviews": "review", "requests": "review_request"}[kind]
                if kind == "requests":
                    raw["id"] = number
                    raw["created_at"] = spec.get("created_at", "")
            else:
                thread_kind = "discussion"
                number = raw["number"] if kind == "discussions" else spec["number"]
                object_kind = "discussion" if kind == "discussions" else "discussion_comment"
                if kind == "discussions":
                    self.remember_root(repo, number, "discussion", raw)
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
            existing = self.store.one("SELECT revision,fingerprint,value,observed FROM snapshots WHERE key=?", (key,))
            old = json.loads(existing["value"]) if existing else None
            if existing and existing["fingerprint"] == digest(cmp):
                if not (object_kind == "pr" and merged_since(old, raw)):
                    observations.append((key, {"comparison": cmp}, None, False))
                    continue
                self.merged.add(key)  # merged while its compared fields stayed the same: a revision all the same
            try:
                actor, author_failure = self.provider.actor(raw, strict=True), None
            except TransportError as exc:  # meanwhile what is known: a known Bot stays a bot
                actor, author_failure = dict(getattr(exc, "actor", None) or unknown_actor()), exc.code
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
            fetched = not (baseline and not existing) and not echo
            if not fetched:
                history, gap, failed, read_at = [], ("baseline_history_not_fetched" if not echo else
                                                     "self_echo_retained_without_history_fetch"), False, None
            else:
                if thread not in history_cache:
                    history_cache[thread] = self.history(route)
                history, gap, failed, read_at = history_cache[thread]
            if echo:
                baseline = "echo"
            root = (self.store.meta("thread_root:" + str(repo["id"]) + ":" + str(number))
                    if object_kind in NEW_WORDS_COMMENTS else None)
            needs, attribution = [], None
            # Nobody was asked to review a pull request first seen without requests: retain the snapshot only.
            quiet = (context["change"] and object_kind == "review_request" and not existing
                     and not raw.get("users") and not raw.get("teams"))
            if (fetched and not quiet and context["change"] and not first_observed_update
                    and (existing or object_kind == "review_request")):
                attribution, failure = self.change_attribution(route, object_kind, old, raw, existing and existing["observed"],
                                                               actor)
                if failure:
                    needs.append({"need": "attribution", "code": failure})
            if (failed and not existing and not first_observed_update and object_kind in NEW_WORDS_COMMENTS
                    and not (root and str(root.get("actor_id") or "") in watched_ids)):
                # Whether these new words are in my thread depends on this read; retry it before the first offer.
                needs.append({"need": "history", "code": gap})
            if author_failure and fetched and not existing and not first_observed_update and object_kind != "review_request":
                # Whose new words these are (my own account, my human's, a bot) decides attention: read it again.
                needs.append({"need": "author", "code": author_failure})
            origin = None
            if (fetched and not existing and not first_observed_update and text and object_kind != "review_request"
                    and not untouched(object_kind, raw, history)):
                # New words first observed after they may have been edited: whether they are their author's alone is
                # GitHub's own record to say, on any host. A Bot, or an author without a number, carries no person's
                # words: ordinary new speech, no read.
                if actor.get("is_bot"):
                    pass
                elif actor.get("platform_actor_id"):
                    origin, failure = self.first_origin(route, object_kind, raw, actor, history)
                    if failure:
                        needs.append({"need": "origin", "code": failure})
                elif author_failure:
                    needs.append({"need": "origin", "code": author_failure})  # decided once its author is known
            # Whose words a later revision carries: this revision's own mark is final only once its attribution is.
            # While that is still being read the snapshot carries the earlier marks alone (``enrich`` adds this
            # one), and no revision observed meanwhile takes its words for their author's (``unsettled``): every
            # earlier revision of this object still waiting counts, read as one range of its own retry records.
            prefix = "enrich:ghobs:" + digest(key) + ":"
            unsettled = bool(existing) and any(
                need.get("need") in ("attribution", "origin") for waiting in self.store.rows(
                    "SELECT value FROM meta WHERE key>=? AND key<?", (prefix, prefix[:-1] + ";"))
                for need in json.loads(waiting["value"]).get("needs") or [])
            marks = ((old.get("foreign_words") or {}) if any(need["need"] == "attribution" for need in needs)
                     else foreign_words(old, raw, attribution, actor)) if old else dict(
                (origin or {}).get("marks") or {}) if not any(need["need"] == "origin" for need in needs) else {}
            if marks:
                snapshot["foreign_words"] = marks
            if object_kind in ("issue", "pr", "discussion") and (
                    (old or {}).get("title_read") or (attribution or {}).get("title_origin") or (not existing and not older)):
                # Its title's origin is read (new words: at first sight; an older object: with its first revision):
                # from here on every observed title change is judged as it happens.
                snapshot["title_read"] = True
            kwargs = dict(account=self.account, repo=repo, thread=thread, kind=object_kind, raw=raw,
                          actor=actor, route=route, watched=self.config["watched_identities"],
                          history=history, history_gap=gap, first_observed_update=first_observed_update,
                          change_context=context["change"], attribution=attribution, root_author=root,
                          writer_id=self.config["expected_writer_id"], signals=context["signals"],
                          unsettled=unsettled, history_at=read_at, origin=origin)
            notification_evidence = []
            for record in self.store.rows("SELECT value FROM notifications WHERE repo_id=? AND number=?", (repo["id"], number)):
                item = json.loads(record["value"])
                match = re.search(r"/(?:issues|pulls|discussions)/(\d+)$", (item.get("subject") or {}).get("url") or "")
                if item.get("repo_id") == repo["id"] and match and int(match[1]) == number:
                    notification_evidence.append(item)
            kwargs["notification"] = notification_evidence
            if needs:
                self.pending[key] = {"needs": needs, "old": old, "observed_at": existing and existing["observed"],
                                     "key": key}
            observations.append((key, snapshot, None if quiet else kwargs, baseline))
        return observations, children

    def defer(self, db, event_id, factory, kwargs, pending, stream, spec):
        """Keep a new event out of dispatch while one decisive read is retried through the job queue."""
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("enrich:" + event_id, dump(
            {"factory": factory, "kwargs": kwargs, "old": pending.get("old"), "observed_at": pending.get("observed_at"),
             "key": pending.get("key"), "needs": pending["needs"], "attempts": 1, "first_at": time.time(),
             **({"thread": pending["thread"]} if pending.get("thread") else {})})))
        db.execute("INSERT OR IGNORE INTO jobs(stream,spec,due) VALUES(?,?,?)",
                   (stream, dump({**spec, "event_id": event_id}), time.time() + 60))

    def enrich(self, job, spec, release=False):
        """Retry the reads a waiting event still lacks; complete it once, or go out with the failure as an explicit
        gap. What an attempt did read stays with the event: only the reads still open are tried again, so a later
        failure never takes back what an earlier attempt established. ``release``: no read at all, the event goes
        out now as its bounded retries would have ended (``release_waiting``); a string names why (its gap code)."""
        event_id = spec["event_id"]
        saved = self.store.meta("enrich:" + event_id)
        row = self.store.one("SELECT state,event FROM inbox WHERE id=?", (event_id,))
        if not saved or not row or row["state"] != "enriching":
            with self.store.transaction() as db:
                # Never strand an event: without its retry record it goes out as it was captured.
                db.execute("UPDATE inbox SET state='ready',due=0 WHERE id=? AND state='enriching'", (event_id,))
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
            return True
        kwargs = dict(saved["kwargs"])
        captured = (json.loads(row["event"]).get("message") or {}).get("observed_at")
        if saved["factory"] == "discovery":
            return self.enrich_discovery(job, event_id, saved, kwargs, captured, release)
        needs = {need["need"]: need.get("code") for need in saved["needs"]}  # still open, each with its last failure
        failed = dict(needs) if release else {}
        if "author" in needs and not release:
            try:
                kwargs["actor"] = self.provider.actor(kwargs["raw"], strict=True)
            except TransportError as exc:
                failed["author"] = exc.code
        if "history" in needs and not release:
            rows, gap, retryable, read_at = self.history(kwargs["route"], kwargs.get("history") or ())
            if rows or not kwargs.get("history"):  # a read that returned nothing never replaces rows already read
                kwargs.update(history=rows, history_gap=gap, history_at=read_at)
            if retryable:
                failed["history"] = gap
        if "origin" in needs and not release:
            if kwargs["actor"].get("platform_actor_id") and not kwargs["actor"].get("is_bot"):
                kwargs["origin"], failure = self.first_origin(kwargs["route"], kwargs["kind"], kwargs["raw"], kwargs["actor"],
                                                              kwargs.get("history") or ())
                if failure:
                    failed["origin"] = failure
            elif "author" in failed:
                failed["origin"] = failed["author"]  # still waits for its author
        if "attribution" in needs and not release:
            attribution, failure = self.change_attribution(kwargs["route"], kwargs["kind"], saved["old"], kwargs["raw"],
                                                           saved.get("observed_at"), kwargs["actor"])
            # A read that answered (even without the record yet) replaces the result; a failed one only another failure.
            if failure in (None, "change_record_missing") or (kwargs.get("attribution") or {}).get("status") == "unavailable":
                kwargs["attribution"] = attribution
            if failure:
                failed["attribution"] = failure
        attempts, deadline = saved["attempts"] + 1, saved["first_at"] + ENRICH_SECONDS
        # A record GitHub has not returned yet gets a few rereads; a failed read the full bounded retry.
        limit = ENRICH_RECORD_ATTEMPTS if all(code == "change_record_missing" for code in failed.values()) else ENRICH_ATTEMPTS
        if failed and not release and attempts < limit and time.time() < deadline:
            retry_at = self.store.meta("quota:" + self.provider.credential_name, {}).get("retry_at", 0)
            due = min(max(retry_at, time.time() + 60 * 2 ** (attempts - 1)), deadline)
            self.store.set_meta("enrich:" + event_id, {**saved, "kwargs": kwargs, "attempts": attempts, "last_failures": failed,
                                                        "needs": [{"need": need, "code": code} for need, code in failed.items()]})
            self.store.gap("page:" + str(job["id"]), next(iter(failed.values())), {"event_id": event_id, "attempts": attempts})
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET due=? WHERE id=?", (due, job["id"]))
            return False
        revision, state = int(event_id.rsplit(":", 1)[1]), "ready"
        value = make_event(event_id, revision, saved["old"], **kwargs, observed_at=captured)
        gaps = value["message"]["coverage_gaps"]
        if "author" in failed:
            gaps.append({"scope": "actor", "code": failed["author"]})
        elif saved["old"] is None and is_echo(self.store, kwargs["route"], kwargs["raw"], kwargs["actor"]):
            state = "echo"  # its author, read late, makes it my own receipted send: retained, never offered
        if release:
            gaps.append({"scope": "enrichment", "code": release if isinstance(release, str) else "released_before_retries_finished",
                         "needs": sorted(failed)})
        # This revision's own mark on its object's words, now that its attribution is final (``prepare``).
        marks = (foreign_words(saved["old"], kwargs["raw"], kwargs.get("attribution"), kwargs["actor"]) if saved["old"]
                 else dict((kwargs.get("origin") or {}).get("marks") or {}))
        title_read = bool((kwargs.get("attribution") or {}).get("title_origin"))
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET event=?,state=?,due=0 WHERE id=? AND state='enriching'", (dump(value), state, event_id))
            current = (db.execute("SELECT value FROM snapshots WHERE key=?", (saved.get("key") or "",)).fetchone()
                       if marks or title_read else None)
            if current:
                snapshot = json.loads(current["value"])
                merged = {**snapshot, **({"title_read": True} if title_read else {}),
                          **({"foreign_words": {**(snapshot.get("foreign_words") or {}), **marks}} if marks else {})}
                if merged != snapshot:
                    db.execute("UPDATE snapshots SET value=? WHERE key=?", (dump(merged), saved["key"]))
            db.execute("DELETE FROM meta WHERE key=?", ("enrich:" + event_id,))
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
        self.store.resolve("page:" + str(job["id"]))
        return True

    def enrich_discovery(self, job, event_id, saved, kwargs, captured=None, release=False):
        """Prove an outside notification again: items proven now go out at once (each once, by its key); the
        notification's own observation waits only while a need remains, then carries what stayed open as gaps.
        The thread and the records an attempt read, the items it proved and the gaps it found stay with the waiting
        observation."""
        lower, previous, history_at = kwargs.pop("lower", None), kwargs.pop("previous", None), kwargs.pop("history_at", None)
        records, proven = kwargs.pop("records", None) or {}, list(kwargs.pop("items", None) or [])
        lost = list(kwargs.pop("lost", None) or [])
        needs = {need["need"]: need.get("code") for need in saved["needs"]}
        # A later read of this thread, by any notification of it, told this waiting one (``supersede``): its own copy
        # is no longer the newest and proves nothing any more. It proves from the newest read, as the notification
        # that made it keeps it, under that read's own time: no network read, so no failed read can stand in for one.
        # Its own copy stays what this notification itself read: what its reader's next update compares with.
        newest = saved.get("superseded")
        adopted = self.retained_read(newest) if newest else None
        rows, read_at = adopted or ((None, None) if newest else (kwargs["history"], history_at))
        own, unread, reread, reopen = kwargs["history"], None, False, set(needs)
        if release:
            if "history" in needs and not adopted:
                unread = ({"need": "history", "code": needs["history"]}, False)
        elif "history" in needs or (newest and not adopted):
            # A read of the thread still open, or a superseded copy with no newer read kept: read it again. A failure
            # matters only when there is no newer read to prove from.
            try:
                kwargs["repo"], kwargs["target"], kwargs["route"], kwargs["history"] = public_subject(self.provider, kwargs["notification"])
                rows, read_at, reread = kwargs["history"], now(), True
                history_at = read_at
            except (TransportError, KeyError, TypeError) as exc:
                if not adopted:
                    unread = ({"need": "history", "code": getattr(exc, "code", "notification_subject_unavailable")}, transient(exc))
        attempts, deadline = saved["attempts"] + 1, saved["first_at"] + ENRICH_SECONDS
        # Rereads of what GitHub has not shown yet get ENRICH_RECORD_ATTEMPTS; then they settle, never block.
        settle = release or attempts >= ENRICH_RECORD_ATTEMPTS or time.time() >= deadline
        # On the last bounded attempt an account lookup that still fails leaves that account unknown, never the item.
        last = release or attempts >= ENRICH_ATTEMPTS or time.time() >= deadline
        if rows is not None and rows is not own:
            # It proves from a later read than its copy (another notification's, or its own just now). What only that
            # older copy showed and was still being read for is an explicit gap, kept with the waiting observation; of
            # the records read for that copy, those of messages the later read shows as it did stay, the others are
            # read again. An author that copy numbered stays numbered where the later read's lookup failed.
            kept_numbers(rows, own)
            lost += [gap for gap in self.withdrawn(needs, own, rows, kwargs["recipient"]) if gap not in lost]
            reopen |= {"edits:" + ident for ident in self.reworded(own, rows)}
        # ``rows`` None: still only a superseded copy (no newer read is kept and the thread could not be read). It is
        # not proven from, on any attempt: its reads stay open while a retry can read the thread, then end as gaps.
        proof = self.prove(kwargs["notification"], kwargs["recipient"], kwargs["route"], rows, lower, previous,
                           settle=settle, last=last, records=records, reopen=reopen)
        if rows is not None and rows is not kwargs["history"]:
            # An adopted read can first resolve an author here. Keep that same message/login's number in this
            # waiting copy, without replacing its words or the newer read it must still prove from next time.
            kept_numbers(kwargs["history"], rows)
        proof["gaps"] += lost
        if rows is None:
            proof["needs"] = list(saved["needs"])
        if release:  # nothing was read now: a read still open keeps the failure its last real attempt met
            proof["needs"] = [{**need, "code": needs.get(need["need"]) or need["code"]} for need in proof["needs"]]
            proof["needs"] += self.unreached(needs, proof, rows, kwargs["recipient"])
        if unread:  # the thread read again failed: its last read stays, with this failure
            need, again = unread
            proof["needs"] = [item for item in proof["needs"] if item["need"] != "history"] + ([need] if again else [])
            if not again:
                proof["gaps"].append(need_gap(need))
        limit = ENRICH_RECORD_ATTEMPTS if all(need["code"] in REREADS for need in proof["needs"]) else ENRICH_ATTEMPTS
        # A superseded copy that no retry can replace (a definite failure of the read) ends now, as on its last attempt.
        final = (release or not proof["needs"] or attempts >= limit or time.time() >= deadline
                 or (rows is None and bool(unread) and not unread[1]))
        due = None
        if not final:
            retry_at = self.store.meta("quota:" + self.provider.credential_name, {}).get("retry_at", 0)
            due = min(max(retry_at, time.time() + 60 * 2 ** (attempts - 1)), deadline)
        with self.store.transaction() as db:
            if reread:
                self.supersede(db, kwargs["route"], history_at, event_id)
            items = self.emit_outside(db, proof, {**kwargs, "history": rows, "history_at": read_at})
            items = proven + [item for item in items if item not in proven]
            if final:
                proof["gaps"] += [need_gap(need, kwargs["notification"].get("reason")) for need in proof["needs"]]
                if release and proof["needs"]:  # only when a read was left open
                    proof["gaps"].append({"scope": "enrichment", "code": release if isinstance(release, str) else
                                          "released_before_retries_finished",
                                          "needs": sorted(need["need"] for need in proof["needs"])})
                value = discovery_event(event_id, int(event_id.rsplit(":", 1)[1]), None, config=self.config, proof=proof,
                                        items=items, previous=previous, observed_at=captured, history_at=history_at, **kwargs)
                db.execute("UPDATE inbox SET event=?,state='ready',due=0 WHERE id=? AND state='enriching'", (dump(value), event_id))
                db.execute("DELETE FROM meta WHERE key=?", ("enrich:" + event_id,))
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
            else:
                # The mark goes only with the superseded copy: when this notification has read the thread itself.
                waiting = {name: value for name, value in saved.items() if name != "superseded" or not reread}
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("enrich:" + event_id, dump(
                    {**waiting, "attempts": attempts, "needs": proof["needs"],
                     "kwargs": {**kwargs, "lower": lower, "previous": previous, "history_at": history_at,
                                "records": proof["records"], "items": items, **({"lost": lost} if lost else {})},
                     "last_failures": {need["need"]: need["code"] for need in proof["needs"]}})))
                db.execute("UPDATE jobs SET due=? WHERE id=?", (due, job["id"]))
        if final:
            self.store.resolve("page:" + str(job["id"]))
        else:
            self.store.gap("page:" + str(job["id"]), proof["needs"][0]["code"], {"event_id": event_id, "attempts": attempts})
        return final

    def release_waiting(self):
        """Before an earlier release takes over this state (SETUP.md, "Returning to an earlier release"): complete
        every waiting event now, without a read. Each goes out once as its bounded retries would have ended: what
        was already read stays, every read still open is an explicit gap. Nothing is left ``enriching`` and no
        ``enrich`` job stays pending, the two forms an earlier release does not read. The released event ids."""
        released, provider = [], self.provider
        self.provider = _Unread()
        try:
            for row in self.store.rows("SELECT id FROM inbox WHERE state='enriching' ORDER BY seq"):
                job = self.store.one("SELECT id FROM jobs WHERE json_extract(spec,'$.kind')='enrich' AND "
                                     "json_extract(spec,'$.event_id')=? ORDER BY id LIMIT 1", (row["id"],))
                self.enrich(job or {"id": None}, {"event_id": row["id"]}, release=True)
                released.append(row["id"])
        finally:
            self.provider = provider
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='done' WHERE json_extract(spec,'$.kind')='enrich' AND state!='done'")
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%'")
        return released

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
            if "recipient" in spec and spec["recipient"] not in self.notifications:
                configured = spec["recipient"] in {self.config["notifications_user_id"]} | (
                    {self.config["expected_writer_id"]} if self.config["writer_notifications"] else set())
                if spec["kind"] != "enrich":
                    # This reader's own feed and discovery wait for it: a reader Settings still name but whose
                    # credential is missing (``companion.build``) resumes them after the restart that restores it.
                    self.store.gap(scope, "notification_reader_unavailable" if configured else "notification_recipient_removed")
                    with self.store.transaction() as db:
                        db.execute("UPDATE jobs SET state='removed' WHERE id=?", (job["id"],))
                    return False
                if not configured:
                    # A notification its removed reader was still proving goes out at this retry, as at release: what
                    # was read stays, each read still open is an explicit gap. Nothing more is read for that reader.
                    provider, self.provider = self.provider, _Unread()
                    try:
                        return self.enrich(job, spec, release="notification_recipient_removed")
                    finally:
                        self.provider = provider
                # Settings still name it: proving what it already took reads through the writer, as ever, and goes on.
            if spec["kind"] in ("notifications", "notification_discovery"):
                # Verification uses this job's retry/backoff and never delays
                # repository feeds or the other verified Notifications reader.
                self.verify_notification_identity(spec["recipient"])
            if spec["kind"] == "notification_discovery":
                notification, recipient = spec["notification"], spec["recipient"]
                repo, target, route, history = public_subject(self.provider, notification)
                history_at = now()  # when the thread was read: every event built from it, now or later, says so
                context = self.capabilities()
                # This reader's notification is evidence of its update. Under the change capability what it proves
                # goes out as one event per GitHub message or action (emit_outside); its own observation links to
                # them and to its previous update as evidence, never as a revision of the words it points to.
                key = self.account + ":notification:" + str(recipient) + ":" + str(notification["id"])
                lower = spec.get("previous_updated_at") or self.store.meta("bootstrap:notifications:" + str(recipient))
                known = self.store.one("SELECT revision FROM snapshots WHERE key=?", (key,))
                previous = "ghobs:" + digest(key) + ":" + str(known["revision"]) if known else None
                proof = self.prove(notification, recipient, route, history, lower, previous) if context["change"] else None
                discovery = dict(notification=notification, recipient=recipient, repo=repo, target=target,
                                 route=route, history=history, change_context=context["change"])
                waiting = bool(proof and proof["needs"])
                with self.store.transaction() as db:
                    items = self.emit_outside(db, proof, {**discovery, "history_at": history_at}) if proof else []
                    eid = self.store.observe(db, key, {"comparison": notification},
                                             lambda eid, rev, old: discovery_event(eid, rev, old, config=self.config, proof=proof,
                                                                                   items=items, previous=previous,
                                                                                   history_at=history_at, **discovery),
                                             state="enriching" if waiting else None)
                    if eid and waiting:
                        self.defer(db, eid, "discovery", {**discovery, "lower": lower, "previous": previous,
                                                          "history_at": history_at, "records": proof["records"], "items": items},
                                   {"needs": proof["needs"], "old": None, "thread": self.thread_of(route)},
                                   "notifications:" + str(recipient) + ":enrich", {"kind": "enrich", "recipient": recipient})
                    self.supersede(db, route, history_at, eid)
                    db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
                self.store.resolve(scope)
                self.store.resolve("notification:" + str(recipient) + ":" + str(notification["id"]))
                return True
            if spec.get("repo") and spec["repo"]["id"] not in {r["id"] for r in self.config["repositories"]}:
                self.store.gap(scope, "repository_removed_from_selection")
                with self.store.transaction() as db:
                    db.execute("UPDATE jobs SET state='removed' WHERE id=?", (job["id"],))
                return False
            if spec["kind"] == "enrich":
                return self.enrich(job, spec)
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
                            child = {"kind": "notification_discovery", "recipient": spec["recipient"], "notification": item}
                            if prior and prior.get("updated_at"):
                                child["previous_updated_at"] = prior["updated_at"]
                            children.append(child)
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
                    pending = self.pending.pop(key, None) if kwargs is not None else None
                    eid = self.store.observe(db, key, snapshot, lambda eid, rev, old, kw=kwargs:
                                             make_event(eid, rev, old, **kw) if kw is not None else None, baseline,
                                             state="enriching" if pending else None, changed=key in self.merged)
                    held = eid and pending and db.execute("SELECT state FROM inbox WHERE id=?", (eid,)).fetchone()
                    if held and held["state"] == "enriching":
                        self.defer(db, eid, "content", kwargs, pending, "repo:" + str(spec["repo"]["id"]) + ":enrich",
                                   {"kind": "enrich", "repo": spec["repo"]})
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
