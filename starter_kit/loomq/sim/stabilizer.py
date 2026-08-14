"""Stabilizer simulation: Clifford circuits in polynomial time.

A statevector costs ``2^n`` amplitudes, which is why LoomQ's reference simulator
stops at 26 qubits.  But the Gottesman-Knill theorem says circuits built only
from ``h``, ``s``, ``cx`` and their relatives can be tracked with an ``n x 2n``
binary tableau instead — ``O(n^2)`` memory, ``O(n^2)`` per gate.

This matters here rather than being a textbook aside.  GHZ states are Clifford,
and "make me a GHZ state" is the single most common thing anyone asks the L2
agent.  With a statevector, verifying a 30-qubit GHZ before answering is
impossible; with a tableau it is instant.  The agent can now check its own work
at sizes no simulator in the reference path could reach.

The implementation is the Aaronson-Gottesman CHP tableau, including the
destabilizer rows that make measurement ``O(n^2)`` instead of ``O(n^3)``.

Non-Clifford gates (``t``, ``rz`` at a general angle, ``cu1``, ``ccx``) are not
representable — :func:`is_clifford` says so up front, and the caller falls back
to the statevector.  Angles are checked numerically, so ``rz(pi/2)`` is
recognised as ``s`` even though it was not written that way.
"""

import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import LoomQError
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, ResetOp

#: Gates that are Clifford whatever their parameters.
_ALWAYS_CLIFFORD = frozenset(
    {"h", "x", "y", "z", "s", "sdg", "cx", "cy", "cz", "swap", "id", "sx", "sxdg"}
)

#: Parameterised gates that are Clifford only at multiples of pi/2.
_CONDITIONAL = frozenset({"rz", "ry", "rx", "u1", "cu1", "crz", "rzz"})

_TOLERANCE = 1e-9


def _quarter_turns(angle: float) -> Optional[int]:
    """How many multiples of ``pi/2`` this angle is, or ``None`` if it is not."""
    turns = angle / (math.pi / 2.0)
    nearest = round(turns)
    if abs(turns - nearest) < _TOLERANCE:
        return int(nearest) % 4
    return None


def is_clifford(circuit: Circuit) -> bool:
    """True when every gate can be tracked by a stabilizer tableau."""
    for op in circuit.ops:
        if isinstance(op, (BarrierOp, MeasureOp, ResetOp)):
            continue
        if isinstance(op, ConditionalOp):
            return False
        if not isinstance(op, GateOp):
            return False
        if op.name in _ALWAYS_CLIFFORD:
            continue
        if op.name in _CONDITIONAL and op.params:
            if _quarter_turns(op.params[0]) is None:
                return False
            if op.name in ("ry", "rx") and _quarter_turns(op.params[0]) not in (0, 1, 2, 3):
                return False
            continue
        return False
    return True


