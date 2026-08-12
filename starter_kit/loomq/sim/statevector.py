"""Exact statevector simulation of a :class:`loomq.ir.Circuit`.

Two execution shapes are covered:

*Terminal measurement* — every ``measure`` sits at the end of the circuit.  The
outcome distribution is then just ``|amplitude|^2`` marginalised onto the
measured clbits, computed in a single pass.  Every evaluation circuit has this
shape.

*Mid-circuit measurement, reset or feed-forward* — the state collapses and later
gates depend on the outcome.  Here the simulator branches: it splits into the
two post-measurement states, renormalises each and recurses, weighting the
leaves by their path probability.  That is exact rather than sampled, at the
cost of ``2^m`` leaves for ``m`` mid-circuit collapses (capped by
:data:`MAX_BRANCHES`).
"""

from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import LoomQError
from ..gates import Matrix, lookup
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp

#: Amplitudes below this are treated as exactly zero when pruning branches.
EPSILON = 1e-12

#: Guard against a circuit whose mid-circuit measurements explode the tree.
MAX_BRANCHES = 4096

#: Refuse to allocate a statevector larger than this (2^26 complex ~ 1 GiB).
MAX_QUBITS = 26

State = List[complex]


# ---------------------------------------------------------------- gate kernel


def _operand_offsets(qubits: Sequence[int]) -> List[int]:
    """Offsets that turn a matrix row index into a statevector index offset.

    Operand ``p`` is bit ``k-1-p`` of the matrix index (operand 0 is the most
    significant), while qubit ``q`` is bit ``q`` of the state index.  Doing the
    translation once per gate keeps the inner loop a single OR.
    """
    count = len(qubits)
    offsets = []
    for row in range(1 << count):
        offset = 0
        for position in range(count):
            if (row >> (count - 1 - position)) & 1:
                offset |= 1 << qubits[position]
        offsets.append(offset)
    return offsets


def apply_matrix(state: State, num_qubits: int, matrix: Matrix, qubits: Sequence[int]) -> None:
    """Apply a ``2^k x 2^k`` unitary to ``qubits`` in place."""
    count = len(qubits)
    dimension = 1 << count
    offsets = _operand_offsets(qubits)
    # Skipping structural zeros makes cx/ccx/swap roughly as cheap as a permutation.
    rows = [
        [(column, matrix[row][column]) for column in range(dimension) if matrix[row][column] != 0]
        for row in range(dimension)
    ]
    occupied = 0
    for qubit in qubits:
        occupied |= 1 << qubit

    for base in range(1 << num_qubits):
        if base & occupied:
            continue
        amplitudes = [state[base | offset] for offset in offsets]
        for row in range(dimension):
            total = 0j
            for column, value in rows[row]:
                total += value * amplitudes[column]
            state[base | offsets[row]] = total


def _marginal(state: State, qubit: int) -> Tuple[float, float]:
    """Probability of measuring ``qubit`` as 0 and as 1."""
    bit = 1 << qubit
    probability_one = 0.0
    for index, amplitude in enumerate(state):
        if index & bit:
            probability_one += amplitude.real * amplitude.real + amplitude.imag * amplitude.imag
    total = 0.0
    for amplitude in state:
        total += amplitude.real * amplitude.real + amplitude.imag * amplitude.imag
    return max(total - probability_one, 0.0), max(probability_one, 0.0)


def _project(state: State, qubit: int, outcome: int, probability: float) -> State:
    """The post-measurement state for ``outcome``, renormalised."""
    bit = 1 << qubit
    scale = 1.0 / (probability ** 0.5)
    projected = [0j] * len(state)
    for index, amplitude in enumerate(state):
        if ((index & bit) != 0) == bool(outcome):
            projected[index] = amplitude * scale
    return projected


def _collapse_to_zero(state: State, qubit: int) -> State:
    """Move all amplitude of ``qubit`` onto |0> (used by ``reset``)."""
    bit = 1 << qubit
    reset_state = [0j] * len(state)
    for index, amplitude in enumerate(state):
        if amplitude != 0j:
            reset_state[index & ~bit] += amplitude
    return reset_state


# ------------------------------------------------------------------ analysis


def measurement_width(circuit: Circuit) -> int:
    """Number of characters in a counts key for this circuit."""
    if circuit.num_clbits:
        return circuit.num_clbits
    if circuit.has_measurements():
        return max(op.clbit for op in circuit.measurements) + 1
    return max(circuit.num_qubits, 1)


def _is_terminal_suffix(ops: Sequence[Operation], position: int) -> bool:
    """True when nothing after ``position`` can change the state any more."""
    for op in ops[position:]:
        if not isinstance(op, (MeasureOp, BarrierOp)):
            return False
    return True


def _format_key(value: int, width: int) -> str:
    return "".join("1" if (value >> bit) & 1 else "0" for bit in range(width - 1, -1, -1))


# ------------------------------------------------------------------ evolution


class _Budget(object):
    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise LoomQError(
                "this circuit has too many mid-circuit measurements to simulate exactly "
                "(limit %d branches)" % MAX_BRANCHES
            )


def _apply_gate(state: State, num_qubits: int, op: GateOp) -> None:
    definition = lookup(op.name)
    apply_matrix(state, num_qubits, definition.matrix(op.params), op.qubits)


