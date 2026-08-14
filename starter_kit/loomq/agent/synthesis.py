"""Deterministic circuit synthesis for the state families users actually ask for.

The division of labour in LoomQ's agent is: **the model understands, LoomQ
decides.**  A language model is excellent at turning "make me the most entangled
state you can on three qubits, then measure everything" into
``{"family": "ghz", "num_qubits": 3}``, and unreliable at emitting correct
OpenQASM for it.  So the model produces the specification and this module builds
the circuit — from the same textbook constructions every time, verified by the
reference simulator before the answer is returned.

Anything outside these families falls back to the model's own QASM, which is
then parsed, simulated and repaired if it does not run.  Both routes end in the
same verification gate.

Every circuit is normalised through ``parse -> lower -> emit`` so the answer only
ever contains the twelve whitelisted gates, whichever route produced it.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from ..circuits import ghz as _ghz, grover as _grover, qft as _qft
from ..emitters.spinq import emit_spinq
from ..errors import LoomQError
from ..passes import lower_to_basis
from ..qasm import parse_qasm
from ..sim import ideal_distribution, measurement_width
from ..sim.stabilizer import is_clifford
from ..sim.stabilizer import sample_counts as stabilizer_counts
from ..sim.statevector import MAX_QUBITS

#: Families this module can build exactly, with the aliases a model may return.
FAMILIES = {
    "bell": ("bell", "epr", "bell_state", "bell_pair", "entangled_pair"),
    "ghz": ("ghz", "greenberger", "cat", "cat_state", "max_entangled", "maximally_entangled"),
    "w": ("w", "w_state"),
    "uniform": ("uniform", "superposition", "equal_superposition", "hadamard_all", "plus_all"),
    "basis": ("basis", "computational_basis", "product_state", "bitstring"),
    "qft": ("qft", "fourier", "quantum_fourier_transform"),
    "grover": ("grover", "search", "amplitude_amplification"),
}

_BELL_VARIANTS = {
    "phi_plus": "",
    "phi_minus": "z q[0];",
    "psi_plus": "x q[1];",
    "psi_minus": "x q[1];\nz q[0];",
}


def canonical_family(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    for family, aliases in FAMILIES.items():
        if key == family or key in aliases:
            return family
    return None


def _wrap(num_qubits: int, body: List[str]) -> str:
    lines = ["OPENQASM 2.0;", 'include "qelib1.inc";']
    lines.append("qreg q[%d];" % num_qubits)
    lines.append("creg c[%d];" % num_qubits)
    lines.extend(body)
    lines.append("measure q -> c;")
    return "\n".join(lines) + "\n"


def bell(variant: str = "phi_plus") -> str:
    tail = _BELL_VARIANTS.get(str(variant).strip().lower(), "")
    body = ["h q[0];", "cx q[0], q[1];"]
    if tail:
        body.extend(tail.split("\n"))
    return _wrap(2, body)


def w_state(num_qubits: int) -> str:
    """``(|10...0> + |01...0> + ... + |0...01>) / sqrt(n)``.

    Built by the standard cascade: seed ``|10...0>``, then repeatedly split the
    amplitude one qubit further along with a controlled ``ry`` and move the
    excitation with a ``cx``.
    """
    if num_qubits < 2:
        raise LoomQError("a W state needs at least two qubits")
    body = ["x q[0];"]
    for index in range(num_qubits - 1):
        theta = 2.0 * math.acos(math.sqrt(1.0 / (num_qubits - index)))
        body.append("cry(%r) q[%d], q[%d];" % (theta, index, index + 1))
        body.append("cx q[%d], q[%d];" % (index + 1, index))
    return _wrap(num_qubits, body)


def uniform(num_qubits: int) -> str:
    return _wrap(num_qubits, ["h q[%d];" % index for index in range(num_qubits)])


def basis_state(bitstring: str) -> str:
    """``bitstring`` is written the way counts are: rightmost character is q[0]."""
    text = str(bitstring).strip()
    if not text or set(text) - {"0", "1"}:
        raise LoomQError("a computational basis state must be a binary string")
    num_qubits = len(text)
    body = [
        "x q[%d];" % qubit
        for qubit in range(num_qubits)
        if text[num_qubits - 1 - qubit] == "1"
    ]
    if not body:
        body = ["id q[0];"]
    return _wrap(num_qubits, body)


def synthesize(spec: Dict[str, Any]) -> Optional[str]:
    """Build QASM for a recognised family, or ``None`` to defer to the model."""
    family = canonical_family(spec.get("family"))
    if family is None:
        return None

    try:
        num_qubits = int(spec.get("num_qubits") or 0)
    except (TypeError, ValueError):
        num_qubits = 0

    if family == "bell":
        return bell(spec.get("variant") or "phi_plus")
    if family == "ghz":
        if num_qubits < 2:
            return None
        return _ghz(num_qubits)
    if family == "w":
        if num_qubits < 2:
            return None
        return w_state(num_qubits)
    if family == "uniform":
        if num_qubits < 1:
            return None
        return uniform(num_qubits)
    if family == "basis":
        bitstring = spec.get("basis_state") or spec.get("bitstring")
        if not bitstring and num_qubits:
            bitstring = "1" * num_qubits
        if not bitstring:
            return None
        return basis_state(str(bitstring))
    if family == "qft":
        if num_qubits < 1:
            return None
        return _qft(num_qubits, prepare_uniform=bool(spec.get("prepare_uniform", True)))
    if family == "grover":
        if not 2 <= num_qubits <= 3:
            return None
        marked = spec.get("marked_state")
        if isinstance(marked, str) and set(marked) <= {"0", "1"} and marked:
            marked_value = int(marked, 2)
        elif isinstance(marked, int):
            marked_value = marked
        else:
            marked_value = None
        return _grover(num_qubits, marked_value)
    return None


def normalize(qasm: str) -> str:
    """Reduce any accepted QASM to whitelist-only, canonically formatted QASM."""
    circuit = parse_qasm(qasm)
    return emit_spinq(lower_to_basis(circuit))


def expected_distribution(spec: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The distribution a recognised family must produce, for verification."""
    qasm = synthesize(spec)
    if qasm is None:
        return None
    circuit = parse_qasm(qasm)
    return ideal_distribution(circuit, measurement_width(circuit))


def analyse(qasm: str) -> Tuple[Dict[str, float], Dict[str, int]]:
    """``(distribution, summary)`` for a circuit — used to explain a result.

    Wide Clifford circuits fall through to the stabilizer engine, so the agent
    can still verify and describe a 40-qubit GHZ state.  Asking for one is
    routine; a statevector would need ``2^40`` amplitudes to answer.
    """
    circuit = parse_qasm(qasm)
    width = measurement_width(circuit)

    if circuit.num_qubits <= MAX_QUBITS:
        return ideal_distribution(circuit, width), circuit.summary()

    if not is_clifford(circuit):
        raise LoomQError(
            "this circuit needs %d qubits and is not Clifford, so LoomQ cannot "
            "verify it before answering" % circuit.num_qubits
        )
    shots = 2048
    counts = stabilizer_counts(circuit, shots, width, seed=0)
    distribution = {key: value / float(shots) for key, value in counts.items()}
    return distribution, circuit.summary()


__all__ = [
    "FAMILIES",
    "analyse",
    "basis_state",
    "bell",
    "canonical_family",
    "expected_distribution",
    "normalize",
    "synthesize",
    "uniform",
    "w_state",
]
