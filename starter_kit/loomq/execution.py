"""The pipeline behind ``transpile()`` and ``run()``.

    QASM 2.0 -> parse -> lower to the 12-gate basis -> emit native IR
                                                    -> execute -> normalise

Every target walks the same path; only the emitter and the backend differ.

**Why results are cross-checked.** LoomQ always knows the exact distribution of
the circuit it was handed, because the reference simulator computes it in closed
form.  So after a vendor SDK returns counts, they are compared against that
distribution.  Two failure modes get caught for free:

* a *bit-order* mismatch — the vendor reports ``c[0]`` leftmost.  LoomQ detects
  it and re-keys, which is precisely the normalisation the contract asks the
  middle layer to own.
* a *translation* bug — a gate lowered wrongly for one platform.  Fidelity
  collapses well below shot noise, and LoomQ falls back to the reference result
  rather than shipping a silently wrong answer.

The acceptance band is derived from shot noise rather than hard-coded: sampling
``shots`` times from a distribution with ``d`` outcomes moves the Hellinger
fidelity by about ``sqrt((d-1)/(8*shots))`` even when everything is correct, so
the tolerance scales with the circuit's width.  Whatever happened is recorded in
``meta`` — ``executor``, ``bit_order_calibrated``, ``fallback_reason``.
"""

import math
import os
from typing import Any, Dict, Optional, Tuple

from .backends import reference_backend, resolve_policy, select_backend
from .emitters import emit, normalize_target
from .errors import BackendError
from .ir import Circuit
from .passes import lower_to_basis
from .qasm import parse_qasm
from .result import (
    build_result,
    counts_to_distribution,
    hellinger_fidelity,
    normalize_counts,
    validate_result,
)
from .sim import ideal_distribution, measurement_width
from .sim.statevector import MAX_QUBITS

#: Never accept a vendor result below this, however wide the distribution.
MIN_ACCEPTANCE = 0.975

#: Never demand more than this, however narrow the distribution.
MAX_ACCEPTANCE = 0.99

#: Multiple of the expected shot-noise Hellinger distance we tolerate.
NOISE_TOLERANCE = 2.5


def acceptance_threshold(outcomes: int, shots: int) -> float:
    """Fidelity a correct vendor result should clear, given pure shot noise."""
    if shots <= 0 or outcomes <= 1:
        return MAX_ACCEPTANCE
    expected = math.sqrt((outcomes - 1) / (8.0 * shots))
    return max(MIN_ACCEPTANCE, min(MAX_ACCEPTANCE, 1.0 - NOISE_TOLERANCE * expected))


def compile_circuit(qasm_str: str, target: str) -> Tuple[Circuit, Circuit, str]:
    """``(source, lowered, native_ir)`` for ``qasm_str`` on ``target``."""
    platform = normalize_target(target)
    source = parse_qasm(qasm_str)
    lowered = lower_to_basis(source)
    return source, lowered, emit(source, platform)


def transpile_qasm(qasm_str: str, target: str) -> str:
    """OpenQASM 2.0 -> the target's native representation."""
    return compile_circuit(qasm_str, target)[2]


def _padded(counts: Dict[str, int], width: int) -> Dict[str, int]:
    return normalize_counts(counts, width)


def _reversed_keys(counts: Dict[str, int]) -> Dict[str, int]:
    flipped = {}  # type: Dict[str, int]
    for key, value in counts.items():
        reversed_key = key[::-1]
        flipped[reversed_key] = flipped.get(reversed_key, 0) + value
    return flipped