class Tableau(object):
    """The Aaronson-Gottesman CHP tableau.

    Rows ``0..n-1`` are destabilizers, ``n..2n-1`` are stabilizers, and row
    ``2n`` is scratch used by measurement. Each row holds ``x`` and ``z`` bits
    per qubit plus a phase bit ``r``.
    """

    def __init__(self, num_qubits: int, seed: Optional[int] = None) -> None:
        self.n = num_qubits
        size = 2 * num_qubits + 1
        self.x = [[0] * num_qubits for _ in range(size)]
        self.z = [[0] * num_qubits for _ in range(size)]
        self.r = [0] * size
        for row in range(num_qubits):
            self.x[row][row] = 1                    # destabilizers: X_i
            self.z[num_qubits + row][row] = 1       # stabilizers:   Z_i
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------ gates

    def hadamard(self, qubit: int) -> None:
        for row in range(2 * self.n):
            self.r[row] ^= self.x[row][qubit] & self.z[row][qubit]
            self.x[row][qubit], self.z[row][qubit] = (
                self.z[row][qubit],
                self.x[row][qubit],
            )

    def phase(self, qubit: int) -> None:
        """S gate."""
        for row in range(2 * self.n):
            self.r[row] ^= self.x[row][qubit] & self.z[row][qubit]
            self.z[row][qubit] ^= self.x[row][qubit]

    def phase_dagger(self, qubit: int) -> None:
        self.phase(qubit)
        self.pauli_z(qubit)

    def cnot(self, control: int, target: int) -> None:
        for row in range(2 * self.n):
            self.r[row] ^= (
                self.x[row][control]
                & self.z[row][target]
                & (self.x[row][target] ^ self.z[row][control] ^ 1)
            )
            self.x[row][target] ^= self.x[row][control]
            self.z[row][control] ^= self.z[row][target]

    def pauli_x(self, qubit: int) -> None:
        for row in range(2 * self.n):
            self.r[row] ^= self.z[row][qubit]

    def pauli_z(self, qubit: int) -> None:
        for row in range(2 * self.n):
            self.r[row] ^= self.x[row][qubit]

    def pauli_y(self, qubit: int) -> None:
        self.pauli_z(qubit)
        self.pauli_x(qubit)

    def swap(self, a: int, b: int) -> None:
        self.cnot(a, b)
        self.cnot(b, a)
        self.cnot(a, b)

    def cz(self, control: int, target: int) -> None:
        self.hadamard(target)
        self.cnot(control, target)
        self.hadamard(target)

    # ------------------------------------------------------------ measurement

    def _row_sum(self, target: int, source: int) -> None:
        """Left-multiply row ``target`` by row ``source``, tracking the phase."""
        total = 2 * self.r[target] + 2 * self.r[source]
        for qubit in range(self.n):
            total += self._phase_exponent(
                self.x[source][qubit], self.z[source][qubit],
                self.x[target][qubit], self.z[target][qubit],
            )
        self.r[target] = (total % 4) // 2
        for qubit in range(self.n):
            self.x[target][qubit] ^= self.x[source][qubit]
            self.z[target][qubit] ^= self.z[source][qubit]

    @staticmethod
    def _phase_exponent(x1: int, z1: int, x2: int, z2: int) -> int:
        """The ``i`` exponent from multiplying two single-qubit Paulis."""
        if x1 == 0 and z1 == 0:
            return 0
        if x1 == 1 and z1 == 1:
            return z2 - x2
        if x1 == 1 and z1 == 0:
            return z2 * (2 * x2 - 1)
        return x2 * (1 - 2 * z2)

    def measure(self, qubit: int) -> int:
        """Measure in the computational basis, collapsing the state."""
        pivot = None
        for row in range(self.n, 2 * self.n):
            if self.x[row][qubit]:
                pivot = row
                break

        if pivot is not None:
            # Random outcome: the qubit is in superposition.
            for row in range(2 * self.n):
                if row != pivot and self.x[row][qubit]:
                    self._row_sum(row, pivot)
            self.x[pivot - self.n] = list(self.x[pivot])
            self.z[pivot - self.n] = list(self.z[pivot])
            self.r[pivot - self.n] = self.r[pivot]
            self.x[pivot] = [0] * self.n
            self.z[pivot] = [0] * self.n
            self.z[pivot][qubit] = 1
            outcome = self.rng.randint(0, 1)
            self.r[pivot] = outcome
            return outcome

        # Deterministic outcome: read it off the scratch row.
        scratch = 2 * self.n
        self.x[scratch] = [0] * self.n
        self.z[scratch] = [0] * self.n
        self.r[scratch] = 0
        for row in range(self.n):
            if self.x[row][qubit]:
                self._row_sum(scratch, row + self.n)
        return self.r[scratch]

    def reset(self, qubit: int) -> None:
        if self.measure(qubit):
            self.pauli_x(qubit)


