"""Names of the people an event names, observed under this bridge's credential; never an identity link."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from .store import BridgeStore

# A person's name observed for an event (users.info, or a users.list scan) answers later events this long;
# after that the next event that names the person reads it again.
NAME_MAX_AGE_SEC = 86400


def directory_key(kind: str, token: str) -> str:
    """A rotated credential or different installation never inherits another's directory observations."""
    return kind + ":" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def person_name(user: Any) -> str | None:
    """The name Slack shows for a person: the profile's display name, else the real name; absent stays None."""
    if not isinstance(user, dict):
        return None
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    for value in (profile.get("display_name"), profile.get("real_name"), user.get("real_name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _name_key(token: str, user_id: str) -> str:
    return directory_key("user_name", token) + ":" + user_id


def known_names(store: BridgeStore, token: str, user_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Each person's last observed name under this credential: ``{name, source, observed_at}`` with epoch seconds,
    from an earlier users.info read for an event, else from the users.list scan ``slack_resolve`` keeps. A name the
    scan alone holds gets its own row, so a later event needs no scan. The caller judges the age."""
    rows = store.directory_snapshots([_name_key(token, user_id) for user_id in user_ids])
    found = {row["user_id"]: {"name": row.get("name"), "source": str(row.get("source") or "users.info"),
                              "observed_at": float(row["observed_at"])} for row in rows.values()}
    missing = [user_id for user_id in user_ids if user_id not in found]
    scan = store.directory_snapshot(directory_key("user", token)) if missing else None
    if scan:
        entries = {str(entry.get("id")): entry for entry in scan.get("entries", []) if isinstance(entry, dict)}
        scanned = {user_id: {"name": person_name(entries[user_id]), "source": "users.list",
                             "observed_at": float(scan["observed_at"])} for user_id in missing if user_id in entries}
        remember_names(store, token, scanned)
        found.update(scanned)
    return found


def remember_names(store: BridgeStore, token: str, observed: Mapping[str, Mapping[str, Any]]) -> None:
    """Keep each person's dated name under this credential, replaced in place by a newer observation."""
    for user_id, entry in observed.items():
        store.save_directory_snapshot(_name_key(token, user_id), {
            "observed_at": float(entry["observed_at"]), "user_id": user_id, "name": entry.get("name"),
            "source": entry.get("source") or "users.info"})
