"""Read LoomQ's own output back and check it still means the same thing.

Formal L1 scoring does not stop at ``run()``'s counts: the organisers *parse and
simulate the artifact ``transpile()`` returned*.  So LoomQ ships the other half
of that loop — importers for OpenQASM 3 and OriginIR — and the test suite runs
every circuit through

    source QASM 2.0 -> emit -> re-import -> simulate -> compare distributions

for all three targets.  A gate spelled wrongly for one platform, a swapped
operand order, a dropped measurement: all of it surfaces locally instead of on
the scoreboard.

These importers exist to verify LoomQ's own emitters, not to be general-purpose
parsers for arbitrary vendor code.
"""

import re
from typing import Dict, List, Tuple

from .errors import QasmError
from .ir import Circuit, GateOp, MeasureOp
from .qasm import parse_qasm
from .result import hellinger_fidelity
from .sim import ideal_distribution, measurement_width

# ------------------------------------------------------------------ OpenQASM 3

_QASM3_GATES = {
    "cp": "cu1",
    "cphase": "cu1",
    "cphaseshift": "cu1",
    "cnot": "cx",
    "ccnot": "ccx",
    "phaseshift": "u1",
    "p": "u1",
    "si": "sdg",
    "ti": "tdg",
}


def qasm3_to_qasm2(source: str) -> str:
    """Rewrite the OpenQASM 3 subset LoomQ emits into equivalent OpenQASM 2."""
    text = source
    text = re.sub(r"OPENQASM\s+3(\.\d+)?\s*;", "OPENQASM 2.0;", text)
    text = re.sub(r'include\s+"stdgates\.inc"\s*;', 'include "qelib1.inc";', text)
    text = re.sub(r"\bqubit\[(\d+)\]\s+([A-Za-z_]\w*)\s*;", r"qreg \2[\1];", text)
    text = re.sub(r"\bbit\[(\d+)\]\s+([A-Za-z_]\w*)\s*;", r"creg \2[\1];", text)
    # c[0] = measure q[0];  /  c = measure q;
    text = re.sub(
        r"([A-Za-z_]\w*(?:\[\d+\])?)\s*=\s*measure\s+([A-Za-z_]\w*(?:\[\d+\])?)\s*;",
        r"measure \2 -> \1;",
        text,
    )
    text = re.sub(r"if\s*\(([^)]*)\)\s*\{([^}]*)\}", r"if (\1) \2", text)
    for source_name, target_name in _QASM3_GATES.items():
        text = re.sub(r"(?m)^(\s*)%s\b" % source_name, r"\1%s" % target_name, text)
    return text


def parse_qasm3(source: str) -> Circuit:
    return parse_qasm(qasm3_to_qasm2(source))


# -------------------------------------------------------------------- OriginIR

_ORIGINIR_GATES = {
    "H": "h",
    "X": "x",
    "Y": "y",
    "Z": "z",
    "S": "s",
    "SDAG": "sdg",
    "T": "t",
    "TDAG": "tdg",
    "RX": "rx",
    "RY": "ry",
    "RZ": "rz",
    "U1": "u1",
    "CNOT": "cx",
    "CZ": "cz",
    "CU1": "cu1",
    "CR": "cu1",
    "SWAP": "swap",
    "TOFFOLI": "ccx",
    "CCX": "ccx",
}

_BIT = re.compile(r"([qc])\s*\[\s*(\d+)\s*\]", re.IGNORECASE)
_LEADING_PARAMS = re.compile(r"^([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(.*)$")
_TRAILING_PARAMS = re.compile(r"^(.*?),?\s*\(([^)]*)\)\s*$")


def parse_originir(source: str) -> Circuit:
    """Import the canonical OriginIR subset from ``target_ir_contract.md``."""
    circuit = Circuit()
    qubits = 0
    clbits = 0

    for number, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0].split("//", 1)[0].strip()
        if not line:
            continue

        head = line.split()[0].upper()
        if head == "QINIT":
            qubits = int(line.split()[1])
            circuit.add_qreg("q", qubits)
            continue
        if head == "CREG":
            clbits = int(line.split()[1])
            circuit.add_creg("c", clbits)
            continue
        if not qubits:
            raise QasmError("OriginIR program uses q[...] before QINIT", line=number)

        if head == "MEASURE":
            bits = _BIT.findall(line)
            if len(bits) != 2:
                raise QasmError("malformed MEASURE: %s" % line, line=number)
            circuit.append(MeasureOp(int(bits[0][1]), int(bits[1][1])))
            continue

        params = []  # type: List[float]
        body = line
        match = _LEADING_PARAMS.match(line)
        if match:
            body = "%s %s" % (match.group(1), match.group(3))
            params = [float(value) for value in match.group(2).split(",") if value.strip()]
        else:
            match = _TRAILING_PARAMS.match(line)
            if match and "(" in line:
                body = match.group(1)
                params = [float(value) for value in match.group(2).split(",") if value.strip()]

        mnemonic = body.split()[0].upper()
        name = _ORIGINIR_GATES.get(mnemonic)
        if name is None:
            raise QasmError("unknown OriginIR mnemonic %r" % mnemonic, line=number)
        operands = [int(index) for kind, index in _BIT.findall(body) if kind.lower() == "q"]
        circuit.append(GateOp(name, params, operands))

    if not qubits:
        raise QasmError("OriginIR program has no QINIT")
    if not clbits:
        circuit.add_creg("c", qubits)
    return circuit


# ------------------------------------------------------------------ comparison

_IMPORTERS = {
    "spinq": parse_qasm,
    "braket": parse_qasm3,
    "originq": parse_originir,
}


def reimport(native_ir: str, target: str) -> Circuit:
    """Parse a native IR artifact back into a circuit."""
    return _IMPORTERS[target](native_ir)


def verify_target_ir(source_qasm: str, target: str, native_ir: str) -> Tuple[bool, float, str]:
    """``(equivalent, fidelity, detail)`` for one emitted artifact.

    The comparison is between exact distributions, not samples, so the only
    thing that can move the number is a genuine semantic difference.
    """
    source = parse_qasm(source_qasm)
    width = measurement_width(source)
    expected = ideal_distribution(source, width)

    try:
        roundtrip = reimport(native_ir, target)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the harness
        return False, 0.0, "%s: %s" % (type(exc).__name__, exc)

    observed = ideal_distribution(roundtrip, width)
    fidelity = hellinger_fidelity(observed, expected)
    if fidelity >= 1.0 - 1e-9:
        return True, fidelity, "distributions match exactly"
    return fidelity >= 0.999, fidelity, "fidelity %.9f against the source circuit" % fidelity


def distribution_report(source_qasm: str) -> Dict[str, float]:
    circuit = parse_qasm(source_qasm)
    return ideal_distribution(circuit, measurement_width(circuit))


__all__ = [
    "distribution_report",
    "parse_originir",
    "parse_qasm3",
    "qasm3_to_qasm2",
    "reimport",
    "verify_target_ir",
]
