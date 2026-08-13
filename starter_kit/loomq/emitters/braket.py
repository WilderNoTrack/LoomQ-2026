"""AWS Braket target: complete OpenQASM 3.

The gate names here were chosen by experiment, not by reading one table.

OpenQASM 3's ``stdgates.inc`` and Braket's own gate set overlap but disagree on
exactly the gates the whitelist needs: ``cx``/``cnot``, ``ccx``/``ccnot``,
``cp``/``cphaseshift``, ``sdg``/``si``, ``tdg``/``ti``.  Braket's parser resolves
``include`` against the filesystem and ships no copy of ``stdgates.inc``, so
feeding it the standard spellings fails with "Gate cx is not defined" — which
means an artifact written in either dialect is only readable by half the tools
that might read it.

So the emitter lowers onto the *intersection*: ``h, x, s, t, rz, ry, swap`` are
spelled identically in both, and ``cx`` is written ``cnot``, which
``target_ir_contract.md`` names explicitly ("评测器接受 cx 或 cnot") and which is
Braket's own name.  Everything else — ``sdg``, ``tdg``, ``cu1``, ``ccx`` — is
decomposed away by :mod:`loomq.passes` before it reaches this printer.

The result costs a few more gates and buys an artifact with no dialect
ambiguity left in it: verified end to end by ``tools/validate_vendor_ir.py``,
which runs the emitted text through Braket's own parser.

The ``include "stdgates.inc";`` line is kept because the competition's contract
example shows it and a parser that understands it loses nothing; no gate in the
output depends on it.
"""

from typing import Dict, List

from ..errors import TranspileError
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, ResetOp
from ..passes import lower_to_basis
from .base import format_params

#: Gates spelled identically in stdgates.inc and in Braket's native gate set.
#: ``cx`` is included because the contract names ``cnot`` as an accepted synonym.
BRAKET_BASIS = frozenset({"h", "x", "s", "t", "rz", "ry", "swap", "cx"})

#: LoomQ gate name -> the spelling both dialects accept.
_NAMES = {
    "h": "h",
    "x": "x",
    "s": "s",
    "t": "t",
    "rz": "rz",
    "ry": "ry",
    "swap": "swap",
    "cx": "cnot",
}  # type: Dict[str, str]


def _statement(circuit: Circuit, op) -> str:
    if isinstance(op, GateOp):
        name = _NAMES.get(op.name)
        if name is None:  # pragma: no cover - lower_to_basis guarantees coverage
            raise TranspileError("gate %r has no unambiguous OpenQASM 3 spelling" % op.name)
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
