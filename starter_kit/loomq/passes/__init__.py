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

__all__ = ["WHITELIST_BASIS", "lower_to_basis", "optimize", "zyz_angles"]
