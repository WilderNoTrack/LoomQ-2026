"""Commutation-aware cancellation and single-qubit run resynthesis.

:mod:`loomq.passes.optimize` handles the easy cases — identity rotations, two
adjacent rotations about the same axis.  This pass handles the two that matter
on hardware:

**Self-inverse pairs.** ``h h``, ``x x``, ``cx cx`` and ``swap swap`` are the
identity.  They rarely appear adjacent in source, but decomposition produces
them constantly: lowering ``ccx`` next to ``cz`` leaves ``h`` against ``h``.
Cancelling needs a *commutation* rule, not just adjacency — the two gates may be
separated by operations that do not touch their qubits, or that commute with
them.

**Single-qubit runs.** A chain of one-qubit gates on the same wire is one 2x2
unitary, whatever its length.  Resynthesising it through ZYZ replaces the chain
with at most three rotations, which is what the generic decomposition emits
anyway — so a ``ccx`` expansion that leaves five gates in a row on one qubit
collapses to three or fewer.

Both rewrites are exact up to global phase, and the test suite checks that by
comparing full statevectors, not counts.

Why it matters: gate count is noise on a real device. The GHZ-3 that ran on
Wukong spent 90% of its shots in the main peaks; every gate removed from a
hardware circuit buys back some of the missing 10%.
"""

import cmath
from typing import Dict, List, Optional, Sequence, Tuple

from ..gates import Matrix, lookup
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp
from .decompose import zyz_angles
from .optimize import ANGLE_EPSILON, _wrap_angle

#: Gates that are their own inverse, so two in a row cancel.
SELF_INVERSE = frozenset({"h", "x", "y", "z", "cx", "cz", "cy", "swap", "ccx", "cswap", "id"})

#: Pairs that annihilate each other.
INVERSE_PAIRS = {
    ("s", "sdg"), ("sdg", "s"),
    ("t", "tdg"), ("tdg", "t"),
    ("sx", "sxdg"), ("sxdg", "sx"),
}

#: Rotations whose angles add.
ADDITIVE = frozenset({"rz", "ry", "rx"})

#: Gates that are diagonal in the computational basis, so they commute with any
#: other diagonal gate and with a control line.
DIAGONAL = frozenset({"rz", "s", "sdg", "t", "tdg", "z", "u1", "cu1", "cz", "rzz"})

_MAX_PASSES = 8


def _is_barrier_like(op: Operation) -> bool:
    return isinstance(op, (BarrierOp, MeasureOp, ResetOp, ConditionalOp))


def _commutes(first: GateOp, second: GateOp) -> bool:
    """Can ``second`` move before ``first`` without changing the circuit?

    Only rules that are unconditionally true are used — disjoint supports, and
    two diagonal gates. Anything subtler is left alone; a missed cancellation
    costs a gate, a wrong one costs the answer.
    """
    if not set(first.qubits) & set(second.qubits):
        return True
    if first.name in DIAGONAL and second.name in DIAGONAL:
        return True
    return False


def _cancel(circuit: Circuit) -> Tuple[Circuit, int]:
    """Remove self-inverse pairs and inverse pairs, looking through commuters."""
    ops = list(circuit.ops)
    removed = 0
    index = 0

    while index < len(ops):
        current = ops[index]
        if not isinstance(current, GateOp):
            index += 1
            continue

        partner = None
        for lookahead in range(index + 1, len(ops)):
            candidate = ops[lookahead]
            if _is_barrier_like(candidate):
                break
            if not isinstance(candidate, GateOp):
                break
            if candidate.qubits == current.qubits:
                if _pair_cancels(current, candidate):
                    partner = lookahead
                break
            if not _commutes(current, candidate):
                break

        if partner is not None:
            del ops[partner]
            del ops[index]
            removed += 2
            index = max(index - 1, 0)
            continue
        index += 1

    if not removed:
        return circuit, 0
    result = circuit.copy_empty()
    for op in ops:
        result.append(op)
    return result, removed


def _pair_cancels(first: GateOp, second: GateOp) -> bool:
    if first.name in SELF_INVERSE and second.name == first.name and not first.params:
        return True
    if (first.name, second.name) in INVERSE_PAIRS:
        return True
    if first.name in ADDITIVE and second.name == first.name:
        return abs(_wrap_angle(first.params[0] + second.params[0])) < ANGLE_EPSILON
    if first.name == "cu1" and second.name == "cu1":
        return abs(_wrap_angle(first.params[0] + second.params[0])) < ANGLE_EPSILON
    return False


def _matrix_product(first: Matrix, second: Matrix) -> Matrix:
    """``second @ first`` — circuit order, so ``first`` is applied first."""
    return [
        [
            sum(second[row][k] * first[k][column] for k in range(2))
            for column in range(2)
        ]
        for row in range(2)
    ]


def _resynthesise_runs(circuit: Circuit) -> Tuple[Circuit, int]:
    """Collapse each run of *consecutive* one-qubit gates into <= 3 rotations.

    Deliberately only consecutive: reordering across other operations would need
    the commutation analysis to be exhaustive, and a decomposition's output is
    consecutive anyway — a lowered ``ccx`` leaves five gates in a row on the
    target wire.
    """
    ops = list(circuit.ops)
    result = circuit.copy_empty()
    saved = 0
    index = 0

    while index < len(ops):
        op = ops[index]
        if not (isinstance(op, GateOp) and len(op.qubits) == 1):
            result.append(op)
            index += 1
            continue

        qubit = op.qubits[0]
        end = index
        while (
            end + 1 < len(ops)
            and isinstance(ops[end + 1], GateOp)
            and ops[end + 1].qubits == (qubit,)
        ):
            end += 1

        run = ops[index:end + 1]
        if len(run) < 2:
            result.append(op)
            index += 1
            continue

        combined = lookup(run[0].name).matrix(run[0].params)
        for element in run[1:]:
            combined = _matrix_product(combined, lookup(element.name).matrix(element.params))

        replacement = _from_matrix(combined, qubit)
        chosen = replacement if len(replacement) < len(run) else run
        saved += len(run) - len(chosen)
        for produced in chosen:
            result.append(produced)
        index = end + 1

    return (result, saved) if saved else (circuit, 0)


def _from_matrix(matrix: Matrix, qubit: int) -> List[GateOp]:
    """The shortest rz/ry sequence equal to ``matrix`` up to global phase."""
    _, beta, gamma, delta = zyz_angles(matrix)
    produced = []
    for name, angle in (("rz", delta), ("ry", gamma), ("rz", beta)):
        wrapped = _wrap_angle(angle)
        if abs(wrapped) >= ANGLE_EPSILON:
            produced.append(GateOp(name, (wrapped,), (qubit,)))
    return produced


def peephole(circuit: Circuit) -> Circuit:
    """Run cancellation and resynthesis to a fixed point."""
    current = circuit
    for _ in range(_MAX_PASSES):
        cancelled, removed = _cancel(current)
        resynthesised, saved = _resynthesise_runs(cancelled)
        current = resynthesised
        if not removed and not saved:
            break
    return current


def savings(before: Circuit, after: Circuit) -> Dict[str, int]:
    return {
        "gates_before": len(before.gates),
        "gates_after": len(after.gates),
        "removed": len(before.gates) - len(after.gates),
        "depth_before": before.depth(),
        "depth_after": after.depth(),
    }


__all__ = ["ADDITIVE", "DIAGONAL", "SELF_INVERSE", "peephole", "savings"]
