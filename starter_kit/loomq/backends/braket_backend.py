"""AWS Braket adapter, built on the SDK's ``Circuit`` object.

Braket is where bit order usually goes wrong: it returns one character per
*qubit*, ordered by ``result.measured_qubits``, while the contract wants one
character per *clbit* with ``c[0]`` rightmost.  Rather than guessing a reversal,
this adapter reads the qubit order Braket reports and applies the circuit's own
qubit-to-clbit map — the mapping is derived, never assumed.

The circuit is built gate by gate instead of by handing Braket OpenQASM text:
Braket's dialect spells the controlled-NOT ``cnot`` and the controlled phase
``cphaseshift``, whereas ``transpile(qasm, "braket")`` must emit the
``stdgates.inc`` spellings the contract asks for.  Going through the object API
keeps both correct without maintaining two QASM printers.
"""

from typing import Any, Callable, Dict, List, Tuple

from ..errors import BackendError
from ..ir import Circuit, GateOp, MeasureOp
from ..result import new_job_id
from ..sim import measurement_width
from .base import Backend, ExecutionOutcome, import_optional


def _builders() -> Dict[str, Callable[[Any, GateOp], None]]:
    """LoomQ gate name -> a call on a Braket ``Circuit``."""
    return {
        "h": lambda circuit, op: circuit.h(op.qubits[0]),
        "x": lambda circuit, op: circuit.x(op.qubits[0]),
        "s": lambda circuit, op: circuit.s(op.qubits[0]),
        "sdg": lambda circuit, op: circuit.si(op.qubits[0]),
        "t": lambda circuit, op: circuit.t(op.qubits[0]),
        "tdg": lambda circuit, op: circuit.ti(op.qubits[0]),
        "rz": lambda circuit, op: circuit.rz(op.qubits[0], op.params[0]),
        "ry": lambda circuit, op: circuit.ry(op.qubits[0], op.params[0]),
        "cx": lambda circuit, op: circuit.cnot(op.qubits[0], op.qubits[1]),
        "cu1": lambda circuit, op: circuit.cphaseshift(op.qubits[0], op.qubits[1], op.params[0]),
        "swap": lambda circuit, op: circuit.swap(op.qubits[0], op.qubits[1]),
        "ccx": lambda circuit, op: circuit.ccnot(op.qubits[0], op.qubits[1], op.qubits[2]),
    }


class BraketBackend(Backend):
    platform = "braket"
    backend_id = "braket_local_simulator"
    executor = "amazon-braket-sdk (LocalSimulator)"

    def availability(self) -> Tuple[bool, str]:
        circuits = import_optional("braket.circuits")
        devices = import_optional("braket.devices")
        if circuits is None or devices is None:
            return False, "amazon-braket-sdk is not installed (pip install amazon-braket-sdk)"
        if not hasattr(circuits, "Circuit") or not hasattr(devices, "LocalSimulator"):
            return False, "amazon-braket-sdk is installed but incomplete"
        return True, "amazon-braket-sdk"

    def _build(self, circuit: Circuit) -> Any:
        circuits = import_optional("braket.circuits")
        builders = _builders()
        native = circuits.Circuit()
        # Pin every declared qubit into the circuit so measured_qubits covers
        # the full register even when a qubit carries no gate.
        for qubit in range(circuit.num_qubits):
            native.i(qubit)
        for op in circuit.ops:
            if isinstance(op, GateOp):
                builder = builders.get(op.name)
                if builder is None:
                    raise BackendError("Braket adapter has no builder for gate %r" % op.name)
                builder(native, op)
            elif isinstance(op, MeasureOp):
                continue  # Braket measures every qubit when shots > 0
        return native

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        devices = import_optional("braket.devices")
        if devices is None:  # pragma: no cover - guarded by availability()
            raise BackendError("amazon-braket-sdk is not installed")

        try:
            device = devices.LocalSimulator()
            task = device.run(self._build(circuit), shots=shots)
            result = task.result()
            raw = dict(result.measurement_counts)
            qubit_order = [int(index) for index in result.measured_qubits]
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("Braket LocalSimulator failed to execute the circuit: %s" % exc)

        counts = self._to_clbit_counts(circuit, raw, qubit_order)

        job_id = new_job_id("braket-local")
        metadata = getattr(result, "task_metadata", None)
        if metadata is not None and getattr(metadata, "id", None):
            job_id = str(metadata.id)

        return ExecutionOutcome(
            counts,
            job_id=job_id,
            meta={
                "executor": self.executor,
                "key_convention": "clbit_msb_first",
                "braket_measured_qubits": qubit_order,
            },
        )

    @staticmethod
    def _to_clbit_counts(
        circuit: Circuit, raw: Dict[str, int], qubit_order: List[int]
    ) -> Dict[str, int]:
        """Re-key Braket's per-qubit strings onto the circuit's clbits."""
        position = {qubit: index for index, qubit in enumerate(qubit_order)}
        pairs = [(op.qubit, op.clbit) for op in circuit.ops if isinstance(op, MeasureOp)]
        if not pairs:
            pairs = [(qubit, qubit) for qubit in range(circuit.num_qubits)]
        width = measurement_width(circuit)

        counts = {}  # type: Dict[str, int]
        for key, count in raw.items():
            value = 0
            for qubit, clbit in pairs:
                index = position.get(qubit)
                if index is None or index >= len(key):
                    continue
                if key[index] == "1":
                    value |= 1 << clbit
            text = "".join("1" if (value >> bit) & 1 else "0" for bit in range(width - 1, -1, -1))
            counts[text] = counts.get(text, 0) + int(count)
        return counts


__all__ = ["BraketBackend"]