def run_circuit(
    qasm_str: str,
    target: str,
    shots: int,
    policy: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute ``qasm_str`` on ``target`` and return the unified schema."""
    if not isinstance(shots, int) or isinstance(shots, bool) or shots <= 0:
        raise ValueError("shots must be a positive integer, got %r" % (shots,))

    platform = normalize_target(target)
    source, lowered, native_ir = compile_circuit(qasm_str, platform)
    width = measurement_width(source)

    # The reference distribution is what a vendor result gets cross-checked
    # against. Beyond the statevector limit there is no closed-form distribution
    # to compare with, so the check is skipped rather than faked — and `meta`
    # says so, since an unchecked result should not look like a checked one.
    expected = None  # type: Optional[Dict[str, float]]
    if source.num_qubits <= MAX_QUBITS:
        expected = ideal_distribution(source, width)

    backend, reason = select_backend(platform, policy)
    meta = {
        "target": platform,
        "policy": resolve_policy(policy),
        "backend_selection": reason,
        "transpiled_gates": len(lowered.gates),
        "depth": lowered.depth(),
        "qubits": source.num_qubits,
        "clbits": width,
        "native_ir_lines": len(native_ir.strip().splitlines()),
    }  # type: Dict[str, Any]

    outcome = None
    fallback_reason = None
    try:
        outcome = backend.execute(lowered, native_ir, shots)
    except Exception as exc:  # noqa: BLE001 - a vendor crash must not lose the run
        if backend.executor == "loomq-reference-simulator":
            raise
        fallback_reason = "%s: %s" % (type(exc).__name__, exc)

    if expected is None and outcome is not None:
        outcome.meta["reference_check"] = "skipped: beyond the statevector limit"

    if expected is not None and outcome is not None and backend.executor != "loomq-reference-simulator":
        counts = _padded(outcome.counts, width)
        threshold = acceptance_threshold(len(expected), shots)
        fidelity = hellinger_fidelity(counts_to_distribution(counts), expected)

        if fidelity < threshold:
            flipped = _reversed_keys(counts)
            flipped_fidelity = hellinger_fidelity(counts_to_distribution(flipped), expected)
            if flipped_fidelity >= threshold:
                counts = flipped
                fidelity = flipped_fidelity
                outcome.meta["bit_order_calibrated"] = True

        if fidelity < threshold:
            fallback_reason = (
                "%s returned fidelity %.4f against the reference distribution "
                "(threshold %.4f for %d outcomes at %d shots)"
                % (backend.backend_id, fidelity, threshold, len(expected), shots)
            )
            outcome = None
        else:
            outcome.counts = counts
            outcome.meta["reference_fidelity"] = round(fidelity, 6)
            outcome.meta["acceptance_threshold"] = round(threshold, 6)

    if outcome is None:
        backend = reference_backend(platform)
        outcome = backend.execute(lowered, native_ir, shots)
        if fallback_reason:
            outcome.meta["fallback_reason"] = fallback_reason

    counts = _padded(outcome.counts, width)
    if sum(counts.values()) != shots:  # pragma: no cover - vendor guard
        raise BackendError(
            "%s returned %d shots but %d were requested"
            % (backend.backend_id, sum(counts.values()), shots)
        )

    meta.update(outcome.meta)
    result = build_result(
        backend=backend.backend_id,
        job_id=outcome.job_id or "loomq-unknown",
        shots=shots,
        counts=counts,
        meta=meta,
    )

    valid, why = validate_result(result)
    if not valid:  # pragma: no cover - guarded by the test suite
        raise BackendError("LoomQ produced a non-conforming result: %s" % why)
    return result


def describe_environment() -> Dict[str, Any]:
    """Snapshot of what governs execution — surfaced by ``loomq doctor``."""
    from .backends import availability_report
    from .sim.sampler import resolve_mode, resolve_seed

    return {
        "executor_policy": resolve_policy(),
        "sampling": resolve_mode(),
        "seed": resolve_seed(),
        "platforms": availability_report(),
        "environment": {
            key: os.environ[key]
            for key in ("LOOMQ_EXECUTOR", "LOOMQ_SAMPLING", "LOOMQ_SEED")
            if key in os.environ
        },
    }


__all__ = [
    "acceptance_threshold",
    "compile_circuit",
    "describe_environment",
    "run_circuit",
    "transpile_qasm",
]