def _condition_holds(op: ConditionalOp, clvalues: int) -> bool:
    observed = 0
    for position, clbit in enumerate(op.clbits):
        if (clvalues >> clbit) & 1:
            observed |= 1 << position
    return observed == op.value


def _evolve(
    state: State,
    circuit: Circuit,
    position: int,
    clvalues: int,
    weight: float,
    width: int,
    accumulator: Dict[str, float],
    budget: _Budget,
) -> None:
    ops = circuit.ops
    num_qubits = circuit.num_qubits
    total = len(ops)

    while position < total:
        op = ops[position]

        if isinstance(op, BarrierOp):
            position += 1
            continue

        if isinstance(op, GateOp):
            _apply_gate(state, num_qubits, op)
            position += 1
            continue

        collapsing = op  # type: Operation
        if isinstance(op, ConditionalOp):
            if not _condition_holds(op, clvalues):
                position += 1
                continue
            body = op.body
            if isinstance(body, GateOp):
                _apply_gate(state, num_qubits, body)
                position += 1
                continue
            if isinstance(body, BarrierOp):
                position += 1
                continue
            collapsing = body

        if isinstance(collapsing, MeasureOp) and _is_terminal_suffix(ops, position):
            break

        if isinstance(collapsing, (MeasureOp, ResetOp)):
            qubit = collapsing.qubit
            probability_zero, probability_one = _marginal(state, qubit)
            for outcome, probability in ((0, probability_zero), (1, probability_one)):
                if probability <= EPSILON:
                    continue
                budget.spend()
                branch = _project(state, qubit, outcome, probability)
                if isinstance(collapsing, ResetOp):
                    if outcome == 1:
                        _apply_gate(branch, num_qubits, GateOp("x", (), (qubit,)))
                    next_clvalues = clvalues
                else:
                    next_clvalues = clvalues & ~(1 << collapsing.clbit)
                    if outcome:
                        next_clvalues |= 1 << collapsing.clbit
                _evolve(
                    branch,
                    circuit,
                    position + 1,
                    next_clvalues,
                    weight * probability,
                    width,
                    accumulator,
                    budget,
                )
            return

        raise LoomQError("cannot simulate operation %r" % (op,))

    # Terminal suffix: fold |amplitude|^2 into the accumulated distribution.
    terminal = [
        (op.qubit, op.clbit) for op in ops[position:] if isinstance(op, MeasureOp)
    ]
    if not terminal and not circuit.has_measurements():
        terminal = [(qubit, qubit) for qubit in range(num_qubits)]

    for index, amplitude in enumerate(state):
        probability = amplitude.real * amplitude.real + amplitude.imag * amplitude.imag
        if probability <= EPSILON:
            continue
        value = clvalues
        for qubit, clbit in terminal:
            value &= ~(1 << clbit)
            if (index >> qubit) & 1:
                value |= 1 << clbit
        key = _format_key(value, width)
        accumulator[key] = accumulator.get(key, 0.0) + probability * weight


# -------------------------------------------------------------- entry points


def simulate_statevector(circuit: Circuit) -> State:
    """Unitary evolution only — raises if the circuit collapses mid-flight.

    Used by tests and by the transpiler's equivalence checker, where comparing
    amplitudes is stricter (and cheaper) than comparing sampled counts.
    """
    if circuit.num_qubits > MAX_QUBITS:
        raise LoomQError(
            "LoomQ's reference simulator handles up to %d qubits, this circuit needs %d"
            % (MAX_QUBITS, circuit.num_qubits)
        )
    state = [0j] * (1 << max(circuit.num_qubits, 1))
    state[0] = 1 + 0j
    for op in circuit.ops:
        if isinstance(op, GateOp):
            _apply_gate(state, circuit.num_qubits, op)
        elif isinstance(op, (BarrierOp, MeasureOp)):
            continue
        else:
            raise LoomQError(
                "simulate_statevector() needs a purely unitary circuit; found %r" % (op,)
            )
    return state


def ideal_distribution(circuit: Circuit, width: Optional[int] = None) -> Dict[str, float]:
    """Exact outcome probabilities keyed by ``c[n-1]...c[1]c[0]`` bit strings."""
    if circuit.num_qubits > MAX_QUBITS:
        raise LoomQError(
            "LoomQ's reference simulator handles up to %d qubits, this circuit needs %d"
            % (MAX_QUBITS, circuit.num_qubits)
        )
    if width is None:
        width = measurement_width(circuit)

    state = [0j] * (1 << max(circuit.num_qubits, 1))
    state[0] = 1 + 0j
    accumulator = {}  # type: Dict[str, float]
    _evolve(state, circuit, 0, 0, 1.0, width, accumulator, _Budget(MAX_BRANCHES))

    total = sum(accumulator.values())
    if total <= 0.0:
        raise LoomQError("the circuit produced no measurable outcome")
    return {key: value / total for key, value in accumulator.items()}


__all__ = [
    "EPSILON",
    "MAX_BRANCHES",
    "MAX_QUBITS",
    "apply_matrix",
    "ideal_distribution",
    "measurement_width",
    "simulate_statevector",
]
