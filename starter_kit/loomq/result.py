"""The one result schema every backend is normalised into.

Every platform reports differently — decimal keys, reversed bit order, nested
task metadata.  Normalising *here* rather than in each backend is what lets a
user compare a SpinQ run against a Braket run without knowing either vendor's
conventions.

Bit order is the part that silently breaks cross-platform comparisons, so it is
fixed by contract: a counts key is ``c[n-1]...c[1]c[0]`` — **the rightmost
character is c[0]** — and ``bit_order`` always reads ``"little"``.
"""

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from .version import CONTRACT_VERSION

_BINARY = re.compile(r"^[01]+$")


def utc_timestamp() -> str:
    """ISO-8601 in UTC with a trailing ``Z``, as the schema example shows."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_job_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:16])


def build_result(
    backend: str,
    job_id: str,
    shots: int,
    counts: Mapping[str, int],
    meta: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble a contract-compliant result dictionary."""
    payload = {
        "backend": backend,
        "job_id": job_id,
        "shots": int(shots),
        "counts": {str(key): int(value) for key, value in counts.items()},
        "bit_order": "little",
        "timestamp": timestamp or utc_timestamp(),
        "meta": dict(meta or {}),
    }
    payload["meta"].setdefault("contract_version", CONTRACT_VERSION)
    return payload


def validate_result(result: Any) -> Tuple[bool, str]:
    """Re-implementation of the public checker, used as an internal guard.

    ``run()`` self-validates before returning, so a schema regression shows up
    in LoomQ's own test suite instead of on the scoreboard.
    """
    if not isinstance(result, dict):
        return False, "result must be an object"
    required = ("backend", "job_id", "shots", "counts", "bit_order", "timestamp")
    missing = [field for field in required if field not in result]
    if missing:
        return False, "missing fields: " + ", ".join(missing)
    shots = result["shots"]
    if not isinstance(shots, int) or isinstance(shots, bool) or shots <= 0:
        return False, "shots must be a positive integer"
    counts = result["counts"]
    if not isinstance(counts, dict) or not counts:
        return False, "counts must be a non-empty object"
    widths = set()
    for key, value in counts.items():
        if not isinstance(key, str) or not _BINARY.match(key):
            return False, "counts keys must be non-empty binary strings"
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False, "counts values must be non-negative integers"
        widths.add(len(key))
    if len(widths) != 1:
        return False, "counts keys must all have the same width"
    if sum(counts.values()) != shots:
        return False, "counts total must equal shots exactly"
    if result["bit_order"] != "little":
        return False, "bit_order must be little"
    if result.get("meta", {}).get("is_mock"):
        return False, "mock results never pass evaluation"
    return True, "schema valid"


def normalize_counts(
    raw: Mapping[Any, int], width: int, reverse: bool = False
) -> Dict[str, int]:
    """Coerce a vendor's counts into ``c[n-1]...c[0]`` binary-string keys.

    ``reverse=True`` flips a platform that reports ``c[0]`` leftmost.  Decimal
    keys (pyqpanda's older shape) are widened to ``width`` bits.
    """
    counts = {}  # type: Dict[str, int]
    for key, value in raw.items():
        if isinstance(key, int):
            text = bin(key)[2:].zfill(width)
        else:
            text = str(key).strip()
            if _BINARY.match(text):
                text = text.zfill(width)
            elif text.isdigit():
                text = bin(int(text))[2:].zfill(width)
            else:
                raise ValueError("cannot interpret counts key %r" % (key,))
        if reverse:
            text = text[::-1]
        counts[text] = counts.get(text, 0) + int(value)
    return counts


def counts_to_distribution(counts: Mapping[str, int]) -> Dict[str, float]:
    total = float(sum(counts.values()))
    if total <= 0:
        return {}
    return {key: value / total for key, value in counts.items()}


def hellinger_fidelity(
    observed: Mapping[str, float], expected: Mapping[str, float]
) -> float:
    """``1 - H(P, Q)`` with the competition's exact Hellinger definition."""
    states = set(observed) | set(expected)
    distance = math.sqrt(
        sum(
            (math.sqrt(observed.get(state, 0.0)) - math.sqrt(expected.get(state, 0.0))) ** 2
            for state in states
        )
    ) / math.sqrt(2.0)
    return max(0.0, min(1.0, 1.0 - distance))


def top_states(counts: Mapping[str, int], limit: int = 4) -> Tuple[str, ...]:
    """The ``limit`` most frequent outcomes — the hardware "main peak" check."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(key for key, _ in ordered[:limit])


__all__ = [
    "build_result",
    "counts_to_distribution",
    "hellinger_fidelity",
    "new_job_id",
    "normalize_counts",
    "top_states",
    "utc_timestamp",
    "validate_result",
]
