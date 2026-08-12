"""LoomQ — a universal middle layer that lets anyone drive real quantum hardware.

The package is deliberately layered so that every stage of the pipeline can be
tested, swapped and explained on its own:

    qasm/       OpenQASM 2.0 front end  -> :class:`loomq.ir.Circuit`
    gates.py    the gate algebra (matrices + qelib1 identities)
    passes/     target-agnostic rewriting (decomposition, validation)
    emitters/   Circuit -> native target IR (QASM2 / QASM3 / OriginIR)
    sim/        an exact statevector reference simulator
    backends/   Circuit -> a real vendor SDK, with a self-validating fallback
    result.py   the one JSON schema every backend is normalised into

Nothing above imports a third-party package; the vendor SDKs are loaded lazily
inside :mod:`loomq.backends` so the core stays runnable in a bare interpreter.
"""

from .version import __version__, CONTRACT_VERSION

__all__ = ["__version__", "CONTRACT_VERSION"]
