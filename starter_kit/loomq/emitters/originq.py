"""Origin Quantum target: the canonical OriginIR subset.

OriginIR has no notion of user-named registers — it opens with ``QINIT n`` /
``CREG m`` and then addresses a single flat ``q[i]`` / ``c[i]`` space.  LoomQ's
IR is already flat-indexed, so the mapping is the identity and multi-register
programs (``qreg a[2]; qreg b[1];``) flatten in declaration order without any
special case.

Gate spellings follow ``target_ir_contract.md``: ``SDAG``/``TDAG`` for the
daggered phase gates, ``CNOT`` for ``cx``, ``TOFFOLI`` for ``ccx``, and the
parameter-before-operand form ``RZ(theta) q[0]``.
"""

from typing import Dict, List

from ..errors import TranspileError
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, ResetOp
from ..passes import WHITELIST_BASIS, lower_to_basis
from .base import format_params, reject_conditionals

ORIGINQ_BASIS = WHITELIST_BASIS

#: LoomQ gate name -> OriginIR mnemonic.
_NAMES = {
    "h": "H",
    "x": "X",
    "s": "S",
    "sdg": "SDAG",
    "t": "T",
    "tdg": "TDAG",
    "rz": "RZ",
    "ry": "RY",
    "cx": "CNOT",
    "cu1": "CU1",
    "swap": "SWAP",
    "ccx": "TOFFOLI",
}  # type: Dict[str, str]


def _statement(op) -> str:
    if isinstance(op, GateOp):
        name = _NAMES.get(op.name)
        if name is None:  # pragma: no cover - lower_to_basis guarantees coverage
            raise TranspileError("gate %r has no OriginIR mnemonic" % op.name)
        operands = ", ".join("q[%d]" % index for index in op.qubits)
        return "%s%s %s" % (name, format_params(op.params), operands)
    if isinstance(op, MeasureOp):
        return "MEASURE q[%d], c[%d]" % (op.qubit, op.clbit)
    if isinstance(op, ResetOp):
        return "RESET q[%d]" % op.qubit
    raise AssertionError("unhandled operation %r" % (op,))


def emit_originq(circuit: Circuit) -> str:
    """Render ``circuit`` as OriginIR."""
    reject_conditionals(circuit, "OriginIR")
    lowered = lower_to_basis(circuit, ORIGINQ_BASIS)

    clbits = lowered.num_clbits or lowered.num_qubits
    lines = ["QINIT %d" % lowered.num_qubits, "CREG %d" % clbits]  # type: List[str]

    for op in lowered.ops:
        if isinstance(op, BarrierOp):
            continue  # OriginIR's canonical subset has no barrier
        if isinstance(op, ConditionalOp):  # pragma: no cover - rejected above
            raise TranspileError("OriginIR cannot express classical feed-forward")
        lines.append(_statement(op))

    if not lowered.has_measurements():
        for qubit in range(lowered.num_qubits):
            lines.append("MEASURE q[%d], c[%d]" % (qubit, qubit))

    return "\n".join(lines) + "\n"


__all__ = ["ORIGINQ_BASIS", "emit_originq"]