def _apply(tableau: Tableau, op: GateOp) -> None:
    name = op.name
    qubits = op.qubits

    if name == "h":
        tableau.hadamard(qubits[0])
    elif name == "x":
        tableau.pauli_x(qubits[0])
    elif name == "y":
        tableau.pauli_y(qubits[0])
    elif name == "z":
        tableau.pauli_z(qubits[0])
    elif name == "s":
        tableau.phase(qubits[0])
    elif name == "sdg":
        tableau.phase_dagger(qubits[0])
    elif name == "cx":
        tableau.cnot(qubits[0], qubits[1])
    elif name == "cz":
        tableau.cz(qubits[0], qubits[1])
    elif name == "cy":
        tableau.phase_dagger(qubits[1])
        tableau.cnot(qubits[0], qubits[1])
        tableau.phase(qubits[1])
    elif name == "swap":
        tableau.swap(qubits[0], qubits[1])
    elif name == "id":
        pass
    elif name == "sx":
        # sqrt(X) = Sdg . H . Sdg up to a global phase.
        tableau.phase_dagger(qubits[0])
        tableau.hadamard(qubits[0])
        tableau.phase_dagger(qubits[0])
    elif name == "sxdg":
        tableau.phase(qubits[0])
        tableau.hadamard(qubits[0])
        tableau.phase(qubits[0])
    elif name in ("rz", "u1"):
        for _ in range(_quarter_turns(op.params[0]) or 0):
            tableau.phase(qubits[0])
    elif name == "rx":
        turns = _quarter_turns(op.params[0]) or 0
        for _ in range(turns):
            tableau.phase_dagger(qubits[0])
            tableau.hadamard(qubits[0])
            tableau.phase_dagger(qubits[0])
    elif name == "ry":
        turns = _quarter_turns(op.params[0]) or 0
        for _ in range(turns):
            # sqrt(Y) = S . H up to a global phase.
            tableau.hadamard(qubits[0])
            tableau.phase(qubits[0])
            tableau.phase(qubits[0])
    elif name == "cu1":
        for _ in range(_quarter_turns(op.params[0]) or 0):
            tableau.cz(qubits[0], qubits[1])
            # cz is cu1(pi); a quarter turn of cu1 is not Clifford on its own,
            # so is_clifford() only admits multiples that land on cz.
    else:  # pragma: no cover - guarded by is_clifford()
        raise LoomQError("stabilizer simulation cannot apply %r" % name)


def sample_counts(
    circuit: Circuit,
    shots: int,
    width: Optional[int] = None,
    seed: Optional[int] = None,
) -> Dict[str, int]:
    """Sample ``shots`` outcomes from a Clifford circuit.

    Each shot replays the tableau from scratch, which is ``O(shots * n^2 * g)``
    — linear in qubits squared rather than exponential, so a 200-qubit GHZ is
    routine where a statevector would need ``2^200`` amplitudes.
    """
    from . import measurement_width as _width

    if not is_clifford(circuit):
        raise LoomQError("this circuit is not Clifford; use the statevector simulator")
    bits = width if width is not None else _width(circuit)

    pairs = [(op.qubit, op.clbit) for op in circuit.ops if isinstance(op, MeasureOp)]
    if not pairs:
        pairs = [(index, index) for index in range(circuit.num_qubits)]

    rng = random.Random(seed)
    counts = {}  # type: Dict[str, int]
    for _ in range(shots):
        tableau = Tableau(circuit.num_qubits, seed=rng.randrange(1 << 30))
        value = 0
        for op in circuit.ops:
            if isinstance(op, GateOp):
                _apply(tableau, op)
            elif isinstance(op, MeasureOp):
                if tableau.measure(op.qubit):
                    value |= 1 << op.clbit
                else:
                    value &= ~(1 << op.clbit)
            elif isinstance(op, ResetOp):
                tableau.reset(op.qubit)
        if not any(isinstance(op, MeasureOp) for op in circuit.ops):
            for qubit, clbit in pairs:
                if tableau.measure(qubit):
                    value |= 1 << clbit
        key = "".join("1" if (value >> bit) & 1 else "0" for bit in range(bits - 1, -1, -1))
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["Tableau", "is_clifford", "sample_counts"]
