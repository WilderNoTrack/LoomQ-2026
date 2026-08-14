#!/usr/bin/env python3
"""Run one OpenQASM 2.0 program on spinqit and print counts as JSON.

This is executed as a *separate process*, by a different interpreter than the
rest of LoomQ. spinqit pins ``antlr4-python3-runtime==4.9.2`` while
amazon-braket's simulator pins ``==4.13.2``, so the two cannot share an
environment — ``pip install`` of both resolves to nothing at all. The official
image therefore puts spinqit in its own virtualenv, and without this bridge
``run(qasm, "spinq")`` would quietly fall back to LoomQ's reference simulator
inside the very container that was built to run it on spinqit.

Contract: stdin is the QASM text, argv is the shot count, stdout is a single
JSON object. Errors go to stderr with a non-zero exit, so the caller can report
the reason rather than a silent fallback.

    /opt/spinq/bin/python -m loomq.backends.spinq_runner 1024 < circuit.qasm
"""

import json
import os
import sys
import tempfile


def main(argv):
    shots = int(argv[1]) if len(argv) > 1 else 1024
    qasm = sys.stdin.read()
    if not qasm.strip():
        print("no QASM on stdin", file=sys.stderr)
        return 2

    try:
        import spinqit
    except Exception as exc:  # noqa: BLE001 - report, do not traceback
        print("spinqit is not importable here: %s" % exc, file=sys.stderr)
        return 3

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".qasm", delete=False, encoding="utf-8"
    )
    try:
        handle.write(qasm)
        handle.close()
        program = spinqit.get_compiler("qasm").compile(handle.name, 0)
    except Exception as exc:
        print("spinqit could not compile the artifact: %s" % exc, file=sys.stderr)
        return 4
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    try:
        engine = spinqit.get_basic_simulator()
        config = spinqit.BasicSimulatorConfig()
        config.configure_shots(shots)
        outcome = engine.execute(program, config)
        counts = {str(key): int(value) for key, value in dict(outcome.counts).items()}
    except Exception as exc:
        print("spinqit failed to execute the circuit: %s" % exc, file=sys.stderr)
        return 5

    json.dump(
        {
            "counts": counts,
            "qubits": getattr(program, "qnum", None),
            "spinqit_version": getattr(spinqit, "__version__", None),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
