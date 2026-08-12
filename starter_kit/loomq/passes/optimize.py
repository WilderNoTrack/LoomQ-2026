"""Peephole clean-up applied after lowering.

Generic decomposition is correct but chatty: the ABC construction happily emits
``rz(0)`` when a gate needs no phase correction, and two rotations about the same
axis land next to each other whenever two rewrites meet.  Both are removed here.

Only exact rewrites are used — merging ``rz(a)`` into ``rz(b)`` is addition, and
dropping a zero-angle rotation drops the identity — so the optimiser can never
change what a circuit means, only how much of it a reviewer has to read.
"""

import math
from typing import Dict, List, Optional

from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp

#: Angles closer to zero than this are treated as exactly zero.
ANGLE_EPSILON = 1e-12

#: Single-qubit rotations that compose by adding their angles.
_ADDITIVE = ("rz", "ry", "rx")

#: Gates that vanish when their angle is zero.
_IDENTITY_AT_ZERO = ("rz", "ry", "rx", "cu1", "u1")


def _wrap_angle(value: float) -> float:
    """Fold an angle into ``(-pi, pi]`` so printed circuits stay readable."""
    wrapped = math.fmod(value, 2.0 * math.pi)
    if wrapped > math.pi:
        wrapped -= 2.0 * math.pi
    elif wrapped <= -math.pi:
        wrapped += 2.0 * math.pi
    return wrapped


def _is_identity(op: GateOp) -> bool:
    if op.name == "id":
        return True
    if op.name in _IDENTITY_AT_ZERO and op.params:
        return abs(_wrap_angle(op.params[0])) < ANGLE_EPSILON
    return False


def optimize(circuit: Circuit) -> Circuit:
    """Remove identity rotations and merge adjacent same-axis rotations."""
    result = circuit.copy_empty()
    ops = []  # type: List[Operation]
    # Index in ``ops`` of the last operation touching each qubit.
    last_on_qubit = {}  # type: Dict[int, int]

    for op in circuit.ops:
        if isinstance(op, GateOp):
            if _is_identity(op):
                continue

            if op.name in _ADDITIVE and len(op.qubits) == 1:
                qubit = op.qubits[0]
                previous_index = last_on_qubit.get(qubit)
                previous = ops[previous_index] if previous_index is not None else None
                if (
                    isinstance(previous, GateOp)
                    and previous.name == op.name
                    and previous.qubits == op.qubits
                ):
                    merged_angle = _wrap_angle(previous.params[0] + op.params[0])
                    if abs(merged_angle) < ANGLE_EPSILON:
                        ops[previous_index] = None  # type: ignore[assignment]
                        del last_on_qubit[qubit]
                    else:
                        ops[previous_index] = GateOp(op.name, (merged_angle,), op.qubits)
                    continue

            if op.params and op.name in _ADDITIVE:
                op = GateOp(op.name, tuple(_wrap_angle(value) for value in op.params), op.qubits)

        ops.append(op)
        index = len(ops) - 1
        touched = op.qubits if not isinstance(op, ConditionalOp) else op.body.qubits
        for qubit in touched:
            last_on_qubit[qubit] = index
        if isinstance(op, (MeasureOp, ResetOp, BarrierOp, ConditionalOp)):
            # A collapse or a barrier breaks the rotation chain on those qubits.
            for qubit in touched:
                last_on_qubit.pop(qubit, None)

    for op in ops:
        if op is not None:
            result.append(op)
    return result


__all__ = ["ANGLE_EPSILON", "optimize"]
