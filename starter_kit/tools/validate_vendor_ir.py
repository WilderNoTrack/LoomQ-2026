#!/usr/bin/env python3
"""Feed LoomQ's emitted IR to each vendor's own parser and compare distributions.

``tests/test_l1_pipeline.py`` proves LoomQ can read back what LoomQ wrote.  That
is necessary but circular: a shared misunderstanding of a vendor's dialect would
pass it.  This script closes the loop with independent implementations —

    spinq    -> spinqit's OpenQASM 2.0 compiler + BasicSimulator
    originq  -> pyqpanda's OriginIR importer + CPUQVM
    braket   -> amazon-braket-sdk's OpenQASM 3 parser + LocalSimulator

Each vendor executes the exact string ``transpile()`` returned, and the sampled
counts are compared against LoomQ's exact distribution.  If a gate is spelled in
a way a vendor does not accept, this fails loudly instead of silently costing
points on evaluation day.

Requires the optional SDKs::

    pip install -r requirements-backends.txt
    python3 tools/validate_vendor_ir.py --shots 8192

Vendors that are not installed are reported as skipped, not as failures.
"""

import argparse
import os
import sys
import tempfile
import traceback
from typing import Callable, Dict, Optional, Tuple

_KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _KIT not in sys.path:
    sys.path.insert(0, _KIT)

from loomq.circuits import official_suite, whitelist_exercise  # noqa: E402
from loomq.execution import transpile_qasm  # noqa: E402
from loomq.qasm import parse_qasm  # noqa: E402
from loomq.result import counts_to_distribution, hellinger_fidelity, normalize_counts  # noqa: E402
from loomq.sim import ideal_distribution, measurement_width  # noqa: E402

THRESHOLD = 0.97


def _import(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:
        return None


# --------------------------------------------------------------------- spinq


def run_spinq(native_ir: str, shots: int, width: int) -> Dict[str, int]:
    spinqit = _import("spinqit")
    if spinqit is None:
        raise RuntimeError("spinqit is not installed")

    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".qasm", delete=False, encoding="utf-8")
    try:
        handle.write(native_ir)
        handle.close()
        program = spinqit.get_compiler("qasm").compile(handle.name, 0)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    engine = spinqit.get_basic_simulator()
    config = spinqit.BasicSimulatorConfig()
    config.configure_shots(shots)
    raw = dict(engine.execute(program, config).counts)
    # spinqit puts c[0] on the left; the contract puts it on the right. Invisible
    # on Bell and GHZ (their outcomes are palindromes), decisive everywhere else.
    return {key[::-1]: value for key, value in raw.items()}


# ------------------------------------------------------------------- originq


def run_originq(native_ir: str, shots: int, width: int) -> Dict[str, int]:
    """Import the OriginIR text itself, not a QASM round-trip."""
    pq = _import("pyqpanda") or _import("pyqpanda3")
    if pq is None:
        raise RuntimeError("pyqpanda is not installed")

    machine = pq.CPUQVM()
    machine.init_qvm()
    try:
        converter = None
        for name in (
            "convert_originir_str_to_qprog",
            "convert_originir_string_to_qprog",
            "originir_to_qprog",
        ):
            if hasattr(pq, name):
                converter = getattr(pq, name)
                break
        if converter is None:
            raise RuntimeError("this pyqpanda build has no OriginIR importer")

        converted = converter(native_ir, machine)
        program = converted[0] if isinstance(converted, (tuple, list)) else converted
        cregs = machine.get_allocate_cbits()
        return dict(machine.run_with_configuration(program, cregs, shots))
    finally:
        try:
            machine.finalize()
        except Exception:
            pass


# -------------------------------------------------------------------- braket


