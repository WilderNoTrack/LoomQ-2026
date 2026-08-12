"""What every LoomQ backend has to provide.

A backend is deliberately thin: it receives a circuit that has *already* been
lowered onto the twelve-gate basis and its native IR text, and returns raw
counts plus whatever provenance the platform gave back.  Bit-order fixing and
schema assembly happen once, in :mod:`loomq.execution`, so a new vendor can
never invent its own result shape.
"""

from typing import Any, Dict, Optional, Tuple

from ..ir import Circuit

#: Canonical backend ids, matching starter_kit/backend_capabilities.json.
SIMULATOR_IDS = {
    "spinq": "spinq_taurus_simulator",
    "originq": "originq_local_simulator",
    "braket": "braket_local_simulator",
}

QPU_IDS = {
    "spinq": "spinq_cloud_qpu",
    "originq": "originq_wukong",
    "braket": "braket_cloud",
}


class ExecutionOutcome(object):
    """Raw counts plus provenance, before normalisation."""

    __slots__ = ("counts", "job_id", "meta")

    def __init__(
        self,
        counts: Dict[str, int],
        job_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.counts = counts
        self.job_id = job_id
        self.meta = dict(meta or {})


class Backend(object):
    """Base class for a platform adapter."""

    #: ``spinq`` | ``originq`` | ``braket``
    platform = ""
    #: Canonical id reported in ``result["backend"]``.
    backend_id = ""
    #: Human-readable executor description for ``meta.executor``.
    executor = ""
    #: True when this backend talks to physical hardware.
    is_hardware = False

    def availability(self) -> Tuple[bool, str]:
        """``(usable, reason)`` — reason explains *why* when unusable."""
        raise NotImplementedError

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        """Run ``circuit`` and return raw counts."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<%s %s>" % (type(self).__name__, self.backend_id)


def import_optional(module_name: str):
    """Import a vendor SDK, returning ``None`` instead of raising.

    Vendor SDKs are optional by design: LoomQ's core is dependency-free so the
    submission still runs if a wheel is missing from the evaluation image.
    """
    try:
        return __import__(module_name, fromlist=["*"])
    except Exception:  # noqa: BLE001 - a broken SDK must not break LoomQ
        return None


__all__ = [
    "Backend",
    "ExecutionOutcome",
    "QPU_IDS",
    "SIMULATOR_IDS",
    "import_optional",
]
