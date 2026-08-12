"""Circuit -> native target IR.

One printer per platform, all fed by the same lowered circuit.  An emitter never
decides *what* gates to use — :mod:`loomq.passes` already did that — it only
decides how to spell them.  That split is what keeps "add a platform" a
50-line job.
"""

from typing import Callable, Dict, Tuple

from ..errors import UnknownTargetError
from ..ir import Circuit
from .braket import emit_braket
from .originq import emit_originq
from .spinq import emit_spinq

#: Canonical target names accepted by ``transpile()`` and ``run()``.
TARGETS = ("spinq", "originq", "braket")

_EMITTERS = {
    "spinq": emit_spinq,
    "originq": emit_originq,
    "braket": emit_braket,
}  # type: Dict[str, Callable[[Circuit], str]]

#: Tolerated spellings, so a user typing "SpinQ" or "aws" is not punished.
_TARGET_ALIASES = {
    "spinq": "spinq",
    "spinqit": "spinq",
    "taurus": "spinq",
    "originq": "originq",
    "origin": "originq",
    "originir": "originq",
    "pyqpanda": "originq",
    "qpanda": "originq",
    "wukong": "originq",
    "braket": "braket",
    "aws": "braket",
    "aws_braket": "braket",
    "awsbraket": "braket",
}


def normalize_target(target: str) -> str:
    """Map a user-supplied target name onto one of :data:`TARGETS`."""
    if not isinstance(target, str):
        raise UnknownTargetError(
            "target must be one of %s, got %s" % (", ".join(TARGETS), type(target).__name__)
        )
    key = target.strip().lower().replace("-", "_").replace(" ", "_")
    if key in _TARGET_ALIASES:
        return _TARGET_ALIASES[key]
    raise UnknownTargetError(
        "unknown target %r; expected one of %s" % (target, ", ".join(TARGETS))
    )


def emit(circuit: Circuit, target: str) -> str:
    """Serialise ``circuit`` into ``target``'s native representation."""
    return _EMITTERS[normalize_target(target)](circuit)


__all__ = ["TARGETS", "emit", "emit_braket", "emit_originq", "emit_spinq", "normalize_target"]
