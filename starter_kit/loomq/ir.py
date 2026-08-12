"""The platform-neutral circuit representation every LoomQ stage speaks.

There is exactly one IR.  The OpenQASM front end produces it, the passes rewrite
it, the emitters serialise it into vendor dialects and the simulator executes it.
Adding a fourth platform means adding one emitter plus one backend — never a new
branch in the front end.  That is what makes the middle layer "universal" rather
than three hard-coded paths behind a single function.

Qubits and clbits are addressed by a flat index.  Declared registers are kept
alongside so emitters can reproduce the user's own naming, and so error messages
can say ``q[1]`` instead of ``qubit 1``.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from .errors import LoomQError


class Register(object):
    """A named ``qreg``/``creg`` mapped onto a slice of the flat index space."""

    __slots__ = ("name", "size", "offset")

    def __init__(self, name: str, size: int, offset: int) -> None:
        self.name = name
        self.size = size
        self.offset = offset

    def index(self, bit: int) -> int:
        if not 0 <= bit < self.size:
            raise LoomQError(
                "index %d is out of range for register %s[%d]" % (bit, self.name, self.size)
            )
        return self.offset + bit

    def indices(self) -> List[int]:
        return [self.offset + bit for bit in range(self.size)]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Register(%r, %d, offset=%d)" % (self.name, self.size, self.offset)


class Operation(object):
    """Base class for anything that can appear in :attr:`Circuit.ops`."""

    __slots__ = ()

    #: Qubit indices this operation touches, in operand order.
    qubits = ()  # type: Sequence[int]


class GateOp(Operation):
    """A unitary gate application.

    ``qubits`` is in *operand order*: for ``cx q[0], q[1]`` the control comes
    first.  Gate matrices follow the same convention (operand 0 is the most
    significant bit of the matrix index), so no reordering is ever needed
    between the parser, the simulator and the emitters.
    """

    __slots__ = ("name", "params", "qubits")

    def __init__(self, name: str, params: Sequence[float], qubits: Sequence[int]) -> None:
        self.name = name
        self.params = tuple(float(value) for value in params)
        self.qubits = tuple(int(index) for index in qubits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "GateOp(%r, %r, %r)" % (self.name, self.params, self.qubits)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, GateOp)
            and self.name == other.name
            and self.params == other.params
            and self.qubits == other.qubits
        )

    def __hash__(self) -> int:
        return hash((self.name, self.params, self.qubits))


class MeasureOp(Operation):
    """``measure q[i] -> c[j]`` on a single bit pair."""

    __slots__ = ("qubit", "clbit")

    def __init__(self, qubit: int, clbit: int) -> None:
        self.qubit = int(qubit)
        self.clbit = int(clbit)

    @property
    def qubits(self) -> Tuple[int, ...]:
        return (self.qubit,)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MeasureOp(%d -> %d)" % (self.qubit, self.clbit)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, MeasureOp)
            and self.qubit == other.qubit
            and self.clbit == other.clbit
        )

    def __hash__(self) -> int:
        return hash(("measure", self.qubit, self.clbit))


class ResetOp(Operation):
    """``reset q[i]`` — collapse to |0>."""

    __slots__ = ("qubit",)

    def __init__(self, qubit: int) -> None:
        self.qubit = int(qubit)

    @property
    def qubits(self) -> Tuple[int, ...]:
        return (self.qubit,)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ResetOp(%d)" % self.qubit


class BarrierOp(Operation):
    """``barrier`` — a scheduling hint with no effect on the state."""

    __slots__ = ("qubits",)

    def __init__(self, qubits: Sequence[int]) -> None:
        self.qubits = tuple(int(index) for index in qubits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "BarrierOp(%r)" % (self.qubits,)


class ConditionalOp(Operation):
    """``if (creg == value) <op>`` — classically controlled operation.

    Evaluation circuits never use this, but valid OpenQASM 2.0 can, and the L2
    agent must not reject a user's working program.  Backends that cannot
    express feed-forward reject it explicitly rather than silently dropping it.
    """

    __slots__ = ("clbits", "value", "body")

    def __init__(self, clbits: Sequence[int], value: int, body: Operation) -> None:
        self.clbits = tuple(int(index) for index in clbits)
        self.value = int(value)
        self.body = body

    @property
    def qubits(self) -> Tuple[int, ...]:
        return tuple(self.body.qubits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ConditionalOp(%r == %d, %r)" % (self.clbits, self.value, self.body)


class Circuit(object):
    """A quantum circuit: registers plus an ordered operation list."""

    def __init__(self) -> None:
        self.qregs = []  # type: List[Register]
        self.cregs = []  # type: List[Register]
        self.ops = []  # type: List[Operation]
        self._qreg_by_name = {}  # type: Dict[str, Register]
        self._creg_by_name = {}  # type: Dict[str, Register]
        self._num_qubits = 0
        self._num_clbits = 0

    # ------------------------------------------------------------------ setup

    def add_qreg(self, name: str, size: int) -> Register:
        if name in self._qreg_by_name:
            raise LoomQError("quantum register %r is declared twice" % name)
        register = Register(name, size, self._num_qubits)
        self.qregs.append(register)
        self._qreg_by_name[name] = register
        self._num_qubits += size
        return register

    def add_creg(self, name: str, size: int) -> Register:
        if name in self._creg_by_name:
            raise LoomQError("classical register %r is declared twice" % name)
        register = Register(name, size, self._num_clbits)
        self.cregs.append(register)
        self._creg_by_name[name] = register
        self._num_clbits += size
        return register

    def qreg(self, name: str) -> Optional[Register]:
        return self._qreg_by_name.get(name)

    def creg(self, name: str) -> Optional[Register]:
        return self._creg_by_name.get(name)

    def append(self, operation: Operation) -> None:
        self.ops.append(operation)

    # ------------------------------------------------------------- properties

    @property
    def num_qubits(self) -> int:
        return self._num_qubits

    @property
    def num_clbits(self) -> int:
        return self._num_clbits

    @property
    def gates(self) -> List[GateOp]:
        return [op for op in self.ops if isinstance(op, GateOp)]

    @property
    def measurements(self) -> List[MeasureOp]:
        return [op for op in self.ops if isinstance(op, MeasureOp)]

    def has_measurements(self) -> bool:
        return any(isinstance(op, MeasureOp) for op in self.ops)

    def depth(self) -> int:
        """Circuit depth counted over gates, measurements and resets."""
        frontier = [0] * max(self.num_qubits, 1)
        depth = 0
        for op in self.ops:
            if isinstance(op, BarrierOp):
                continue
            touched = op.qubits
            if not touched:
                continue
            level = max(frontier[index] for index in touched) + 1
            for index in touched:
                frontier[index] = level
            if level > depth:
                depth = level
        return depth

    # ------------------------------------------------------------------ names

    def qubit_label(self, index: int) -> str:
        """Render a flat qubit index using the user's own register names."""
        for register in self.qregs:
            if register.offset <= index < register.offset + register.size:
                return "%s[%d]" % (register.name, index - register.offset)
        return "q[%d]" % index

    def clbit_label(self, index: int) -> str:
        for register in self.cregs:
            if register.offset <= index < register.offset + register.size:
                return "%s[%d]" % (register.name, index - register.offset)
        return "c[%d]" % index

    # ------------------------------------------------------------------ misc

    def copy_empty(self) -> "Circuit":
        """A circuit with the same registers but no operations."""
        clone = Circuit()
        for register in self.qregs:
            clone.add_qreg(register.name, register.size)
        for register in self.cregs:
            clone.add_creg(register.name, register.size)
        return clone

    def measured_clbits(self) -> List[int]:
        return sorted({op.clbit for op in self.ops if isinstance(op, MeasureOp)})

    def summary(self) -> Dict[str, int]:
        return {
            "qubits": self.num_qubits,
            "clbits": self.num_clbits,
            "gates": len(self.gates),
            "measurements": len(self.measurements),
            "depth": self.depth(),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Circuit(qubits=%d, clbits=%d, ops=%d)" % (
            self.num_qubits,
            self.num_clbits,
            len(self.ops),
        )


__all__ = [
    "Register",
    "Operation",
    "GateOp",
    "MeasureOp",
    "ResetOp",
    "BarrierOp",
    "Circuit",
]
