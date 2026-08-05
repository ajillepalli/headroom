"""Pure transformations from captured snapshots to bounded readings."""

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Dict, Optional

from .freshness import freshness_seconds
from .resets import reset_time_is_plausible, window_minutes_from_raw


class Confidence(str, Enum):
    """How precisely a reading describes current usage."""

    FRESH = "fresh"
    STALE_BOUNDED = "stale_bounded"
    POST_RESET = "post_reset"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Snapshot:
    """A usage value captured from one tool at one point in time."""

    used_percentage: float
    captured_at: float
    resets_at: Optional[float]
    window: str
    source: str
    limit_reached: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""

        return {
            "used_percentage": self.used_percentage,
            "captured_at": self.captured_at,
            "resets_at": self.resets_at,
            "window": self.window,
            "source": self.source,
            "limit_reached": self.limit_reached,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Snapshot":
        """Build a snapshot from persisted state."""

        resets_at = value.get("resets_at")
        raw = value.get("raw")
        return cls(
            used_percentage=float(value["used_percentage"]),
            captured_at=float(value["captured_at"]),
            resets_at=float(resets_at) if resets_at is not None else None,
            window=str(value["window"]),
            source=str(value["source"]),
            limit_reached=bool(value.get("limit_reached", False)),
            raw=dict(raw) if isinstance(raw, dict) else {},
        )


@dataclass(frozen=True)
class Reading:
    """A current, sound interpretation of a possibly stale snapshot."""

    certain: bool
    lower_bound_percent: Optional[float]
    resets_at: Optional[float]
    age_seconds: float
    window: str
    source: str
    confidence: Confidence
    limit_reached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""

        return {
            "certain": self.certain,
            "lower_bound_percent": self.lower_bound_percent,
            "resets_at": self.resets_at,
            "age_seconds": self.age_seconds,
            "window": self.window,
            "source": self.source,
            "confidence": self.confidence.value,
            "limit_reached": self.limit_reached,
        }


def bound_snapshot(
    snapshot: Optional[Snapshot],
    now: float,
    source: str,
    window: str,
    fresh_for_seconds: Optional[float] = None,
) -> Reading:
    """Apply reset and monotonicity rules to one snapshot."""

    if snapshot is None:
        return Reading(
            certain=False,
            lower_bound_percent=None,
            resets_at=None,
            age_seconds=0.0,
            window=window,
            source=source,
            confidence=Confidence.UNKNOWN,
        )

    age = max(0.0, float(now) - snapshot.captured_at)
    resets_at = snapshot.resets_at
    if resets_at is not None and not reset_time_is_plausible(
        resets_at,
        snapshot.captured_at,
        window_minutes_from_raw(snapshot.raw),
    ):
        resets_at = None
    if resets_at is not None and now > resets_at:
        return Reading(
            certain=True,
            lower_bound_percent=0.0,
            resets_at=resets_at,
            age_seconds=age,
            window=snapshot.window,
            source=snapshot.source,
            confidence=Confidence.POST_RESET,
            limit_reached=False,
        )

    configured_freshness = (
        freshness_seconds(source)
        if fresh_for_seconds is None
        else float(fresh_for_seconds)
    )
    fresh_for = max(0.0, configured_freshness)
    confidence = (
        Confidence.FRESH if age <= fresh_for else Confidence.STALE_BOUNDED
    )
    return Reading(
        certain=confidence is Confidence.FRESH,
        lower_bound_percent=snapshot.used_percentage,
        resets_at=resets_at,
        age_seconds=age,
        window=snapshot.window,
        source=snapshot.source,
        confidence=confidence,
        limit_reached=snapshot.limit_reached,
    )


def valid_percent(value: Any) -> Optional[float]:
    """Return a finite, non-negative percentage or None."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0:
        return None
    return number
