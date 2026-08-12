"""SpinQ target: complete, executable OpenQASM 2.0.

``target_ir_contract.md`` asks for "complete, executable OpenQASM 2.0" using the
twelve-gate whitelist, with register declarations and measurement statements.
Measurements are written one bit at a time even when the source used the
``measure q -> c;`` shorthand: the expanded form carries the qubit-to-clbit
mapping explicitly, so no reader has to infer the bit order.
"""

from typing import List

from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, ResetOp
from ..passes import WHITELIST_BASIS, lower_to_basis
from .base import format_params

#: SpinQ consumes qelib1 directly, so the whitelist is the basis verbatim.
SPINQ_BASIS = WHITELIST_BASIS


def qasm2_statement(circuit: Circuit, op) -> str:
    """Render one operation as an OpenQASM 2.0 statement (L3 reuses this)."""
    if isinstance(op, GateOp):
        operands = ", ".join(circuit.qubit_label(index) for index in op.qubits)
        return "%s%s %s;" % (op.name, format_params(op.params, symbolic_pi=True), operands)
    if isinstance(op, MeasureOp):
        return "measure %s -> %s;" % (
            circuit.qubit_label(op.qubit),
            circuit.clbit_label(op.clbit),
        )
    if isinstance(op, ResetOp):
        return "reset %s;" % circuit.qubit_label(op.qubit)
    if isinstance(op, BarrierOp):
        return "barrier %s;" % ", ".join(circuit.qubit_label(index) for index in op.qubits)
    raise AssertionError("unhandled operation %r" % (op,))


def emit_spinq(circuit: Circuit) -> str:
    """Render ``circuit`` as OpenQASM 2.0."""
    lowered = lower_to_basis(circuit, SPINQ_BASIS)

    lines = ["OPENQASM 2.0;", 'include "qelib1.inc";']  # type: List[str]
    for register in lowered.qregs:
        lines.append("qreg %s[%d];" % (register.name, register.size))
    for register in lowered.cregs:
        lines.append("creg %s[%d];" % (register.name, register.size))
    if not lowered.cregs:
        # A circuit with no classical register still has to produce counts.
        lines.append("creg c[%d];" % lowered.num_qubits)

    for op in lowered.ops:
        if isinstance(op, ConditionalOp):
            register = lowered.cregs[0].name if lowered.cregs else "c"
            for candidate in lowered.cregs:
                if list(candidate.indices()) == list(op.clbits):
                    register = candidate.name
                    break
            lines.append("if (%s==%d) %s" % (register, op.value, qasm2_statement(lowered, op.body)))
        else:
            lines.append(qasm2_statement(lowered, op))

    if not lowered.has_measurements():
        register = lowered.cregs[0].name if lowered.cregs else "c"
        for qubit in range(lowered.num_qubits):
            lines.append("measure %s -> %s[%d];" % (lowered.qubit_label(qubit), register, qubit))

    return "\n".join(lines) + "\n"


__all__ = ["SPINQ_BASIS", "emit_spinq", "qasm2_statement"]
