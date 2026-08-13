#!/usr/bin/env python3
"""Score the L2 agent against a real model service, the way the judges will.

Formal L2 uses undisclosed *variants* of three published task shapes — reworded,
different qubit counts, different target states — so testing against the three
published prompts proves very little.  This file is sixteen variants written in
that spirit, each with a machine-checkable expectation:

* circuit tasks   — the reply is run through the official ``extract_qasm``
  regex from ``evaluator.py``, parsed, simulated, and compared against the
  distribution the request asked for (Hellinger fidelity >= 0.97);
* backend tasks   — the reply must contain a canonical backend id from the
  official capability table, and must not contain one that fails a constraint.

Usage::

    cp secrets.env.example secrets.env     # then paste your key into it
    python3 tools/l2_live_check.py

The credential is read from ``secrets.env`` and never printed. Add ``--verbose``
to see the full replies (they contain no secrets either).
"""

import argparse
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
for _path in (_KIT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import loomq_env  # noqa: E402

from loomq.agent import respond  # noqa: E402
from loomq.agent.llm import REQUIRED_ENV  # noqa: E402
from loomq.qasm import parse_qasm  # noqa: E402
from loomq.result import hellinger_fidelity  # noqa: E402
from loomq.sim import ideal_distribution, measurement_width  # noqa: E402

#: The extractor from starter_kit/evaluator.py, copied verbatim.
EXTRACT = re.compile(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", re.DOTALL | re.MULTILINE)

PASS_FIDELITY = 0.97


# ------------------------------------------------------------------ expected


def uniform(num_qubits: int) -> Dict[str, float]:
    size = 1 << num_qubits
    return {format(index, "0%db" % num_qubits): 1.0 / size for index in range(size)}


def ghz_distribution(num_qubits: int) -> Dict[str, float]:
    return {"0" * num_qubits: 0.5, "1" * num_qubits: 0.5}


def w_distribution(num_qubits: int) -> Dict[str, float]:
    return {
        "".join("1" if bit == index else "0" for bit in range(num_qubits - 1, -1, -1)): 1.0
        / num_qubits
        for index in range(num_qubits)
    }


def exact(bitstring: str) -> Dict[str, float]:
    return {bitstring: 1.0}


# --------------------------------------------------------------------- cases

CIRCUIT_CASES = [
    # (label, prompt, expected distribution)
    ("gen/ghz3-published",
     "生成一个 3 比特的最大纠缠态 (GHZ 态)，并进行全测量",
     ghz_distribution(3)),
    ("gen/ghz4-reworded",
     "帮我做一个四个量子比特的 GHZ 态，然后把每一个都测量掉",
     ghz_distribution(4)),
    ("gen/bell-colloquial",
     "我想要一对纠缠在一起的量子比特，就是那种一个是 0 另一个必定也是 0 的",
     {"00": 0.5, "11": 0.5}),
    ("gen/ghz5-english",
     "Prepare a 5-qubit cat state and measure every qubit.",
     ghz_distribution(5)),
    ("gen/w3",
     "给我一个 3 比特的 W 态并全部测量",
     w_distribution(3)),
    ("gen/basis-101",
     "准备 |101> 这个状态然后测量",
     exact("101")),
    ("gen/uniform4",
     "把 4 个量子比特都变成等概率的叠加态，然后测量",
     uniform(4)),
    ("gen/basis-1101-english",
     "Give me a circuit that prepares the computational basis state |1101> "
     "and measures all four qubits.",
     exact("1101")),

    ("fix/bell-published",
     "我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]",
     {"00": 0.5, "11": 0.5}),
    ("fix/ghz3-missing-header",
     "下面这段本来想做一个 3 比特 GHZ 态，但是跑不起来，帮我修好：\n"
     "qreg q[3];\nh q[0];\ncx q[0],q[1];\ncx q[1],q[2];",
     ghz_distribution(3)),
    ("fix/no-measure",
     "This is supposed to prepare |11> on two qubits but I get nothing back. Fix it:\n"
     "OPENQASM 2.0;\nqreg q[2];\nx q[0];\nx q[1];",
     exact("11")),
    ("fix/wrong-qubit",
     "我想让两个比特都进入均匀叠加，但我写成了 h q[0]; h q[0]; 帮我改对，两个比特都要测量",
     uniform(2)),
]

BACKEND_CASES = [
    # (label, prompt, ids that count as correct, ids that must not appear)
    ("pick/15q-no-queue-published",
     "我需要运行一个 15 比特电路，且零排队等待，选哪个平台？",
     {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"},
     {"spinq_cloud_qpu", "braket_cloud"}),
    ("pick/hardware-free-published",
     "我想在真实量子硬件上跑一个 5 比特电路，但不想花钱，选哪个？",
     {"spinq_cloud_qpu", "originq_wukong"},
     {"braket_cloud"}),
    ("pick/26q-no-queue",
     "跑一个 26 比特的电路，不能排队等，用哪个后端？",
     {"originq_local_simulator"},
     {"braket_local_simulator", "spinq_taurus_simulator", "braket_cloud"}),
    ("pick/no-account-english",
     "I have a 20-qubit circuit, I need the results right now and I do not want "
     "to register for anything. Which backend?",
     {"originq_local_simulator", "braket_local_simulator", "spinq_taurus_simulator"},
     {"spinq_cloud_qpu", "originq_wukong", "braket_cloud"}),
]


# -------------------------------------------------------------------- runner


def check_circuit(text: str, expected: Dict[str, float]) -> Tuple[bool, str]:
    match = EXTRACT.search(text)
    if not match:
        return False, "the official extractor found no OpenQASM 2.0 program"
    try:
        circuit = parse_qasm(match.group(0))
    except Exception as exc:
        return False, "extracted program does not parse: %s" % exc
    width = len(next(iter(expected)))
    if circuit.num_qubits != width:
        return False, "circuit has %d qubits, the request asked for %d" % (
            circuit.num_qubits, width
        )
    try:
        observed = ideal_distribution(circuit, measurement_width(circuit))
    except Exception as exc:
        return False, "extracted program does not simulate: %s" % exc
    fidelity = hellinger_fidelity(observed, expected)
    if fidelity >= PASS_FIDELITY:
        return True, "fidelity %.4f" % fidelity
    top = sorted(observed.items(), key=lambda item: -item[1])[:4]
    return False, "fidelity %.4f; got %s" % (
        fidelity, ", ".join("%s=%.2f" % pair for pair in top)
    )


def check_backend(text: str, correct: set, forbidden: set) -> Tuple[bool, str]:
    hit = sorted(name for name in correct if name in text)
    bad = sorted(name for name in forbidden if name in text)
    if not hit:
        return False, "no correct backend id in the reply"
    if bad:
        return False, "also recommended %s" % ", ".join(bad)
    return True, "named " + ", ".join(hit)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verbose", action="store_true", help="print every reply")
    parser.add_argument("--only", help="run cases whose label contains this string")
    parser.add_argument("--secrets", help="path to the credentials file")
    args = parser.parse_args(argv)

    loomq_env.load(args.secrets)
    loomq_env.print_report("L2 model service configuration:", REQUIRED_ENV)
    missing = loomq_env.require(REQUIRED_ENV)
    if missing:
        print(
            "\nMissing: %s\n"
            "Copy starter_kit/secrets.env.example to starter_kit/secrets.env and "
            "fill in your key, then run this again." % ", ".join(missing)
        )
        return 2
    print()

    cases = []  # type: List[Tuple[str, str, Callable[[str], Tuple[bool, str]]]]
    for label, prompt, expected in CIRCUIT_CASES:
        cases.append((label, prompt, lambda text, e=expected: check_circuit(text, e)))
    for label, prompt, correct, forbidden in BACKEND_CASES:
        cases.append(
            (label, prompt, lambda text, c=correct, f=forbidden: check_backend(text, c, f))
        )
    if args.only:
        cases = [case for case in cases if args.only in case[0]]

    passed = 0
    failures = []  # type: List[Tuple[str, str]]
    header = "%-28s %-6s %-7s %s" % ("case", "result", "seconds", "detail")
    print(header)
    print("-" * 96)

    for label, prompt, checker in cases:
        started = time.time()
        try:
            result = respond(prompt)
            text = result.text
            calls = result.trace.get("successful_model_calls", 0)
            ok, detail = checker(text)
            if not calls:
                ok, detail = False, "no successful model call (case would not score)"
        except Exception as exc:
            text, ok, detail = "", False, "%s: %s" % (type(exc).__name__, exc)
        elapsed = time.time() - started
        passed += 1 if ok else 0
        if not ok:
            failures.append((label, detail))
        print("%-28s %-6s %-7.1f %s" % (label, "PASS" if ok else "FAIL", elapsed, detail))
        if args.verbose or not ok:
            print("    prompt: %s" % prompt.replace("\n", " / ")[:110])
            if text:
                for line in text.splitlines()[:24]:
                    print("    | " + line)
            print()

    total = len(cases)
    print("\n%d/%d passed (%.0f%%)" % (passed, total, 100.0 * passed / max(total, 1)))
    if failures:
        print("\nfailures:")
        for label, detail in failures:
            print("  %-28s %s" % (label, detail))
    print(
        "\nFormal L2 scores 12 cases at 20 points; this is a proxy, not the score.\n"
        "A case only counts if the submission made a successful model call, which "
        "is checked above."
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
