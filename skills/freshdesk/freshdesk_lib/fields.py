"""Ticket field catalog: the helpdesk's own names for Freshdesk's numbers.

``GET /ticket_fields`` is open to an agent who may view ticket fields or create tickets; it carries the
choices of status (custom statuses included), priority, source, type, group, agent and product, and of every
custom field. Tools accept a label or an id and answer with both. A label is used only when exactly one choice
matches it, ignoring case; otherwise the caller gets the choices back and nothing is written.
"""
from __future__ import annotations

from .client import FreshdeskError

# Our argument name -> (field type in /ticket_fields, key in a ticket's API body, value kind).
STANDARD = {
    "status": ("default_status", "status", "id"),
    "priority": ("default_priority", "priority", "id"),
    "source": ("default_source", "source", "id"),
    "type": ("default_ticket_type", "type", "text"),
    "group": ("default_group", "group_id", "id"),
    "agent": ("default_agent", "responder_id", "id"),
    "product": ("default_product", "product_id", "id"),
}
BODY_KEYS = {body_key: name for name, (_, body_key, _) in STANDARD.items()}
CLOSING_STATUSES = (4, 5)  # Resolved, Closed: the statuses before which required_for_closure fields are checked


def _choices(field: dict) -> list[dict]:
    """Choices as ``[{id, label}]`` for numbered fields and ``[{value}]`` for text ones; nested stay raw."""
    raw = field.get("choices")
    if field.get("type") == "default_status" and isinstance(raw, dict):
        out = []
        for key, labels in raw.items():
            label = labels[0] if isinstance(labels, list) and labels else str(labels)
            customer = labels[1] if isinstance(labels, list) and len(labels) > 1 else None
            try:
                out.append({"id": int(key), "label": str(label), "customer_label": customer})
            except (TypeError, ValueError):
                continue
        return sorted(out, key=lambda row: row["id"])
    if isinstance(raw, dict):
        out = []
        for label, value in raw.items():
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
                out.append({"id": int(value), "label": str(label)})
        return sorted(out, key=lambda row: row["id"])
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return [{"value": item} for item in raw]
    return []


class FieldCatalog:
    def __init__(self, fields: list):
        self.fields = [field for field in fields if isinstance(field, dict)]
        self.by_type = {field.get("type"): field for field in self.fields if str(field.get("type", "")).startswith("default_")}
        self.custom = {field.get("name"): field for field in self.fields
                       if not field.get("default") and isinstance(field.get("name"), str)}

    @classmethod
    def load(cls, client) -> "FieldCatalog":
        response = client.get("ticket_fields")
        return cls(response.data if isinstance(response.data, list) else [])

    # -- reading ------------------------------------------------------------------------------------
    def label(self, name: str, value):
        """The label of a standard field's value, or ``None`` when the catalog does not know it."""
        spec = STANDARD.get(name)
        field = self.by_type.get(spec[0]) if spec else None
        if field is None or value is None:
            return None
        for choice in _choices(field):
            if choice.get("id") == value or choice.get("value") == value:
                return choice.get("label", choice.get("value"))
        return None

    def named(self, value, name: str) -> dict:
        """``{"id": 3, "label": "Pending"}``; the label is ``None`` when unknown."""
        return {"id": value, "label": self.label(name, value)}

    def describe(self) -> dict:
        standard = {}
        for name, (field_type, body_key, kind) in STANDARD.items():
            field = self.by_type.get(field_type)
            if field is None:
                continue
            standard[name] = {"api_key": body_key, "required_for_closure": bool(field.get("required_for_closure")),
                              "choices": _choices(field)}
        custom = []
        for name, field in sorted(self.custom.items()):
            row = {"name": name, "label": field.get("label"), "type": field.get("type"),
                   "required_for_closure": bool(field.get("required_for_closure"))}
            choices = _choices(field)
            if choices:
                row["choices"] = choices
            elif field.get("choices"):
                row["choices_raw"] = field.get("choices")
            custom.append(row)
        return {"standard": standard, "custom": custom}

    # -- writing ------------------------------------------------------------------------------------
    def resolve(self, name: str, value):
        """The ``(body key, API value)`` for a standard field given by label or id."""
        spec = STANDARD.get(name)
        if spec is None:
            raise FreshdeskError("field_unknown", f"{name!r} is not a ticket property this tool sets.")
        field_type, body_key, kind = spec
        field = self.by_type.get(field_type)
        choices = _choices(field) if field else []
        if value is None or value == "":
            return body_key, None
        if kind == "text":
            if not choices:
                return body_key, str(value)
            wanted = str(value).strip().casefold()
            found = [row["value"] for row in choices if row["value"].casefold() == wanted]
            if len(found) == 1:
                return body_key, found[0]
            raise self._choice_error(name, value, choices)
        if isinstance(value, bool):
            raise self._choice_error(name, value, choices)
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            number = int(value)
            if not choices or any(row["id"] == number for row in choices):
                return body_key, number
            raise self._choice_error(name, value, choices)
        wanted = str(value).strip().casefold()
        found = [row["id"] for row in choices if str(row["label"]).casefold() == wanted]
        if len(found) == 1:
            return body_key, found[0]
        raise self._choice_error(name, value, choices)

    def resolve_custom(self, values: dict) -> dict:
        """Custom fields by their API name (``cf_…``) or label; dropdown values matched like labels."""
        out = {}
        for key, value in (values or {}).items():
            field = self.custom.get(key) or next(
                (f for f in self.custom.values() if str(f.get("label", "")).casefold() == str(key).casefold()), None)
            if field is None:
                raise FreshdeskError("field_unknown", f"No custom ticket field is named {key!r}.",
                                     errors=[{"field": key, "known": sorted(self.custom)}])
            choices = _choices(field)
            if choices and "value" in choices[0] and value not in (None, ""):
                wanted = str(value).strip().casefold()
                found = [row["value"] for row in choices if row["value"].casefold() == wanted]
                if len(found) != 1:
                    raise self._choice_error(field["name"], value, choices)
                value = found[0]
            out[field["name"]] = value
        return out

    def closure_gaps(self, ticket: dict, changes: dict) -> list[dict]:
        """Fields Freshdesk requires before Resolved/Closed that would still be empty after ``changes``."""
        gaps = []
        custom_after = {**(ticket.get("custom_fields") or {}), **(changes.get("custom_fields") or {})}
        for name, (field_type, body_key, _) in STANDARD.items():
            field = self.by_type.get(field_type)
            if field and field.get("required_for_closure"):
                after = changes.get(body_key, ticket.get(body_key))
                if after in (None, "", []):
                    gaps.append({"field": name, "label": field.get("label"), "choices": _choices(field)})
        for name, field in self.custom.items():
            if field.get("required_for_closure") and custom_after.get(name) in (None, "", []):
                gaps.append({"field": name, "label": field.get("label"), "choices": _choices(field)})
        return gaps

    @staticmethod
    def _choice_error(name, value, choices) -> FreshdeskError:
        return FreshdeskError("choice_unknown", f"{value!r} is not exactly one choice of {name!r}; nothing was written.",
                              errors=[{"field": name, "given": value, "choices": choices}])
