"""SMTP observations, separate from recipient delivery or read claims."""


def tool_origin(ctx):
    origin = {"kind": "tool"}
    if getattr(ctx, "task_id", None):
        origin["task_id"] = str(ctx.task_id)
    event = ((getattr(ctx, "task_metadata", None) or {}).get("presence") or {}).get("event") or {}
    if event.get("source_event_id"):
        origin["source_event_id"] = str(event["source_event_id"])
    return origin


def email_report(item, state, *, recipients=None, error="", refused=None):
    info = item.reporting
    if info.get("version") != 1:
        return None
    wire = info.get("wire") or {}
    references = list(item.references)
    if item.in_reply_to and item.in_reply_to not in references:
        references.append(item.in_reply_to)
    targets = wire.get("recipients", item.envelope_recipients) if recipients is None else recipients
    message = {
        "message_id": item.message_id, "subject": wire.get("subject", item.subject),
        "recipients": list(targets),
        "in_reply_to": item.in_reply_to, "references": references,
        "smtp_accepted": state == "accepted" and (info.get("provider_receipt") or {}).get("transport", "smtp") == "smtp",
    }
    if info.get("provider_receipt"):
        message["provider_receipt"] = info["provider_receipt"]
    if info.get("sender"):
        message["from"] = info["sender"]
    if "to" in wire:
        message["to"] = wire["to"]
    if "cc" in wire:
        message["cc"] = wire["cc"]
    if refused:
        message["refused_recipients"] = {
            address: {"code": value[0], "message": value[1].decode("utf-8", errors="replace") if isinstance(value[1], bytes) else str(value[1])}
            for address, value in refused.items()
        }
    if error:
        message["error"] = error
    thread = item.references[0] if item.references else (item.in_reply_to or item.message_id)
    return {
        "schema_version": 1, "delivery_id": item.request_id,
        "part_id": "0" if state == "accepted" else "status", "state": state,
        "provider": "email", "account_id": str(info.get("account_id") or ""),
        "conversation_id": thread, "thread_id": thread,
        "text": wire.get("text", item.body) if state == "accepted" else "", "format": "plain",
        "message": message, "origin": info.get("origin") or {"kind": "automatic"},
    }
