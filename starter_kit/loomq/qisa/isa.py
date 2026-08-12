"""The LoomQ-Q instruction encoding.

Every quantum instruction is a standard RISC-V R-type word in the ``custom-0``
opcode space (``0b0001011``), which the base ISA reserves for exactly this:

    31       25 24    20 19    15 14    12 11     7 6        0
    |  funct7  |  rs2   |  rs1   | funct3 |   rd   |  opcode  |

``funct3`` picks the instruction class and ``funct7`` picks the operation inside
it, so the twelve whitelisted gates plus measurement, reset and sampling all fit
in one opcode with room to spare.

Operands are *register numbers*, never immediates.  A qubit index lives in a
register, so a loop can sweep a register file the same way classical RISC-V code
sweeps memory — ``li x28, 0`` / ``qh x28`` / ``addi x28, x28, 1`` is a real loop
over qubits, not an unrolled listing.

Angles are fixed point: a register holding ``k`` means ``k * pi / 4096`` radians.
That keeps the whole encoding integer-only, matching the base ISA, and 4096
steps is finer than any hardware calibration these platforms expose.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

#: RISC-V reserves this opcode for non-standard extensions.
OPCODE_CUSTOM_0 = 0x0B

#: A register value of ``k`` denotes ``k * pi / ANGLE_SCALE`` radians.
ANGLE_SCALE = 4096

# ----------------------------------------------------------------- funct3 map

FUNCT3_QSYS = 0b000   # register-file lifecycle: init, reset, sample
FUNCT3_QG1 = 0b001    # single-qubit gate, no parameter
FUNCT3_QG1P = 0b010   # single-qubit gate with an angle
FUNCT3_QG2 = 0b011    # two-qubit gate (angle in rd when parameterised)
FUNCT3_QG3 = 0b100    # three-qubit gate
FUNCT3_QMEAS = 0b101  # measure one qubit into a register


class QuantumInstruction(object):
    """One decoded LoomQ-Q instruction."""

    __slots__ = ("mnemonic", "funct3", "funct7", "rd", "rs1", "rs2")

    def __init__(
        self,
        mnemonic: str,
        funct3: int,
        funct7: int,
        rd: int = 0,
        rs1: int = 0,
        rs2: int = 0,
    ) -> None:
        self.mnemonic = mnemonic
        self.funct3 = funct3
        self.funct7 = funct7
        self.rd = rd
        self.rs1 = rs1
        self.rs2 = rs2

    def operands(self) -> List[int]:
        """Register numbers in assembly order."""
        return [getattr(self, field) for field in SPEC[self.mnemonic].operands]

    def text(self) -> str:
        fields = ", ".join("x%d" % value for value in self.operands())
        return "%s %s" % (self.mnemonic, fields) if fields else self.mnemonic

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, QuantumInstruction)
            and self.mnemonic == other.mnemonic
            and self.rd == other.rd
            and self.rs1 == other.rs1
            and self.rs2 == other.rs2
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "QuantumInstruction(%s)" % self.text()


class _Spec(object):
    __slots__ = ("mnemonic", "funct3", "funct7", "operands", "gate", "summary")

    def __init__(
        self,
        mnemonic: str,
        funct3: int,
        funct7: int,
        operands: Sequence[str],
        gate: Optional[str],
        summary: str,
    ) -> None:
        self.mnemonic = mnemonic
        self.funct3 = funct3
        self.funct7 = funct7
        self.operands = tuple(operands)
        self.gate = gate
        self.summary = summary


def _spec(mnemonic, funct3, funct7, operands, gate, summary):
    return _Spec(mnemonic, funct3, funct7, operands, gate, summary)


#: The instruction set. ``operands`` is the assembly operand order; each entry
#: names the encoding field it lands in.
SPEC = {}  # type: Dict[str, _Spec]

for _entry in (
    # --- register-file lifecycle -------------------------------------------
    _spec("qinit", FUNCT3_QSYS, 0x00, ("rs1",), None,
          "allocate x[rs1] qubits, all in |0>"),
    _spec("qreset", FUNCT3_QSYS, 0x01, ("rs1",), None,
          "collapse qubit x[rs1] to |0>"),
    _spec("qsample", FUNCT3_QSYS, 0x02, ("rd",), None,
          "measure every qubit; x[rd] receives the outcome as an integer"),
    # --- single-qubit gates -------------------------------------------------
    _spec("qh", FUNCT3_QG1, 0x00, ("rs1",), "h", "Hadamard on qubit x[rs1]"),
    _spec("qx", FUNCT3_QG1, 0x01, ("rs1",), "x", "Pauli-X on qubit x[rs1]"),
    _spec("qs", FUNCT3_QG1, 0x02, ("rs1",), "s", "S on qubit x[rs1]"),
    _spec("qsdg", FUNCT3_QG1, 0x03, ("rs1",), "sdg", "S-dagger on qubit x[rs1]"),
    _spec("qt", FUNCT3_QG1, 0x04, ("rs1",), "t", "T on qubit x[rs1]"),
    _spec("qtdg", FUNCT3_QG1, 0x05, ("rs1",), "tdg", "T-dagger on qubit x[rs1]"),
    # --- parameterised single-qubit gates -----------------------------------
    _spec("qrz", FUNCT3_QG1P, 0x00, ("rs1", "rs2"), "rz",
          "Rz(x[rs2] * pi / 4096) on qubit x[rs1]"),
    _spec("qry", FUNCT3_QG1P, 0x01, ("rs1", "rs2"), "ry",
          "Ry(x[rs2] * pi / 4096) on qubit x[rs1]"),
    # --- two-qubit gates ----------------------------------------------------
    _spec("qcx", FUNCT3_QG2, 0x00, ("rs1", "rs2"), "cx",
          "CNOT, control x[rs1], target x[rs2]"),
    _spec("qswap", FUNCT3_QG2, 0x01, ("rs1", "rs2"), "swap",
          "SWAP qubits x[rs1] and x[rs2]"),
    _spec("qcu1", FUNCT3_QG2, 0x02, ("rs1", "rs2", "rd"), "cu1",
          "CU1(x[rd] * pi / 4096), control x[rs1], target x[rs2]"),
    # --- three-qubit gates --------------------------------------------------
    _spec("qccx", FUNCT3_QG3, 0x00, ("rs1", "rs2", "rd"), "ccx",
          "Toffoli, controls x[rs1] and x[rs2], target x[rd]"),
    # --- measurement --------------------------------------------------------
    _spec("qmeas", FUNCT3_QMEAS, 0x00, ("rd", "rs1"), None,
          "measure qubit x[rs1]; x[rd] receives 0 or 1 and the state collapses"),
):
    SPEC[_entry.mnemonic] = _entry
del _entry

#: Reverse lookup used by the decoder.
_BY_CODE = {(entry.funct3, entry.funct7): entry for entry in SPEC.values()}

#: LoomQ gate name -> mnemonic, so the compiler never hard-codes opcodes.
GATE_MNEMONIC = {
    entry.gate: entry.mnemonic for entry in SPEC.values() if entry.gate
}


def _check_register(value: int, field: str, mnemonic: str) -> int:
    if not isinstance(value, int) or not 0 <= value <= 31:
        raise ValueError("%s: %s must be a register number 0..31, got %r" % (mnemonic, field, value))
    return value


def encode(mnemonic: str, *operands: int) -> int:
    """Encode one instruction into its 32-bit word."""
    key = mnemonic.strip().lower()
    entry = SPEC.get(key)
    if entry is None:
        raise ValueError("unknown LoomQ-Q instruction %r" % mnemonic)
    if len(operands) != len(entry.operands):
        raise ValueError(
            "%s takes %d operand(s), got %d" % (key, len(entry.operands), len(operands))
        )
    fields = {"rd": 0, "rs1": 0, "rs2": 0}
    for name, value in zip(entry.operands, operands):
        fields[name] = _check_register(int(value), name, key)

    return (
        (entry.funct7 & 0x7F) << 25
        | (fields["rs2"] & 0x1F) << 20
        | (fields["rs1"] & 0x1F) << 15
        | (entry.funct3 & 0x07) << 12
        | (fields["rd"] & 0x1F) << 7
        | OPCODE_CUSTOM_0
    )


def decode(word: int) -> QuantumInstruction:
    """Decode a 32-bit word into a :class:`QuantumInstruction`."""
    word &= 0xFFFFFFFF
    opcode = word & 0x7F
    if opcode != OPCODE_CUSTOM_0:
        raise ValueError(
            "0x%08X is not a LoomQ-Q instruction (opcode 0x%02X, expected 0x%02X)"
            % (word, opcode, OPCODE_CUSTOM_0)
        )
    funct3 = (word >> 12) & 0x07
    funct7 = (word >> 25) & 0x7F
    entry = _BY_CODE.get((funct3, funct7))
    if entry is None:
        raise ValueError(
            "0x%08X has no LoomQ-Q operation (funct3=0b%03b funct7=0x%02X)"
            % (word, funct3, funct7)
        )
    return QuantumInstruction(
        entry.mnemonic,
        funct3,
        funct7,
        rd=(word >> 7) & 0x1F,
        rs1=(word >> 15) & 0x1F,
        rs2=(word >> 20) & 0x1F,
    )


def is_quantum_word(word: int) -> bool:
    return (word & 0x7F) == OPCODE_CUSTOM_0


# --------------------------------------------------------------------- angles


def quantize_angle(theta: float) -> int:
    """Radians -> the fixed-point register value, wrapped into (-pi, pi]."""
    wrapped = math.fmod(theta, 2.0 * math.pi)
    if wrapped > math.pi:
        wrapped -= 2.0 * math.pi
    elif wrapped <= -math.pi:
        wrapped += 2.0 * math.pi
    return int(round(wrapped * ANGLE_SCALE / math.pi))


def radians(value: int) -> float:
    """The fixed-point register value -> radians."""
    return value * math.pi / ANGLE_SCALE


def describe_table() -> List[Tuple[str, str, str, str]]:
    """``(mnemonic, funct3, funct7, summary)`` rows, for the spec document."""
    rows = []
    for entry in sorted(SPEC.values(), key=lambda item: (item.funct3, item.funct7)):
        rows.append(
            (
                "%s %s" % (entry.mnemonic, ", ".join(entry.operands)),
                "0b" + format(entry.funct3, "03b"),
                "0x%02X" % entry.funct7,
                entry.summary,
            )
        )
    return rows


__all__ = [
    "ANGLE_SCALE",
    "GATE_MNEMONIC",
    "OPCODE_CUSTOM_0",
    "SPEC",
    "QuantumInstruction",
    "decode",
    "describe_table",
    "encode",
    "is_quantum_word",
    "quantize_angle",
    "radians",
]