def run_braket(native_ir: str, shots: int, width: int) -> Dict[str, int]:
    """Hand the OpenQASM 3 text straight to Braket's own parser.

    One edit is made first: ``include "stdgates.inc";`` is dropped.  The
    competition's IR contract shows that include, and the emitted artifact keeps
    it — but Braket's parser resolves includes against the *filesystem* and
    ships no copy of the file, so leaving it in fails before a single gate is
    read.  Removing it leaves the gate names themselves under test, which is the
    thing worth testing.
    """
    devices = _import("braket.devices")
    openqasm = _import("braket.ir.openqasm")
    if devices is None or openqasm is None:
        raise RuntimeError("amazon-braket-sdk is not installed")

    source = "\n".join(
        line for line in native_ir.splitlines() if "stdgates.inc" not in line
    )
    task = devices.LocalSimulator().run(openqasm.Program(source=source), shots=shots)
    result = task.result()
    raw = dict(result.measurement_counts)
    # Braket keys are ordered by measured_qubits; our IR measures q[i] -> c[i],
    # so position i is qubit i and the string needs reversing to put c[0] last.
    return {key[::-1]: value for key, value in raw.items()}


def run_braket_sdk(native_ir: str, shots: int, width: int) -> Dict[str, int]:
    """The path the LoomQ backend actually uses: the SDK's Circuit object."""
    from loomq.backends.braket_backend import BraketBackend  # noqa: E402

    from loomq.passes import lower_to_basis  # noqa: E402
    from loomq.qasm import parse_qasm  # noqa: E402

    circuit = lower_to_basis(parse_qasm(run_braket_sdk.source))  # type: ignore[attr-defined]
    return BraketBackend().execute(circuit, native_ir, shots).counts


RUNNERS = {
    "spinq": run_spinq,
    "originq": run_originq,
    "braket": run_braket,
    "braket-sdk": run_braket_sdk,
}  # type: Dict[str, Callable[[str, int, int], Dict[str, int]]]


# ---------------------------------------------------------------------- main


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--shots", type=int, default=8192)
    parser.add_argument("--targets", default="spinq,originq,braket")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    targets = [name.strip() for name in args.targets.split(",") if name.strip()]
    suite = official_suite() + [("whitelist", whitelist_exercise())]

    available = {}
    for target in targets:
        try:
            RUNNERS[target]("", 1, 1)
        except RuntimeError as exc:
            if "not installed" in str(exc) or "no OriginIR importer" in str(exc):
                available[target] = str(exc)
                continue
            available[target] = None
        except Exception:
            available[target] = None

    print("LoomQ vendor IR validation — %d shots" % args.shots)
    for target in targets:
        note = available.get(target)
        print("  %-9s %s" % (target, note or "available"))
    print()

    header = "%-12s %-9s %-10s %s" % ("circuit", "vendor", "fidelity", "verdict")
    print(header)
    print("-" * len(header))

    failures = 0
    skipped = 0
    for name, qasm in suite:
        circuit = parse_qasm(qasm)
        width = measurement_width(circuit)
        expected = ideal_distribution(circuit, width)
        for target in targets:
            if available.get(target):
                skipped += 1
                print("%-12s %-9s %-10s skipped" % (name, target, "-"))
                continue
            native = transpile_qasm(qasm, target.split("-")[0])
            run_braket_sdk.source = qasm  # type: ignore[attr-defined]
            try:
                raw = RUNNERS[target](native, args.shots, width)
                counts = normalize_counts(raw, width)
                fidelity = hellinger_fidelity(counts_to_distribution(counts), expected)
            except Exception as exc:
                failures += 1
                print("%-12s %-9s %-10s ERROR %s: %s"
                      % (name, target, "-", type(exc).__name__, exc))
                if args.verbose:
                    traceback.print_exc()
                    print(native)
                continue
            verdict = "ok" if fidelity >= THRESHOLD else "FAIL"
            if verdict == "FAIL":
                failures += 1
                if args.verbose:
                    print(native)
                    print("  vendor:", counts)
                    print("  ideal :", expected)
            print("%-12s %-9s %-10.6f %s" % (name, target, fidelity, verdict))

    print()
    if failures:
        print("%d check(s) FAILED" % failures)
    else:
        print("every installed vendor accepted LoomQ's IR and reproduced the "
              "reference distribution (%d skipped)" % skipped)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
