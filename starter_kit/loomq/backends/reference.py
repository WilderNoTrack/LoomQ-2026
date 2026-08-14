"""LoomQ's built-in executor.

Always available, never mocked: the counts come from an exact simulation of the
circuit the user actually submitted.  It plays three roles — default executor
when no vendor SDK is installed, oracle for cross-checking vendor results, and
the verifier the L2 agent runs its own generated QASM through.

Two engines sit behind it, chosen by the circuit rather than by configuration:

*Statevector* is exact and gives closed-form probabilities, so shots can be
allocated without sampling noise.  It costs ``2^n`` amplitudes and stops at 26
qubits.

*Stabilizer* handles Clifford circuits in ``O(n^2)``.  It only samples — there
is no closed-form distribution to read off — so it is used exactly where the
statevector cannot go: a Clifford circuit too wide to hold in memory.  A
60-qubit GHZ state runs here in milliseconds, and "make me a GHZ state" is the
most common thing anyone asks the agent.
"""

from typing import Dict, Optional, Tuple

from ..errors import LoomQError
from ..ir import Circuit
from ..sim import ideal_distribution, measurement_width
from ..sim.sampler import resolve_mode, resolve_seed, sample_counts
from ..sim.stabilizer import is_clifford
from ..sim.stabilizer import sample_counts as stabilizer_counts
from ..sim.statevector import MAX_QUBITS
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
        width = measurement_width(circuit)

        if circuit.num_qubits > MAX_QUBITS:
            if not is_clifford(circuit):
                raise LoomQError(
                    "this circuit needs %d qubits, which exceeds the %d-qubit "
                    "statevector limit, and it is not Clifford so the stabilizer "
                    "engine cannot take it either"
                    % (circuit.num_qubits, MAX_QUBITS)
                )
            seed = resolve_seed()
            counts = stabilizer_counts(circuit, shots, width, seed=seed)
            return ExecutionOutcome(
                counts,
                job_id=new_job_id("loomq-ref"),
                meta={
                    "executor": self.executor,
                    "engine": "stabilizer",
                    "sampling": "multinomial",
                    "seed": seed,
                    "noise_model": "none",
                    "note": "Clifford circuit beyond the statevector limit; "
                            "simulated with an Aaronson-Gottesman tableau",
                },
            )

        distribution = ideal_distribution(circuit, width)
        counts = sample_counts(distribution, shots)
        return ExecutionOutcome(
            counts,
            job_id=new_job_id("loomq-ref"),
            meta={
                "executor": self.executor,
                "engine": "statevector",
                "sampling": resolve_mode(),
                "seed": resolve_seed(),
                "noise_model": "none",
            },
        )


__all__ = ["ReferenceBackend"]
