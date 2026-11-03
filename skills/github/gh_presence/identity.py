"""Credential identities from the provider's existing verification records."""
from datetime import datetime, timezone


def connector_identity(config, metadata):
    writer = metadata.get("auth:writer", {})
    if (writer.get("state") != "valid" or writer.get("id") != config["expected_writer_id"]
            or writer.get("auth_mode", "pat") != config["auth_mode"] or not writer.get("login")):
        return {}
    login = writer["login"]
    kind = "user"
    if config["auth_mode"] == "app":
        if writer.get("app_id") != config["app_id"] or writer.get("installation_id") != config["installation_id"]:
            return {}
        if writer.get("expires_at") and datetime.fromisoformat(writer["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            return {}
        kind = "bot"
    result = {"sends_as": {"id": str(writer["id"]), "label": login, "kind": kind}}
    reader = metadata.get("auth:notifications", {})
    if (reader.get("state") == "valid" and reader.get("id") == config["notifications_user_id"]
            and reader.get("id") != writer["id"] and reader.get("login")):
        result["reads_as"] = [{"id": str(reader["id"]), "label": reader["login"], "kind": "user"}]
    return result
