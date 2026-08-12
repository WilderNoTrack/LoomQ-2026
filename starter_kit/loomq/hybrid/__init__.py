"""L3 — Hybrid-QASM x RISC-V mixed compilation.

``compile_hybrid(source)`` returns ``(quantum_operations, riscv_assembly)``:

* the quantum operations are the gate and measurement statements, in source
  order, rendered as valid OpenQASM 2.0 statements using the program's own
  register names;
* the assembly implements the ``classical { ... }`` blocks for
  ``riscv_emulator.TinyRISCVEmulator``, reading measurement values from
  ``x10, x11, ...`` and leaving results in ``x1..x9``.

:func:`verify` closes the loop locally: it runs the generated assembly through
the official emulator for *every* combination of measurement values and compares
the final register state against :mod:`loomq.hybrid.interpreter`, which is the
same check formal scoring performs.
"""

import itertools
import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

from ..emitters.spinq import qasm2_statement
from ..ir import BarrierOp, Circuit
from .ast import Program, measurement_bits
from .interpreter import register_state
from .parser import parse_hybrid
from .riscv import generate_assembly


def quantum_operations(circuit: Circuit) -> List[str]:
    """Gate and measurement statements in source order."""
    return [
        qasm2_statement(circuit, op)
        for op in circuit.ops
        if not isinstance(op, BarrierOp)
    ]


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Compile Hybrid-QASM into a quantum operation list and RISC-V assembly."""
    circuit, program = parse_hybrid(hybrid_qasm_str)
    return quantum_operations(circuit), generate_assembly(program)


# ------------------------------------------------------------- local checking


def load_emulator():
    """Import the official ``TinyRISCVEmulator`` from the starter kit."""
    try:
        from ..riscv_emulator import TinyRISCVEmulator  # type: ignore
        return TinyRISCVEmulator
    except Exception:
        pass
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if kit not in sys.path:
        sys.path.insert(0, kit)
    from riscv_emulator import TinyRISCVEmulator  # type: ignore

    return TinyRISCVEmulator


def _injections(bits: Iterable[int]) -> List[Dict[int, int]]:
    indices = sorted(bits)
    if not indices:
        return [{}]
    return [
        dict(zip(indices, values))
        for values in itertools.product((0, 1), repeat=len(indices))
    ]


def verify(hybrid_qasm_str: str, bits: Optional[Iterable[int]] = None) -> List[Dict[str, object]]:
    """Exhaustively compare generated assembly against the reference semantics.

    Returns one record per measurement injection with the expected and actual
    register states, so a failure points straight at the offending combination.
    """
    circuit, program = parse_hybrid(hybrid_qasm_str)
    assembly = generate_assembly(program)
    emulator_class = load_emulator()
    indices = list(bits) if bits is not None else measurement_bits(program)

    records = []  # type: List[Dict[str, object]]
    for injection in _injections(indices):
        expected = register_state(program, injection)
        emulator = emulator_class()
        emulator.load_program(assembly)
        for index, value in injection.items():
            emulator.set_register("x%d" % (10 + index), value)
        actual = emulator.execute()
        records.append(
            {
                "injection": dict(injection),
                "expected": expected,
                "actual": dict(actual),
                "match": dict(actual) == expected,
            }
        )
    return records


def verify_all(hybrid_qasm_str: str) -> Tuple[bool, List[Dict[str, object]]]:
    records = verify(hybrid_qasm_str)
    return all(record["match"] for record in records), records


__all__ = [
    "Program",
    "compile_hybrid",
    "generate_assembly",
    "load_emulator",
    "parse_hybrid",
    "quantum_operations",
    "verify",
    "verify_all",
]
