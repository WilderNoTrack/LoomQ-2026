"""LoomQ's built-in executor, backed by the reference statevector simulator.

Always available, never mocked: the counts come from an exact simulation of the
circuit the user actually submitted.  It plays three roles — default executor
when no vendor SDK is installed, oracle for cross-checking vendor results, and
the verifier the L2 agent runs its own generated QASM through.
"""

from typing import Tuple

from ..ir import Circuit
from ..sim import ideal_distribution, measurement_width
from ..sim.sampler import resolve_mode, resolve_seed, sample_counts
from ..result import new_job_id
from .base import Backend, ExecutionOutcome


class ReferenceBackend(Backend):
    executor = "loomq-reference-simulator"

    def __init__(self, platform: str, backend_id: str) -> None:
        self.platform = platform
        self.backend_id = backend_id

    def availability(self) -> Tuple[bool, str]:
        return True, "built in"

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        distribution = ideal_distribution(circuit, measurement_width(circuit))
        counts = sample_counts(distribution, shots)
        return ExecutionOutcome(
            counts,
            job_id=new_job_id("loomq-ref"),
            meta={
                "executor": self.executor,
                "sampling": resolve_mode(),
                "seed": resolve_seed(),
                "noise_model": "none",
            },
        )


__all__ = ["ReferenceBackend"]
