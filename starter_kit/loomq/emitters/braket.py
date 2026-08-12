"""AWS Braket target: complete OpenQASM 3.

Two spellings differ from OpenQASM 2 and both matter:

* ``cu1(theta)`` is ``cp(theta)`` in ``stdgates.inc`` — same unitary, different
  name.  Nothing is decomposed here; the gate survives intact.
* measurement is an assignment, ``c[0] = measure q[0];``, not an arrow.

Registers become ``qubit[n]`` / ``bit[n]`` declarations.  Per-bit measurement
assignment is used rather than the whole-register form so the qubit-to-clbit
mapping stays explicit even when the source measured a whole register.
"""

from typing import Dict, List

from ..errors import TranspileError
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, ResetOp
from ..passes import WHITELIST_BASIS, lower_to_basis
from .base import format_params

BRAKET_BASIS = WHITELIST_BASIS

#: LoomQ gate name -> ``stdgates.inc`` gate name.
_NAMES = {
    "h": "h",
    "x": "x",
    "s": "s",
    "sdg": "sdg",
    "t": "t",
    "tdg": "tdg",
    "rz": "rz",
    "ry": "ry",
    "cx": "cx",
    "cu1": "cp",
    "swap": "swap",
    "ccx": "ccx",
}  # type: Dict[str, str]


def _statement(circuit: Circuit, op) -> str:
    if isinstance(op, GateOp):
        name = _NAMES.get(op.name)
        if name is None:  # pragma: no cover - lower_to_basis guarantees coverage
            raise TranspileError("gate %r has no stdgates.inc spelling" % op.name)
        operands = ", ".join(circuit.qubit_label(index) for index in op.qubits)
        return "%s%s %s;" % (name, format_params(op.params), operands)
    if isinstance(op, MeasureOp):
        return "%s = measure %s;" % (
            circuit.clbit_label(op.clbit),
            circuit.qubit_label(op.qubit),
        )
    if isinstance(op, ResetOp):
        return "reset %s;" % circuit.qubit_label(op.qubit)
    if isinstance(op, BarrierOp):
        return "barrier %s;" % ", ".join(circuit.qubit_label(index) for index in op.qubits)
    raise AssertionError("unhandled operation %r" % (op,))


def emit_braket(circuit: Circuit) -> str:
    """Render ``circuit`` as OpenQASM 3."""
    lowered = lower_to_basis(circuit, BRAKET_BASIS)

    lines = ["OPENQASM 3.0;", 'include "stdgates.inc";']  # type: List[str]
    for register in lowered.qregs:
        lines.append("qubit[%d] %s;" % (register.size, register.name))
    for register in lowered.cregs:
        lines.append("bit[%d] %s;" % (register.size, register.name))
    if not lowered.cregs:
        lines.append("bit[%d] c;" % lowered.num_qubits)

    for op in lowered.ops:
        if isinstance(op, ConditionalOp):
            register = lowered.cregs[0].name if lowered.cregs else "c"
            for candidate in lowered.cregs:
                if list(candidate.indices()) == list(op.clbits):
                    register = candidate.name
                    break
            lines.append(
                "if (%s == %d) { %s }" % (register, op.value, _statement(lowered, op.body))
            )
        else:
            lines.append(_statement(lowered, op))

    if not lowered.has_measurements():
        register = lowered.cregs[0].name if lowered.cregs else "c"
        for qubit in range(lowered.num_qubits):
            lines.append("%s[%d] = measure %s;" % (register, qubit, lowered.qubit_label(qubit)))

    return "\n".join(lines) + "\n"


__all__ = ["BRAKET_BASIS", "emit_braket"]
