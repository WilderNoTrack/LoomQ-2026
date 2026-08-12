"""LoomQ-Q — a quantum extension to RISC-V, in the ``custom-0`` opcode space.

L3 compiles a Hybrid-QASM program into *two* artifacts that the harness has to
staple back together: a list of quantum operations, and classical assembly whose
measurement inputs are injected from outside.  LoomQ-Q closes that seam.  Quantum
operations become real instructions in the same 32-bit encoding as the classical
ones, ``qmeas`` writes its outcome straight into ``x10``, and the classical block
that reads ``x10`` is the very next instruction in the stream.

One program, one instruction stream, one program counter.

    loomq/qisa/isa.py        the encoding: fields, opcodes, encode/decode
    loomq/qisa/assembler.py  mnemonics <-> 32-bit words
    loomq/qisa/compile.py    Hybrid-QASM -> one unified stream
    ../riscv_emulator_loomq.py   the official emulator, extended

The full specification is in ``starter_kit/QISA.md``.
"""

from .assembler import assemble, disassemble
from .isa import (
    ANGLE_SCALE,
    OPCODE_CUSTOM_0,
    SPEC,
    QuantumInstruction,
    decode,
    encode,
    quantize_angle,
    radians,
)

__all__ = [
    "ANGLE_SCALE",
    "OPCODE_CUSTOM_0",
    "SPEC",
    "QuantumInstruction",
    "assemble",
    "decode",
    "disassemble",
    "encode",
    "quantize_angle",
    "radians",
]
