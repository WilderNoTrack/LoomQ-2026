"""Target-agnostic circuit rewriting.

Everything a platform "does not support" is handled here, once, against a
declared basis set — never inside an emitter and never per vendor.  Adding a
backend with an unusual gate set means passing a different ``basis``, not
writing new rewriting code.
"""

from .decompose import (
    WHITELIST_BASIS,
    lower_to_basis,
    zyz_angles,
)
from .optimize import optimize
from .peephole import peephole, savings
from .routing import (
    CouplingMap,
    DEVICE_COUPLING,
    Layout,
    TWO_QUBIT_BASIS,
    lower_for_routing,
    route,
    routed_gate_count,
)

__all__ = [
    "CouplingMap",
    "DEVICE_COUPLING",
    "Layout",
    "TWO_QUBIT_BASIS",
    "WHITELIST_BASIS",
    "lower_for_routing",
    "lower_to_basis",
    "optimize",
    "peephole",
    "route",
    "savings",
    "routed_gate_count",
    "zyz_angles",
]
