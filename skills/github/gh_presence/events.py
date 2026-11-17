"""Observed content revisions and evidence-based facts; no importance decisions."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .config import LOGIN_PATTERN
from .store import digest, dump, now

PARSER = "github-syntactic-mention/2"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SIGNAL_NEW_DISCUSSION = "signal.new_discussion"
# The thread snapshot a focus renders whole: its opening and the newest messages
# within this many characters. Every provider row stays in the connector's inbox.
THREAD_CHAR_BOUND = 30000
ROOT_KINDS = ("issue", "pr", "discussion")
CONTENT_KINDS = ROOT_KINDS + ("issue_comment", "inline_comment", "review", "discussion_comment", "pr_details")
NOT_FETCHED = ("baseline_history_not_fetched", "self_echo_retained_without_history_fetch")


def unknown_actor():
    return {"platform": "github", "attribution": "unknown"}


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


def provider_time(value):
    """A GitHub timestamp as an ISO time with its zone; "" when absent or unreadable."""
    moment = instant(value)
    return moment.isoformat() if moment else ""


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.utcoffset() is not None else None


def times(kind, raw, edited):
    """``message.sent_at``, when the object was written, and when its words were edited ``edited_at``, GitHub's last
    change (for an issue or pull request any change, a new comment included); never an observation time. A state
    change is no edit, and a review request has no time of its own (it borrows the pull request's)."""
    if kind == "review_request":
        return {}
    changed = provider_time(raw.get("updated_at") or raw.get("updatedAt")) if edited else ""
    return {key: value for key, value in (("sent_at", provider_time(created(raw))), ("edited_at", changed)) if value}


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


def merged_at(raw):
    """When a pull request was merged. Its Issues-feed row says so only in the nested ``pull_request.merged_at`` (no
    top-level ``merged``), which ``comparison`` leaves out: fingerprints stay the ones every release stored."""
    pull = raw.get("pull_request") if isinstance(raw, dict) else None
    return (raw.get("merged_at") if isinstance(raw, dict) else None) or (pull.get("merged_at") if isinstance(pull, dict) else None)


def merged_since(old, raw):
    """A pull request merged since its retained snapshot while its row's compared fields stayed the same: closed
    without a merge then, reopened and merged between two polls, ``state`` closed both times."""
    earlier = (old or {}).get("raw")
    return bool(isinstance(earlier, dict) and "pull_request" in raw and merged_at(raw) and not merged_at(earlier)
                and (old.get("comparison") or {}).get("state") == comparison(raw).get("state"))


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
        intent = json.loads(row["intent"])
        if route_identity(intent["route"]) != route_identity(route) or str(intent["writer_id"]) != actor["platform_actor_id"]:
            continue
        if raw.get("body") != row["body"]:
            continue  # manual edit of our published comment is a new observed revision
        receipt = json.loads(row["receipt"]) if row["receipt"] else {}
        if (receipt.get("provider_id") is not None and str(receipt["provider_id"]) == str(raw.get("databaseId", raw.get("id")))) or row["marker"] in raw.get("body", ""):
            return True
    return False


def words(raw):
    """The words an object carries: its title above its body, exactly as GitHub returns them."""
    return (raw.get("title", "") + "\n\n" if raw.get("title") else "") + (raw.get("body") or "")


def node_id(raw):
    """GitHub's global node id (REST ``node_id``, GraphQL ``id``): unique across issues, comments and reviews."""
    value = raw.get("node_id") if isinstance(raw.get("node_id"), str) else raw.get("id")
    return value if isinstance(value, str) and value else None


def login_of(user):
    """A REST-style login: GraphQL names a Bot by its bare slug, REST by its ``[bot]`` account."""
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        return None
    return login + "[bot]" if user.get("__typename") == "Bot" and not login.endswith("[bot]") else login


def mention_identities(text, watched, writer_id):
    """``message.mentions`` in the host's identity shape: each watched account the text mentions, once."""
    canonical = {str(u["id"]): u["login"] for u in watched}
    found = {}
    for span in mentions(text, watched):
        ident = str(span["user_id"])
        found.setdefault(ident, {"id": ident, "name": canonical.get(ident) or span["login"],
                                 "is_self": writer_id is not None and ident == str(writer_id)})
    return list(found.values())


def _row_kind(row):
    if "issue_url" in row:
        return "issue_comment"
    if "pull_request_review_id" in row or "diff_hunk" in row:
        return "review_comment"
    if "submitted_at" in row:
        return "review"
    if "number" in row:
        return ("pull_request" if "pull_request" in row else "issue") if "user" in row else "discussion"
    return "discussion_comment" if "replyTo" in row else "comment"


def thread_history(history, history_gap, writer_id, waking=None, observed=None):
    """``conversation.thread_history`` (the host's documented thread snapshot): normalized messages with their
    author's numeric id, login, provider time and exact words, an author directory and an honest status. The
    raw provider rows stay in ``conversation.history`` inside the connector's inbox. ``observed`` is when those
    rows were read; an event completed later never restamps them."""
    observed = observed or now()
    rows = [row for row in history if isinstance(row, dict) and ("body" in row or "number" in row)]
    if not rows:
        return {"status": "unavailable", "observed_at": observed, "error": history_gap} if history_gap else None
    addresses = {str(row.get("id")): node_id(row) or str(row.get("id")) for row in rows if row.get("id") is not None}
    messages = []
    for row in rows:
        kind = _row_kind(row)
        message = {"message_id": node_id(row) or str(row.get("id") or ""), "kind": kind,
                   "text": words(row) if kind in ("issue", "pull_request", "discussion") else (row.get("body") or "")}
        sent = provider_time(created(row))
        if sent:
            message["sent_at"] = sent
        if row.get("_actor_id"):
            message["platform_actor_id"] = str(row["_actor_id"])
        login = login_of(row.get("user") or row.get("author"))
        if login:
            message["username"] = login
        if kind == "review" and row.get("state"):
            message["review_state"] = row["state"]
        parent = row.get("in_reply_to_id") or (row.get("replyTo") or {}).get("id")
        if parent:
            message["reply_to"] = addresses.get(str(parent), str(parent))
        messages.append(message)
    opening = messages[0] if messages[0]["kind"] in ("issue", "pull_request", "discussion") else None
    rest = messages[1:] if opening else messages
    # GitHub reads a pull request's general comments, inline comments and reviews as separate lists: order them by
    # provider time (read order among equal times, an unknown time last) before the newest words are kept.
    times = [instant(m.get("sent_at")) for m in rest]
    rest = [rest[i] for i in sorted(range(len(rest)), key=lambda i: (times[i] is None, times[i] or _EPOCH, i))]
    kept, used = [], len(opening["text"]) if opening else 0
    for message in reversed(rest):
        if kept and used + len(message["text"]) > THREAD_CHAR_BOUND:
            break
        kept.append(message)
        used += len(message["text"])
    kept.reverse()
    present = {m["message_id"] for m in kept} | ({opening["message_id"]} if opening else set())
    if waking and waking not in present:
        found = [index for index, message in enumerate(rest) if message["message_id"] == waking]
        if found:  # the waking message is always shown, in its place
            kept = sorted(kept + [rest[found[0]]], key=lambda m: next(i for i, r in enumerate(rest) if r is m))
    selected = ([opening] if opening else []) + kept
    complete = len(selected) == len(messages)
    authors, seen = [], set()
    for message in selected:
        ident = message.get("platform_actor_id")
        if ident and ident not in seen:
            seen.add(ident)
            authors.append({"id": ident, "name": message.get("username"),
                            "is_self": writer_id is not None and ident == str(writer_id)})
    value = {"status": "available" if complete and not history_gap else "partial", "observed_at": observed,
             "authors": authors, "data": {"messages": selected, "complete": complete}}
    if history_gap:
        value["gap"] = history_gap
    if not complete:
        value["note"] = ("Older messages are left out here; github_place_status with this event's "
                         "source_event_id returns every retained provider row.")
    return value


def _people(field, rows):
    if field == "teams":
        return ", ".join("team " + str(r.get("slug") or r.get("name") or "?") + " (id " + str(r.get("id")) + ")" for r in rows)
    return ", ".join("@" + (login_of(r) or "?") + " (id " + str(r.get("id")) + ")" for r in rows)


_KNOWN_FIELDS = {"title", "body", "state", "state_reason", "draft", "locked", "closed", "assignees", "users", "teams",
                 "merged", "merged_at", "id", "node_id", "databaseId", "author_id"}


def state_changes(kind, old, raw):
    """Plain factual sentences for what changed between two retained snapshots (``message.non_text``)."""
    old_raw = (old or {}).get("raw") or {}
    prev, cur = (old or {}).get("comparison") or {}, comparison(raw)
    noun = {"issue": "the issue", "pr": "the pull request", "pr_details": "the pull request",
            "discussion": "the discussion", "review": "the review"}.get(kind, "the comment")
    lines = []

    def changed(field):
        return prev.get(field) != cur.get(field)
    if kind != "review_request":
        if changed("title"):
            lines.append("Changed the title of " + noun + " from " + json.dumps(old_raw.get("title") or "", ensure_ascii=False)
                         + " to " + json.dumps(raw.get("title") or "", ensure_ascii=False) + ".")
        if changed("body"):
            lines.append("Edited the text of " + noun + ".")
        if changed("state"):
            if kind == "review":
                lines.append("The review state changed from " + str(prev.get("state")) + " to " + str(cur.get("state")) + ".")
            elif cur.get("state") == "closed":
                merged = (raw.get("pull_request") or {}).get("merged_at") or raw.get("merged_at")
                lines.append(("Merged and closed " + noun if merged else "Closed " + noun)
                             + (" as " + str(raw["state_reason"]) if raw.get("state_reason") and not merged else "") + ".")
            else:
                lines.append("Reopened " + noun + ".")
        elif changed("state_reason"):
            lines.append("The close reason of " + noun + " changed from " + str(prev.get("state_reason"))
                         + " to " + str(cur.get("state_reason")) + ".")
        if merged_since(old, raw):
            lines.append("Merged " + noun + ".")
        if changed("draft"):
            lines.append("Converted " + noun + " to a draft." if cur.get("draft") else "Marked " + noun + " ready for review.")
        if changed("locked"):
            lines.append(("Locked" if cur.get("locked") else "Unlocked") + " the conversation.")
        if changed("closed"):
            lines.append(("Closed " if cur.get("closed") else "Reopened ") + noun + ".")
    # A requested reviewer leaves the list when the request is removed and also when they submit their review:
    # the observation is the list, so its sentence does not name a removal nobody may have made.
    for field, added_words, removed_words in (("assignees", "Assigned ", "Unassigned "),
                                               ("users", "Requested a review from ", "No longer a requested reviewer: "),
                                               ("teams", "Requested a review from ", "No longer a requested reviewer: ")):
        before_ids, after_ids = set(prev.get(field) or []), set(cur.get(field) or [])
        current = {str(x.get("id")): x for x in raw.get(field) or [] if isinstance(x, dict)}
        earlier = {str(x.get("id")): x for x in old_raw.get(field) or [] if isinstance(x, dict)}
        if after_ids - before_ids:
            lines.append(added_words + _people(field, [current.get(i, {"id": i}) for i in sorted(after_ids - before_ids)]) + ".")
        if before_ids - after_ids:
            lines.append(removed_words + _people(field, [earlier.get(i, {"id": i}) for i in sorted(before_ids - after_ids)]) + ".")
    other = sorted(field for field in set(prev) | set(cur) if field not in _KNOWN_FIELDS and changed(field))
    if other and kind != "review_request":
        lines.append("GitHub changed " + ", ".join(other) + " of " + noun + ".")
    return lines


def observed_changes(kind, old, raw):
    """The changes between two snapshots that GitHub's own records can attribute, as (change, value) pairs."""
    prev, cur = (old or {}).get("comparison") or {}, comparison(raw)
    found = []
    if kind != "review_request":
        for field in ("title", "body", "draft", "locked", "closed"):
            if prev.get(field) != cur.get(field):
                found.append((field, cur.get(field)))
        if prev.get("state") != cur.get("state"):
            found.append(("state", cur.get("state")))
        elif prev.get("state_reason") != cur.get("state_reason"):
            found.append(("state_reason", cur.get("state_reason")))
        found += [(field, None) for field in sorted(set(prev) | set(cur))
                  if field not in _KNOWN_FIELDS and prev.get(field) != cur.get(field)]
        if prev.get("merged") != cur.get("merged") or prev.get("merged_at") != cur.get("merged_at"):
            found.append(("merged", cur.get("merged") or cur.get("merged_at")))
        elif merged_since(old, raw):
            found.append(("merged", merged_at(raw)))
    for field in ("assignees", "users", "teams"):
        before_ids, after_ids = set(prev.get(field) or []), set(cur.get(field) or [])
        found += [(field + ":added", ident) for ident in sorted(after_ids - before_ids)]
        found += [(field + ":removed", ident) for ident in sorted(before_ids - after_ids)]
    return found


_KEYED = {("assignees", "added"): ("AssignedEvent", "assignee"), ("assignees", "removed"): ("UnassignedEvent", "assignee"),
          ("users", "added"): ("ReviewRequestedEvent", "reviewer"), ("users", "removed"): ("ReviewRequestRemovedEvent", "reviewer"),
          ("teams", "added"): ("ReviewRequestedEvent", "reviewer"), ("teams", "removed"): ("ReviewRequestRemovedEvent", "reviewer")}


def recorded_change(kind, change):
    """Whether GitHub always keeps an actor record for this observed change, so a record not yet returned is worth
    a reread: content edits of every object; title, state, lock and assignee events of issues and pull requests
    (draft and merge too for a pull request); a review request added. A reviewer leaving the requested list has a
    record only when someone removed the request: one who submits their review leaves it without any."""
    if kind in ("issue", "pr", "pr_details"):
        return (change in ("title", "body", "state", "locked") or change.startswith("assignees:")
                or (kind != "issue" and change in ("draft", "merged")))
    if kind == "review_request":
        return change in ("users:added", "teams:added")
    return change == "body"


def word_editors(record):
    """Every account GitHub's edit record names for an object's words, whenever it edited them: their numeric ids,
    and whether that record is complete with each editor identified. A complete empty record: nobody edited them."""
    ids, complete = set(), not record.get("edits_incomplete")
    for edit in record.get("edits") or []:
        editor = edit.get("editor") if isinstance(edit, dict) else None
        ident = editor.get("databaseId") if isinstance(editor, dict) else None
        if type(ident) is int and ident > 0:
            ids.add(str(ident))
        else:
            complete = False
    return {"ids": sorted(ids), "complete": complete}


def attribute(kind, old, raw, record, resolve, *, lower=None, upper=None):
    """Who made an observed revision, from GitHub's own record of each change (content edits, timeline events).

    Every observed change must match its own record inside the observation interval; one account across all of
    them attributes the revision. A change GitHub records no actor for, several accounts between two polls, or
    an incomplete record leave the actor unknown. Never the latest editor for every changed field.
    """
    found = observed_changes(kind, old, raw)
    if not found:
        return {"status": "unresolved", "code": "change_not_identified", "records": []}
    low, high, slack = instant(lower), instant(upper), timedelta(seconds=2)

    def within(at, strict=True):
        moment = instant(at)
        if moment is None or (high and moment > high + slack):
            return False
        return not low or (moment > low if strict else moment >= low - slack)
    events = record.get("events") or []

    def latest(types, match=lambda event: True, strict=True):
        hits = [e for e in events if e.get("type") in types and match(e) and within(e.get("created_at"), strict)]
        return max(hits, key=lambda e: instant(e["created_at"])) if hits else None
    matched, missing = [], []
    for change, value in found:
        if change == "body":
            edits = [e for e in record.get("edits") or [] if not e.get("deleted") and within(e.get("edited_at"))]
            if edits and record.get("edits_incomplete") and all(within(e.get("edited_at")) for e in record.get("edits") or []):
                missing.append("body_incomplete")
            elif not edits:
                missing.append(change)
            matched += [{"change": change, "source": "UserContentEdit", "at": e["edited_at"], "actor": e.get("editor")} for e in edits]
            continue
        if change == "title":
            # Every rename of the interval made this title, not only the newest: another account's rename before the
            # author's own stays in its words. The newest one sets this title (else GitHub has not returned it yet); a
            # timeline page that does not reach back to the interval's start may hide another.
            renames = [e for e in events if e.get("type") == "RenamedTitleEvent" and within(e.get("created_at"))]
            newest = max((instant(e["created_at"]) for e in renames), default=None)
            if not any(instant(e["created_at"]) == newest and e.get("current_title") == raw.get("title") for e in renames):
                missing.append(change)
                continue
            if record.get("events_incomplete") and not (low and any(
                    instant(e.get("created_at")) and instant(e["created_at"]) <= low for e in events)):
                missing.append("title_incomplete")
            matched += [{"change": change, "source": e["type"], "at": e["created_at"], "actor": e.get("actor")} for e in renames]
            continue
        hits = []
        if change == "state" and value == "closed":
            hits = [latest({"ClosedEvent"})]
            if (raw.get("pull_request") or {}).get("merged_at"):
                hits.append(latest({"MergedEvent"}))
        elif change == "state" and value == "open":
            hits = [latest({"ReopenedEvent"})]
        elif change == "merged":
            hits = [latest({"MergedEvent"})]
        elif change == "draft":
            hits = [latest({"ConvertToDraftEvent"} if value else {"ReadyForReviewEvent"})]
        elif change == "locked":
            hits = [latest({"LockedEvent"} if value else {"UnlockedEvent"})]
        elif ":" in change:
            field, how = change.split(":")
            name, target = _KEYED[(field, how)]
            hits = [latest({name}, lambda e: str((e.get(target) or {}).get("id")) == str(value)
                           and ((e.get(target) or {}).get("typename") == "Team") == (field == "teams"), strict=False)]
        if not hits or any(hit is None for hit in hits):
            missing.append(change)
            continue
        matched += [{"change": change, "source": hit["type"], "at": hit["created_at"], "actor": hit.get("actor")} for hit in hits]
    actors = [resolve(item["actor"]) if item.get("actor") else unknown_actor() for item in matched]
    records = [{"change": item["change"], "source": item["source"], "at": item["at"],
                "actor_id": actor.get("platform_actor_id"), "login": actor.get("username")}
               for item, actor in zip(matched, actors)]
    if missing:
        code = "change_record_incomplete" if {"body_incomplete", "title_incomplete"} & set(missing) else "change_record_missing"
        # A record GitHub keeps for this kind of change may still be on its way; one it never keeps is not.
        return {"status": "unresolved", "code": code, "missing": missing, "records": records,
                "reread": code == "change_record_missing" and all(recorded_change(kind, change) for change in missing)}
    identities = {actor.get("platform_actor_id") or ("unresolved", actor.get("username"), bool(actor.get("is_bot")))
                  for actor in actors}
    if len(identities) > 1:
        return {"status": "ambiguous", "code": "multiple_change_actors", "records": records}
    actor = actors[0]
    if not actor.get("platform_actor_id"):
        # A known Bot keeps its type when its number is unknown; an unknown account is not a bot.
        return {"status": "unresolved", "code": "change_actor_unresolved", "records": records,
                **({"actor": actor} if actor.get("is_bot") else {})}
    text_times = [item["at"] for item in matched if item["change"] in ("body", "title") and instant(item["at"])]
    result = {"status": "attributed", "actor": actor, "records": records, "word_editors": word_editors(record)}
    if text_times:
        result["edited_at"] = provider_time(max(text_times, key=instant))
    return result


def _who(actor):
    if not actor or not (actor.get("platform_actor_id") or actor.get("username")):
        return "an unidentified account"
    return "@" + str(actor.get("username") or "?") + (" (id " + actor["platform_actor_id"] + ")" if actor.get("platform_actor_id") else "")


def same_account(one, other):
    """Both name the same numeric GitHub account; a login or an unknown account proves nothing."""
    ident = (one or {}).get("platform_actor_id")
    return bool(ident) and ident == (other or {}).get("platform_actor_id")


def word_origin(author):
    """``message.forward_origin`` for revised words: the account that first wrote them, or an explicitly unknown one
    (also for words several accounts wrote). The editor is never taken for the author of copied words."""
    author = author or {}
    named = {key: value for key, value in (("id", author.get("platform_actor_id")), ("username", author.get("username")),
                                           ("display_name", author.get("display_name") or author.get("username"))) if value}
    return {"sender_user": named or unknown_actor()}


def foreign_words(old, raw, attribution, author):
    """What an observed revision leaves on its object's words for every later one, kept with its snapshot. ``title``:
    a title change not proven to be its first author's (every rename of the observed interval is read, and a
    discussion has none). ``body``: an edit of the text GitHub's records named another account for, or one its
    record answered without, so that record cannot later prove the text. Never cleared: those words stay mixed.
    An edit whose record was not read at all leaves no mark: the complete edit record read with every later
    revision answers for it (``mixed_words``)."""
    flags = dict((old or {}).get("foreign_words") or {})
    if not old:
        return flags
    prev, cur = old.get("comparison") or {}, comparison(raw)
    ident = (author or {}).get("platform_actor_id")
    attribution = attribution or {}
    records, missing = attribution.get("records") or [], attribution.get("missing") or []
    if attribution.get("title_origin") == "other":
        flags["title"] = True  # renamed by another account, or by nobody its record can name, before it was retained
    if prev.get("title") != cur.get("title"):
        hits = [item for item in records if item.get("change") == "title"]
        if not (ident and hits and not {"title", "title_incomplete"} & set(missing)
                and all(item.get("actor_id") == ident for item in hits)):
            flags["title"] = True
    if prev.get("body") != cur.get("body") and (
            ("body" in missing and attribution.get("code") == "change_record_missing")
            or any(item.get("change") == "body" and item.get("actor_id") and item["actor_id"] != ident for item in records)):
        flags["body"] = True
    return flags


def mixed_words(old, attribution, author, unsettled=False):
    """Why words their first author revised are still not that account's alone, or None when GitHub's records prove
    they are: every change of this interval is that account's (the caller's check), its complete edit record names
    no other editor of the text, and no earlier observed revision left another hand on it or is still being read.
    The author making the latest edit never proves the whole text."""
    editors = (attribution or {}).get("word_editors") or {}
    if (old or {}).get("foreign_words"):
        return "earlier_revision_not_its_authors"
    if unsettled:
        return "earlier_revision_unsettled"
    if (attribution or {}).get("title_origin") == "other":
        return "title_not_its_authors"
    if set(editors.get("ids") or ()) - {(author or {}).get("platform_actor_id")}:
        return "other_editor_recorded"
    return None if editors.get("complete") is True else "edit_record_incomplete"


def attribution_words(attribution, event_actor, author, edited, mixed=None):
    """One sentence on who made a change, kept apart from the change itself."""
    status = (attribution or {}).get("status")
    if status == "attributed":
        if edited and not same_account(event_actor, author):
            return ("Edited by " + _who(event_actor) + "; the text was first written by " + _who(author)
                    + ". Who wrote which part is not recorded separately.")
        if edited and mixed:
            return ("Edited by " + _who(event_actor) + ", who first wrote this text; it is not established as theirs "
                    "alone (" + mixed + "). Who wrote which part is not recorded separately.")
        return "GitHub records " + _who(event_actor) + " as having made this change."
    if status == "ambiguous":
        logins = sorted({"@" + str(r.get("login")) for r in attribution.get("records") or [] if r.get("login")})
        return ("Several accounts made changes in this interval" + (" (" + ", ".join(logins) + ")" if logins else "")
                + "; no single actor is attributed.")
    code = (attribution or {}).get("code") or "change_actor_unresolved"
    return "Who made this change is not established (" + code + ")."


def origin_words(origin, author):
    """One sentence on words first observed after they were edited, kept apart from the words themselves."""
    if origin.get("status") == "foreign":
        return ("First observed after it was edited: GitHub's records name " + ", ".join(origin.get("named") or ["another account"])
                + " besides its author " + _who(author) + ". Who wrote which part is not recorded separately.")
    if origin.get("code") == "title_record_unavailable":
        return ("First observed after something had happened to it; GitHub keeps no record of who set a Discussion's "
                "title, so its words are not established as its author's alone (title_record_unavailable).")
    return ("First observed after it was edited; GitHub's records do not establish whose edits its words carry ("
            + str(origin.get("code")) + ").")


def make_event(event_id, revision, old, *, account, repo, thread, kind, raw, actor, route,
               watched, history, history_gap=None, notification=None, first_observed_update=False,
               change_context=False, attribution=None, root_author=None, writer_id=None, signals=False,
               unsettled=False, observed_at=None, history_at=None, origin=None):
    """``observed_at`` and ``history_at``: when the object and its thread were really read. An event completed later
    (a retried read) keeps them; only a first build stamps the present. ``origin``: why new words first observed
    after they were edited are not proven their author's alone (``Collector.first_origin``), else None."""
    text = original = words(raw)
    observed = observed_at or now()
    history_read = history_at or observed
    action = "edited" if old else "updated_before_first_observation" if first_observed_update else "observed"
    evidence, facts = {}, []
    gaps = []
    # Relations ("in my thread", "a reply to me") belong to new words someone wrote. A revision (an edit, merge, close,
    # reopen, draft flip), a first-scan snapshot of an older object and a review-request list are not new words: they
    # carry only what that revision newly adds (a mention, an assignee, a requested reviewer), so the account's own
    # merges and edits never read as replies in its threads.
    new_words = not old and not first_observed_update and kind not in ("pr_details", "review_request")
    old_text = (old or {}).get("text", "")
    edited = bool(old) and text != old_text  # the words themselves, before any placeholder below
    if not change_context:
        # A host without presence_change_context keeps the 0.1.6 form: any_message, and a revision repeats its words
        # under an unknown actor (a REST original author does not identify the editor/assigner/state actor).
        event_actor = actor if not old and not first_observed_update and kind != "review_request" else unknown_actor()
        facts.append("any_message")
    elif new_words:
        event_actor = actor
        facts.append("any_message")
    else:
        # A change is not a message: its actor is GitHub's own record of who made it, else unknown.
        event_actor = unknown_actor() if first_observed_update else dict((attribution or {}).get("actor") or unknown_actor())
    if event_actor.get("is_bot"):
        facts.append("from_bot")
    if new_words and kind in ("issue", "pr"):
        facts.append("new_issue" if kind == "issue" else "new_pull_request")
    if new_words and kind == "discussion" and signals:
        facts.append(SIGNAL_NEW_DISCUSSION)
    if text and text != old_text and not first_observed_update:
        spans = mentions(text, watched)
        if old:  # an edit mentions only a watched login it newly adds
            earlier = {span["user_id"] for span in mentions(old_text, watched)}
            spans = [span for span in spans if span["user_id"] not in earlier]
        if spans:
            facts.append("mention")
            evidence["mention"] = spans
    ids = {str(x["id"]) for x in watched}
    prior = []
    target_time = (raw.get("updated_at") or raw.get("updatedAt")) if old or first_observed_update else created(raw)
    for entry in history:
        # Numeric user identity is attached by collection, never guessed from a login.
        opened = entry.get("number") == route["number"] and kind in ROOT_KINDS
        if str(entry.get("_actor_id", "")) in ids and (opened or before(created(entry), target_time or "")):
            prior.append({"id": entry.get("id"), "created_at": created(entry), "actor_id": entry["_actor_id"]})
    if (root_author and new_words and kind not in ROOT_KINDS and str(root_author.get("actor_id") or "") in ids
            and not any(str(p.get("id")) == str(root_author.get("id")) for p in prior)):
        # The retained opening proves its watched author's thread even when this history read failed.
        prior.append({"id": root_author.get("id"), "created_at": root_author.get("created_at", ""),
                      "actor_id": str(root_author["actor_id"]), "source": "retained_thread_opening"})
    if prior and new_words:
        facts.append("my_thread")
        evidence["my_thread"] = prior
    if history_gap:
        gaps.append({"scope": "history", "code": history_gap})
    parent_id = raw.get("in_reply_to_id") or (raw.get("replyTo") or {}).get("id")
    parents = [r for r in history if str(r.get("id")) == str(parent_id) and str(r.get("_actor_id", "")) in ids]
    if parent_id and parents and new_words:
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
    non_text, forward = [], None
    if not change_context:
        if not text:
            text = "[GitHub structured observation: " + kind + "]\n" + dump(comparison(raw))
    else:
        if not new_words and not edited:
            text = ""  # a change of state carries no new words: message.non_text describes it
        if first_observed_update:
            non_text.append({"kind": "state_change", "description":
                             "Changed before this connector first observed it (first seen " + observed + "); its earlier "
                             "text, what changed and who changed it are not available."})
        elif not new_words:
            noun = "pull request" if kind in ("pr", "pr_details", "review_request") else kind.replace("_", " ")
            by_author = (attribution or {}).get("status") == "attributed" and same_account(event_actor, actor)
            mixed = mixed_words(old, attribution, actor, unsettled) if edited and by_author else None
            non_text += [{"kind": "state_change", "description": line} for line in state_changes(kind, old, raw)] or [
                {"kind": "state_change", "description": "GitHub changed this " + noun + "."}]
            non_text.append({"kind": "change_attribution",
                             "description": attribution_words(attribution, event_actor, actor, edited, mixed)})
            if (attribution or {}).get("status") != "attributed":
                gaps.append({"scope": "actor", "code": (attribution or {}).get("code") or "change_actor_unresolved"})
            if (attribution or {}).get("records"):
                evidence["change_actor"] = {"status": attribution["status"], "records": attribution["records"]}
            if edited and not by_author:
                # The revised words copy their first author's text, named or unknown: never the editor's own words
                # until GitHub's records prove one account wrote and edited them.
                forward = word_origin(actor)
            elif mixed:
                # Their first author made this edit, but the text is not that account's alone (another account's
                # edit, an unproven earlier revision, an incomplete record): no single origin, never own words.
                forward = word_origin(None)
                evidence["word_origin"] = {"code": mixed, "editors": (attribution.get("word_editors") or {}).get("ids", []),
                                           "earlier": sorted((old or {}).get("foreign_words") or {})}
        if kind == "review" and new_words and raw.get("state"):
            non_text.append({"kind": "review_verdict", "description": "Review state: " + str(raw["state"]) + "."})
    if origin and new_words:
        # Still new words under their author (never a change, on any host), but not that account's own: GitHub's
        # records name another account for the text, or do not establish that nobody else edited it.
        forward = word_origin(None)
        evidence["word_origin"] = {name: origin[name] for name in ("code", "editors") if origin.get(name)}
        if origin.get("status") != "foreign":
            gaps.append({"scope": "revision", "code": origin["code"], **({"error": origin["error"]} if origin.get("error") else {})})
        if change_context:
            non_text.append({"kind": "change_attribution", "description": origin_words(origin, actor)})
    source_url = raw.get("html_url") or raw.get("url")
    thread_url = next((entry.get("html_url") or entry.get("url") for entry in history
                       if entry.get("number") == route["number"] and (entry.get("html_url") or entry.get("url"))), None)
    key = ":".join(("github", account, "repo." + str(repo["id"]), thread or "0"))
    message = {"kind": kind, "action": action, "revision": revision, "url": source_url,
               "previous_source_event_id": event_id.rsplit(":", 1)[0] + ":" + str(revision - 1) if old else None,
               "raw": raw, "original_author": actor, "reply_route": route,
               "fact_evidence": evidence, "coverage_gaps": gaps, "notification": notification,
               "context_fingerprint": context_fingerprint(history), "observed_at": observed}
    if not change_context:
        message.update(times(kind, raw, edited))
    else:
        sent = provider_time(created(raw)) if kind != "review_request" else ""
        message.update({k: v for k, v in (("sent_at", sent),
                                          ("edited_at", (attribution or {}).get("edited_at", "") if edited else "")) if v})
    ident = node_id(raw) if kind in CONTENT_KINDS else None
    if ident:
        message["message_id"] = ident
    if (new_words or edited) and original:
        named = mention_identities(original, watched, writer_id)
        if named:
            message["mentions"] = named
    if non_text:
        message["non_text"] = non_text
    if forward and forward["sender_user"]:
        message["forward_origin"] = forward
    conversation = {"repository_id": repo["id"], "full_name": repo["full_name"],
                    "visibility": repo.get("visibility", "public"), "server": route.get("server", "https://api.github.com"),
                    "url": thread_url,
                    "history": {"observed_at": history_read, "complete": not bool(history_gap), "rows": history}}
    if history or (history_gap and history_gap not in NOT_FETCHED):
        snapshot = thread_history(history, history_gap, writer_id, waking=ident, observed=history_read)
        if snapshot:
            conversation["thread_history"] = snapshot
    return {"source_event_id": event_id, "provider": "github", "account_id": account,
            "conversation_id": "repo." + str(repo["id"]), "thread_id": thread, "conversation_key": key,
            "actor": event_actor, "text": text, "facts": facts, "conversation": conversation, "message": message}
