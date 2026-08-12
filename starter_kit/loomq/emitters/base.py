"""Formatting helpers shared by the three emitters."""

import math
from typing import List

from ..errors import TranspileError
from ..ir import Circuit, ConditionalOp, MeasureOp

#: Recognisable multiples of pi, printed symbolically where the target's parser
#: accepts them.  Everything else falls back to a round-tripping decimal.
_PI_FRACTIONS = (1, 2, 3, 4, 6, 8, 12, 16)


def format_angle(value: float, symbolic_pi: bool = False) -> str:
    """Render an angle exactly.

    ``repr`` of a Python float is the shortest decimal that round-trips, so the
    emitted text reconstructs the same double the simulator used — no silent
    precision loss between LoomQ and the target platform.
    """
    if symbolic_pi and value != 0.0:
        for denominator in _PI_FRACTIONS:
            for sign in (1, -1):
                candidate = sign * math.pi / denominator
                if abs(value - candidate) < 1e-15:
                    prefix = "-" if sign < 0 else ""
                    if denominator == 1:
                        return "%spi" % prefix
                    return "%spi/%d" % (prefix, denominator)
    if value == int(value) and abs(value) < 1e15:
        return "%d" % int(value)
    return repr(float(value))


def format_params(params, symbolic_pi: bool = False) -> str:
    if not params:
        return ""
    return "(" + ",".join(format_angle(value, symbolic_pi) for value in params) + ")"


def reject_conditionals(circuit: Circuit, target: str) -> None:
    """Fail loudly rather than silently dropping feed-forward semantics."""
    for op in circuit.ops:
        if isinstance(op, ConditionalOp):
            raise TranspileError(
                "%s's canonical IR has no classical feed-forward, so `if (...)` "
                "cannot be transpiled to it; run this circuit on a target that "
                "supports it, or move the classical logic into L3 Hybrid-QASM" % target
            )


def measurement_lines(circuit: Circuit) -> List[MeasureOp]:
    return [op for op in circuit.ops if isinstance(op, MeasureOp)]


__all__ = ["format_angle", "format_params", "measurement_lines", "reject_conditionals"]
