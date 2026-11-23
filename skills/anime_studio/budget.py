"""Self-contained budget rail for Anime Studio jobs.

Why this exists: the Ouroboros core cannot see this skill's provider spend —
the skill calls OpenRouter directly with a granted key, so core accounting
records its usage as unknown/unmetered. This rail is therefore the ONLY budget
protection a job has. Every number in this module is an ESTIMATE derived by
decomposition from the single measured medium-mode data point ($7.39/scene,
one 2-scene run costing $14.77 — see modes.py); nothing here is a billed
amount, and drift between these estimates and real provider bills is expected.
"""
from __future__ import annotations

from .modes import mode_config, normalize_mode

# Estimated per-unit USD costs. Self-consistency check — the medium-mode
# composition must land near the measured $7.39/scene. For the measured
# 2-scene medium run (2 characters, 1 location, 1 music cue, 2 video
# candidates + 1 judge per scene, adjacent continuity):
#
#   job overhead: storyboard 0.30
#               + 2 x (character_sheet 0.25 + vlm_image_verify 0.05) = 0.60
#               + 1 x (location_art 0.20 + vlm_image_verify 0.05)    = 0.25
#               + 1 x music_cue 0.40                                  -> 1.55
#   per scene:   keyframe 0.85 + vlm_image_verify 0.05
#              + 2 x video_clip 2.65        = 5.30
#              + 2 x vlm_video_verify 0.12  = 0.24
#              + 1 judge x 2 order-swap calls x vlm_judge 0.10 = 0.20
#              + 1 x vlm_cross_scene 0.08                             -> 6.72
#   total: 1.55 + 2 x 6.72 = 14.99, ~1.5% over the measured 14.77.
#   The order-swap pair is counted at full price on purpose: the estimate
#   should not be cheaper than the ledger it is compared against.
UNIT_COST_USD = {
    "storyboard": 0.30,
    "character_sheet": 0.25,
    "location_art": 0.20,
    "keyframe": 0.85,
    "video_clip": 2.65,
    "music_cue": 0.40,
    "vlm_image_verify": 0.05,
    "vlm_video_verify": 0.12,
    "vlm_judge": 0.10,
    "vlm_cross_scene": 0.08,
}

# Default headroom applied when the caller does not set an explicit limit:
# estimates are decomposed from one data point, so real jobs routinely run a
# little over; 30% absorbs that without letting a job run away.
DEFAULT_LIMIT_HEADROOM = 1.30

_ENTRIES_MAX = 200

# Assumptions used only for estimation (matching the measured-run shape).
_EST_CHARACTERS = 2
_EST_LOCATIONS = 1


def _cross_scene_checks(continuity: str, num_scenes: int) -> int:
    """Estimated cross-scene VLM checks for a continuity policy."""
    adjacent = max(0, num_scenes - 1)
    if continuity == "off":
        return 0
    if continuity == "all_recheck":
        # Adjacent chain plus a full end-of-job recheck pass.
        return adjacent * 2
    return adjacent  # "adjacent"


def estimate_job_usd(num_scenes: int, mode: str, *, include_music: bool = True) -> dict:
    """Decomposed pre-flight cost estimate for a job. ESTIMATE, never a bill."""
    num_scenes = max(1, int(num_scenes))
    cfg = mode_config(mode)
    candidates = int(cfg["video_candidates"])
    judges = int(cfg["judges"])

    counts = {
        "storyboard": 1,
        "character_sheet": _EST_CHARACTERS,
        "location_art": _EST_LOCATIONS,
        "keyframe": num_scenes,
        "video_clip": num_scenes * candidates,
        "music_cue": 1 if include_music else 0,
        # Sheets + locations + one verify per keyframe.
        "vlm_image_verify": _EST_CHARACTERS + _EST_LOCATIONS + num_scenes,
        "vlm_video_verify": num_scenes * candidates,
        # x2: the panel's order-swap mitigation issues TWO provider calls per
        # judge per scene (forward + reversed), and the pipeline charges both.
        # Estimating one hid half the judge spend from the pre-flight quote.
        "vlm_judge": num_scenes * judges * 2,
        "vlm_cross_scene": _cross_scene_checks(str(cfg["continuity"]), num_scenes),
    }
    breakdown = {
        kind: round(UNIT_COST_USD[kind] * count, 4)
        for kind, count in counts.items()
        if count > 0
    }
    total = round(sum(breakdown.values()), 2)
    return {
        "estimate_usd": total,
        "per_scene_usd": round(total / num_scenes, 2),
        "basis": cfg["est_basis"],
        "breakdown": breakdown,
    }


def derive_limit_usd(estimate_usd: float, requested_limit_usd: float) -> float:
    """An explicit positive limit wins; otherwise estimate + default headroom."""
    if requested_limit_usd and requested_limit_usd > 0:
        return round(float(requested_limit_usd), 2)
    return round(float(estimate_usd) * DEFAULT_LIMIT_HEADROOM, 2)


