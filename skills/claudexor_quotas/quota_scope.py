"""Exact applicability scope of one quota window — standard library only.

The engine's window (QuotaConstraint) says which requests it governs with four
independent fields: ``applies_to_models`` (null/absent, ``[]`` or a list),
``applies_to_model_prefixes`` (family prefixes matched literally),
``applies_to_unspecified_model`` (whether a request that names no model is
governed) and ``applies_to_request_kinds`` (absent = text and image).

This module keeps what each field said, *including whether it was said at
all*: an absent field, an explicit ``false`` and an empty list are three
different facts for inspection, and none of them is rewritten into another.
Three things are derived from it, each with one owner here:

* ``models_hash`` — the scope id a cooldown or a reported model exhaustion is
  tied to a window by. With no prefixes it is byte-identical to the id the
  history has always stored (so no recorded series changes identity).
* ``key_token`` — the last field of a limit's group key: the same id, plus a
  short digest only when the window is also scoped by the unspecified-model
  flag being true or by request kinds. A window with none of the new fields
  keeps exactly its old key and its old history.
* ``applies`` — whether the window governs a request ``(model, kind)``, by the
  engine's own projection rule (``quotaSnapshotAvailability``). ``model=None``
  is a request that names no model.

What this module refuses to do: guess a scope from a label, treat a window
whose scope fields it cannot read as vendor-wide, or invent a model list.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

MAX_SHOWN = 24
MAX_TEXT = 80
REQUEST_KINDS = ("text", "image")


def names_of(raw: Any) -> Tuple[str, ...]:
    """Every name of one scope list, sorted, each in full: what the scope's
    identity is taken from. Display bounds are applied afterwards."""
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()}))


def list_state(raw: Any) -> str:
    """``absent`` (key missing or null), ``empty`` (``[]``), ``listed`` (a
    list naming something), ``unreadable`` (anything else, or a list of
    nothing readable)."""
    if raw is None:
        return "absent"
    if not isinstance(raw, list):
        return "unreadable"
    if not raw:
        return "empty"
    return "listed" if names_of(raw) else "unreadable"


def models_hash(models: Sequence[str], prefixes: Sequence[str] = ()) -> str:
    """A scope's identity: the whole sorted model list, every name in full,
    and — only when there are any — the sorted family prefixes.

    Serialized so that two different lists never give the same bytes. Joined
    by newlines it is, as long as no name holds a newline — and every scope
    recorded so far is such a list, so it keeps the key its history is
    stored under. A list with a newline inside a name is serialized as JSON
    behind a leading newline instead (a joined list never starts with one:
    names are stripped and never empty). Prefixes extend it behind a unit
    separator no stripped name can contain at its start."""
    if not models and not prefixes:
        return "-"
    if any("\n" in name for name in models):
        joined = "\n" + json.dumps(list(models), ensure_ascii=False, separators=(",", ":"))
    else:
        joined = "\n".join(models)
    if prefixes:
        joined += "\x1fprefixes\x1f" + json.dumps(list(prefixes), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class Scope:
    """What one window said about the requests it governs."""

    models: Tuple[str, ...] = ()
    models_state: str = "absent"
    prefixes: Tuple[str, ...] = ()
    prefixes_state: str = "absent"
    # None = the field was absent; True/False exactly as sent.
    unspecified: Optional[bool] = None
    # None = absent (text and image); a tuple exactly as sent otherwise.
    kinds: Optional[Tuple[str, ...]] = None
    kinds_state: str = "absent"

    @property
    def scoped(self) -> bool:
        """Tied to some models: the engine's projection rule (an empty list
        and no prefixes is vendor-wide, as is an absent one)."""
        return bool(self.models or self.prefixes)

    @property
    def token(self) -> str:
        return models_hash(self.models, self.prefixes)

    def key_token(self) -> str:
        """The scope field of the group key. Legacy-identical unless the
        window also carries ``applies_to_unspecified_model: true`` or
        request kinds; those split the group, as the engine's own identity
        splits them. ``false`` and absent are one identity there too."""
        extra: Dict[str, Any] = {}
        if self.unspecified is True:
            extra["u"] = True
        if self.kinds is not None:
            extra["k"] = list(self.kinds)
        if not extra:
            return self.token
        digest = hashlib.sha256(json.dumps(extra, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:6]
        return f"{self.token}~{digest}"

    def describe(self) -> Dict[str, Any]:
        """The scope as the widget and the tool show it: every field with
        what was said (``*_state``), names bounded for display only."""
        def shown(names: Tuple[str, ...]) -> Dict[str, Any]:
            return {"names": [name[:MAX_TEXT] for name in names[:MAX_SHOWN]],
                    "omitted": max(0, len(names) - MAX_SHOWN)}
        return {
            "models": shown(self.models), "models_state": self.models_state,
            "prefixes": shown(self.prefixes), "prefixes_state": self.prefixes_state,
            "unspecified_model": self.unspecified,
            "request_kinds": None if self.kinds is None else list(self.kinds),
            "request_kinds_state": self.kinds_state,
            "scoped": self.scoped,
        }


def scope_of(constraint: Any) -> Scope:
    """The scope one constraint carries, field by field, as sent."""
    if not isinstance(constraint, dict):
        return Scope()
    raw_models = constraint.get("applies_to_models")
    raw_prefixes = constraint.get("applies_to_model_prefixes")
    raw_flag = constraint.get("applies_to_unspecified_model")
    raw_kinds = constraint.get("applies_to_request_kinds")
    kinds_state = list_state(raw_kinds)
    kinds: Optional[Tuple[str, ...]] = None
    if kinds_state != "absent":
        listed = raw_kinds if isinstance(raw_kinds, list) else []
        kinds = tuple(sorted({k for k in listed if isinstance(k, str) and k in REQUEST_KINDS}))
        if kinds_state == "listed" and len(kinds) != len({str(k) for k in listed}):
            kinds_state = "unreadable"  # a kind the engine's schema forbids: never guessed at
    return Scope(
        models=names_of(raw_models), models_state=list_state(raw_models),
        prefixes=names_of(raw_prefixes), prefixes_state=list_state(raw_prefixes),
        unspecified=raw_flag if isinstance(raw_flag, bool) else None,
        kinds=kinds, kinds_state=kinds_state,
    )


def applies(scope: Scope, model: Optional[str], kind: str = "text") -> Optional[bool]:
    """Whether a window governs a request, by the engine's projection rule.

    None when the answer cannot be given from what the window said: its
    request-kind field was unreadable (a value the engine's schema forbids) —
    an unreadable scope is never read as vendor-wide."""
    if scope.kinds_state == "unreadable" or scope.models_state == "unreadable" \
            or scope.prefixes_state == "unreadable":
        return None
    if scope.kinds is not None and kind not in scope.kinds:
        return False
    if not scope.scoped:
        return True
    if model is None:
        return scope.unspecified is True
    # Match the existing quota-block projection, NOT BudgetLedger's stricter
    # admission predicate: aliases are case-insensitive containment in either
    # direction; declared family prefixes remain literal and case-sensitive.
    # Source: schema/quota-availability.ts modelScopeMatches + its scoped arm.
    target = model.strip().lower()
    return any(model.startswith(p) for p in scope.prefixes) or any(
        target and (alias.lower() in target or target in alias.lower()) for alias in scope.models)
