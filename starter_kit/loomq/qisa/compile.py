"""Hybrid-QASM -> one unified LoomQ-Q instruction stream.

L3's contract splits a hybrid program in two and leaves the harness to inject
measurement values from outside.  Here there is nothing to inject: ``qmeas``
writes its outcome into ``x10`` and the classical block that reads ``x10`` is
the next instruction.  The branch is taken *because* the qubit collapsed the way
it did, inside a single program counter.

Register budget, fixed so the two halves cannot collide:

    x0            hard-wired zero
    x1  .. x9     the classical variables r1..r9
    x10 .. x19    measurement outcomes c[0], c[1], ... (written by qmeas)
    x20 .. x27    scratch for the classical code generator
    x28 .. x31    qubit-index and angle operands for quantum instructions

The quantum operand registers are cleared on exit for the same reason the L3
generator clears its own scratch: the final register state should contain the
program's results and nothing else.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import HybridQasmError
from ..hybrid.ast import FIRST_MEASUREMENT_REGISTER, Program
from ..hybrid.parser import parse_hybrid_segments
from ..hybrid.riscv import generate_assembly
from ..ir import BarrierOp, Circuit, GateOp, MeasureOp, ResetOp
from ..passes import lower_to_basis
from .isa import GATE_MNEMONIC, SPEC, quantize_angle

#: Registers the quantum instructions use for operands.
QUBIT_REGISTERS = (28, 29, 30)
ANGLE_REGISTER = 31

#: Highest register the classical generator may take as scratch.
CLASSICAL_SCRATCH_TOP = 27


class _Emitter(object):
    def __init__(self) -> None:
        self.lines = []  # type: List[str]
        self.loaded = {}  # type: Dict[int, int]
        self.touched = set()  # type: set

    def emit(self, text: str, comment: Optional[str] = None) -> None:
        self.lines.append("    %-28s%s" % (text, "# " + comment if comment else ""))

    def note(self, text: str) -> None:
        self.lines.append("# " + text)

    def load(self, register: int, value: int, comment: Optional[str] = None) -> None:
        """``li`` with a tiny cache, so a repeated qubit index costs nothing."""
        if self.loaded.get(register) == value:
            return
        self.emit("li x%d, %d" % (register, value), comment)
        self.loaded[register] = value
        self.touched.add(register)

    def invalidate(self) -> None:
        """Forget the cache across a classical block, which may clobber anything."""
        self.loaded.clear()


def _gate_instruction(emitter: _Emitter, op: GateOp) -> None:
    mnemonic = GATE_MNEMONIC.get(op.name)
    if mnemonic is None:
        raise HybridQasmError("LoomQ-Q has no instruction for gate %r" % op.name)

    for position, qubit in enumerate(op.qubits):
        emitter.load(QUBIT_REGISTERS[position], qubit, "qubit index %d" % qubit)
    operands = ["x%d" % QUBIT_REGISTERS[index] for index in range(len(op.qubits))]

    if op.params:
        value = quantize_angle(op.params[0])
        emitter.load(ANGLE_REGISTER, value, "%.6f rad" % op.params[0])
        operands.append("x%d" % ANGLE_REGISTER)

    order = SPEC[mnemonic].operands
    if len(order) != len(operands):  # pragma: no cover - table is self-consistent
        raise HybridQasmError("operand count mismatch for %s" % mnemonic)
    emitter.emit("%s %s" % (mnemonic, ", ".join(operands)))


def compile_unified(hybrid_qasm_str: str) -> str:
    """Compile a Hybrid-QASM program into a single LoomQ-Q assembly listing."""
    circuit, segments = parse_hybrid_segments(hybrid_qasm_str)
    lowered_ops = _lowered_ops(circuit)

    emitter = _Emitter()
    emitter.note("LoomQ-Q unified stream — quantum and classical in one program")
    emitter.note("x1..x9 = r1..r9   x10.. = measurement outcomes   x28..x31 = quantum operands")
    emitter.load(QUBIT_REGISTERS[0], circuit.num_qubits, "qubit count")
    emitter.emit("qinit x%d" % QUBIT_REGISTERS[0])
    emitter.loaded.clear()

    label_index = 0
    for kind, payload in segments:
        if kind == "gates":
            start, end = payload  # type: ignore[misc]
            for expansion in lowered_ops[start:end]:
                for op in expansion:
                    _emit_operation(emitter, op)
        else:
            label_index += 1
            program = payload  # type: ignore[assignment]
            emitter.note("classical block %d" % label_index)
            block = generate_assembly(
                program,
                top=CLASSICAL_SCRATCH_TOP,
                label_prefix="LQQ%d" % label_index,
                header=False,
            )
            emitter.lines.extend(block.rstrip("\n").split("\n"))
            emitter.invalidate()

    if emitter.touched:
        emitter.note("clear quantum operand registers")
        for register in sorted(emitter.touched):
            emitter.emit("li x%d, 0" % register)
    return "\n".join(emitter.lines) + "\n"


def _emit_operation(emitter: _Emitter, op) -> None:
    if isinstance(op, GateOp):
        _gate_instruction(emitter, op)
    elif isinstance(op, MeasureOp):
        emitter.load(QUBIT_REGISTERS[0], op.qubit, "qubit index %d" % op.qubit)
        emitter.emit(
            "qmeas x%d, x%d" % (FIRST_MEASUREMENT_REGISTER + op.clbit, QUBIT_REGISTERS[0]),
            "c[%d]" % op.clbit,
        )
    elif isinstance(op, ResetOp):
        emitter.load(QUBIT_REGISTERS[0], op.qubit, "qubit index %d" % op.qubit)
        emitter.emit("qreset x%d" % QUBIT_REGISTERS[0])
    elif isinstance(op, BarrierOp):
        pass
    else:
        raise HybridQasmError("LoomQ-Q cannot encode %r" % (op,))


def _lowered_ops(circuit: Circuit) -> List[List[object]]:
    """One expansion list per source operation, so segment indices still line up.

    The parser hands back gate spans as indices into ``circuit.ops``.  A single
    ``ccx`` lowers into fifteen whitelist gates, so flattening here would shift
    every later boundary; keeping the expansions grouped preserves the mapping
    without a second bookkeeping structure.
    """
    expansions = []  # type: List[List[object]]
    for op in circuit.ops:
        if isinstance(op, GateOp):
            single = circuit.copy_empty()
            single.append(op)
            expansions.append(list(lower_to_basis(single).ops))
        else:
            expansions.append([op])
    return expansions


def unified_segments(hybrid_qasm_str: str) -> Tuple[Circuit, List[Tuple[str, object]]]:
    """Expose the parsed segmentation, for tests and the CLI listing."""
    return parse_hybrid_segments(hybrid_qasm_str)


__all__ = [
    "ANGLE_REGISTER",
    "CLASSICAL_SCRATCH_TOP",
    "QUBIT_REGISTERS",
    "compile_unified",
    "unified_segments",
]