def refusal_payload(estimate: dict, limit_usd: float) -> dict:
    """Structured pre-flight refusal: nothing was generated, nothing was spent."""
    required = float(estimate.get("estimate_usd", 0.0))
    return {
        "error": (
            f"Estimated job cost ${required:.2f} exceeds the budget limit "
            f"${float(limit_usd):.2f}; a limit of at least ${required:.2f} is "
            f"required. Nothing was generated and nothing was spent."
        ),
        "reason": "budget_estimate_exceeds_limit",
        "estimate_usd": round(required, 2),
        "limit_usd": round(float(limit_usd), 2),
        "required_usd": round(required, 2),
        "basis": estimate.get("basis", ""),
        "hint": (
            "Raise budget_limit_usd, lower num_scenes, or pick a cheaper "
            "quality_mode (low/medium/max)."
        ),
    }


class BudgetLedger:
    """In-job estimated-spend ledger. Charges are unit ESTIMATES, not bills."""

    def __init__(self, limit_usd: float, estimate_usd: float, mode: str):
        self.mode = normalize_mode(mode)
        self.basis = mode_config(self.mode)["est_basis"]
        self.limit_usd = round(float(limit_usd), 4)
        self.estimate_usd = round(float(estimate_usd), 4)
        self.spent_usd = 0.0
        self.entries: list[dict] = []
        self.hard_stop_reason = ""
        self.stopped = False
        self._next_receipt_id = 1
        self._receipts: dict[int, dict] = {}

    def charge(self, kind: str, count: int = 1, *, note: str = "") -> dict:
        """Record estimated spend and RETURN a receipt.

        The receipt is what makes a refund safe: a refund keyed by kind/count
        could credit back a different dispatch's charge, or fire twice. Unknown
        kinds charge 0.0 and are flagged in the note instead of raising — a
        bookkeeping gap must never kill a half-finished generation job.
        """
        unit = UNIT_COST_USD.get(kind)
        if unit is None:
            unit = 0.0
            note = f"unknown_kind {note}".strip()
        charged = round(unit * count, 4)
        self.spent_usd = round(self.spent_usd + charged, 4)
        receipt_id = self._next_receipt_id
        self._next_receipt_id += 1
        receipt = {
            "id": receipt_id,
            "kind": kind,
            "count": count,
            "usd": charged,
            "refunded": False,
        }
        self._receipts[receipt_id] = receipt
        self.entries.append(
            {
                "kind": kind,
                "count": count,
                "usd": charged,
                "note": note,
                "receipt_id": receipt_id,
            }
        )
        if len(self.entries) > _ENTRIES_MAX:
            # Bounded ledger: keep the most recent entries; totals stay exact.
            del self.entries[: len(self.entries) - _ENTRIES_MAX]
        if len(self._receipts) > _ENTRIES_MAX:
            for stale in sorted(self._receipts)[: len(self._receipts) - _ENTRIES_MAX]:
                del self._receipts[stale]
        return dict(receipt)

    def refund(self, receipt: dict | None, *, note: str = "") -> bool:
        """Credit back EXACTLY one recorded charge. Idempotent by receipt id.

        Used only when nothing was generated: the provider refused the request
        before starting a job (HTTP 400/422) or the request was never sent. A
        dropped connection, 429 or 5xx is deliberately not refunded: the job may
        have been accepted and billed, and crediting it back would under-count
        real spend — the mirror image of the phantom charges this fixes.
        """
        if not receipt:
            return False
        try:
            live = self._receipts.get(int(receipt.get("id", 0)))
        except (TypeError, ValueError, AttributeError):
            return False
        if live is None or live["refunded"]:
            return False
        live["refunded"] = True
        self.spent_usd = round(self.spent_usd - live["usd"], 4)
        self.entries.append(
            {
                "kind": live["kind"],
                "count": -live["count"],
                "usd": -live["usd"],
                "note": f"refund {note}".strip(),
                "receipt_id": live["id"],
            }
        )
        if len(self.entries) > _ENTRIES_MAX:
            del self.entries[: len(self.entries) - _ENTRIES_MAX]
        return True

    @property
    def remaining_usd(self) -> float:
        return round(max(0.0, self.limit_usd - self.spent_usd), 4)

    @property
    def exceeded(self) -> bool:
        if self.limit_usd > 0:
            return self.spent_usd >= self.limit_usd
        return False

    def would_exceed(self, kind: str, count: int = 1) -> bool:
        if self.limit_usd <= 0:
            return False
        unit = UNIT_COST_USD.get(kind, 0.0)
        # STRICT ">": refusal_payload promises the owner that a limit EQUAL to
        # the estimate is sufficient, so a charge landing exactly ON the limit
        # must be admitted. `exceeded` (>=) then flips and stops the NEXT one.
        return round(self.spent_usd + unit * count, 4) > self.limit_usd

    def hard_stop(self, reason: str) -> None:
        self.hard_stop_reason = str(reason)
        self.stopped = True

    def to_dict(self) -> dict:
        return {
            "limit_usd": round(self.limit_usd, 2),
            "estimate_usd": round(self.estimate_usd, 2),
            "spent_usd": round(self.spent_usd, 2),
            "remaining_usd": round(self.remaining_usd, 2),
            "mode": self.mode,
            "basis": self.basis,
            "exceeded": self.exceeded,
            "hard_stop_reason": self.hard_stop_reason,
            "entries": list(self.entries),
        }
