"""Backend registry: pick an executor for a target platform.

``LOOMQ_EXECUTOR`` selects the policy:

``auto`` (default)
    Use the vendor SDK when it imports cleanly, otherwise LoomQ's reference
    simulator.  Either way the result is cross-checked against the reference
    distribution before it is returned (see :mod:`loomq.execution`).
``sdk``
    Require the vendor SDK; fail loudly if it is missing.  Used by the
    cross-platform regression tests and when producing hardware evidence.
``reference``
    Always use LoomQ's own simulator.  Deterministic, dependency-free, and what
    makes the submission runnable in an image where no wheel installed.
"""

import os
from typing import Dict, List, Optional, Tuple

from ..emitters import normalize_target
from .base import Backend, ExecutionOutcome, QPU_IDS, SIMULATOR_IDS
from .braket_backend import BraketBackend
from .originq_backend import OriginQBackend
from .reference import ReferenceBackend
from .spinq_backend import SpinQBackend

AUTO = "auto"
SDK_ONLY = "sdk"
REFERENCE_ONLY = "reference"

_VENDORS = {
    "spinq": SpinQBackend,
    "originq": OriginQBackend,
    "braket": BraketBackend,
}


def resolve_policy(policy: Optional[str] = None) -> str:
    candidate = (policy or os.environ.get("LOOMQ_EXECUTOR") or AUTO).strip().lower()
    return candidate if candidate in (AUTO, SDK_ONLY, REFERENCE_ONLY) else AUTO


def vendor_backend(target: str) -> Backend:
    """The vendor adapter for ``target`` (regardless of availability)."""
    return _VENDORS[normalize_target(target)]()


def reference_backend(target: str) -> ReferenceBackend:
    platform = normalize_target(target)
    return ReferenceBackend(platform, SIMULATOR_IDS[platform])


def select_backend(target: str, policy: Optional[str] = None) -> Tuple[Backend, str]:
    """``(backend, reason)`` for the chosen execution policy."""
    platform = normalize_target(target)
    mode = resolve_policy(policy)

    if mode == REFERENCE_ONLY:
        return reference_backend(platform), "LOOMQ_EXECUTOR=reference"

    vendor = vendor_backend(platform)
    usable, reason = vendor.availability()
    if usable:
        return vendor, reason
    if mode == SDK_ONLY:
        from ..errors import BackendError

        raise BackendError("LOOMQ_EXECUTOR=sdk but %s is unusable: %s" % (platform, reason))
    return reference_backend(platform), reason


def availability_report() -> Dict[str, Dict[str, object]]:
    """What every platform can do right now — used by ``loomq doctor``."""
    report = {}  # type: Dict[str, Dict[str, object]]
    for platform in ("spinq", "originq", "braket"):
        usable, reason = vendor_backend(platform).availability()
        report[platform] = {
            "simulator_id": SIMULATOR_IDS[platform],
            "qpu_id": QPU_IDS[platform],
            "sdk_available": usable,
            "detail": reason,
        }
    return report


def available_platforms() -> List[str]:
    return [name for name, info in availability_report().items() if info["sdk_available"]]


__all__ = [
    "AUTO",
    "Backend",
    "ExecutionOutcome",
    "REFERENCE_ONLY",
    "SDK_ONLY",
    "availability_report",
    "available_platforms",
    "reference_backend",
    "resolve_policy",
    "select_backend",
    "vendor_backend",
]
