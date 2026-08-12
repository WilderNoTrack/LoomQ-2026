"""The gate algebra: one table of definitions, one table of matrices.

Two sets matter here and they are deliberately different sizes.

*The scoring whitelist* (:data:`WHITELIST`) is the twelve ``qelib1`` gates the
competition guarantees will appear in evaluation circuits.  Everything in L1 is
sized against that list.

*The accepted set* (:data:`GATES`) is larger.  The L2 agent writes OpenQASM for
humans, and a language model asked for "a Bell state" may well emit ``cz``,
``u3`` or ``y``.  Rejecting valid qelib1 would make the agent look broken to the
user, so the front end understands the whole standard library and the passes
lower anything outside the whitelist before it reaches a backend.

Matrix convention: for a k-qubit gate the *first operand is the most significant
bit* of the matrix index, i.e. ``cx`` is ``diag-block(I, X)`` with the control
first.  :mod:`loomq.sim.statevector` maps that onto the little-endian state
index, so this convention never leaks outside the two modules.
"""

import cmath
import math
from typing import Callable, Dict, List, Sequence, Tuple

from .errors import UnsupportedGateError

Matrix = List[List[complex]]

#: The twelve gates the competition guarantees in evaluation circuits.
WHITELIST = (
    "h",
    "x",
    "s",
    "sdg",
    "t",
    "tdg",
    "rz",
    "ry",
    "cx",
    "cu1",
    "swap",
    "ccx",
)


# --------------------------------------------------------------------- helpers


def _identity(size: int) -> Matrix:
    return [[1.0 + 0j if row == col else 0j for col in range(size)] for row in range(size)]


def controlled(matrix: Matrix, num_controls: int = 1) -> Matrix:
    """Prefix ``num_controls`` control qubits onto ``matrix``.

    The controls become the most significant operands, matching the operand
    order used throughout LoomQ (``ccx a, b, c`` -> controls ``a``, ``b``).
    """
    inner = len(matrix)
    size = inner * (1 << num_controls)
    result = _identity(size)
    base = size - inner
    for row in range(inner):
        for col in range(inner):
            result[base + row][base + col] = matrix[row][col]
    return result


def dagger(matrix: Matrix) -> Matrix:
    return [
        [matrix[col][row].conjugate() for col in range(len(matrix))]
        for row in range(len(matrix))
    ]


# ------------------------------------------------------------- single qubit

_SQRT1_2 = 1.0 / math.sqrt(2.0)

I_MATRIX = _identity(2)
X_MATRIX = [[0j, 1 + 0j], [1 + 0j, 0j]]
Y_MATRIX = [[0j, -1j], [1j, 0j]]
Z_MATRIX = [[1 + 0j, 0j], [0j, -1 + 0j]]
H_MATRIX = [[_SQRT1_2 + 0j, _SQRT1_2 + 0j], [_SQRT1_2 + 0j, -_SQRT1_2 + 0j]]
S_MATRIX = [[1 + 0j, 0j], [0j, 1j]]
SDG_MATRIX = [[1 + 0j, 0j], [0j, -1j]]
T_MATRIX = [[1 + 0j, 0j], [0j, cmath.exp(1j * math.pi / 4)]]
TDG_MATRIX = [[1 + 0j, 0j], [0j, cmath.exp(-1j * math.pi / 4)]]
SX_MATRIX = [
    [(1 + 1j) / 2, (1 - 1j) / 2],
    [(1 - 1j) / 2, (1 + 1j) / 2],
]
SXDG_MATRIX = dagger(SX_MATRIX)


def u1_matrix(theta: float) -> Matrix:
    """Phase gate ``diag(1, e^{i theta})`` — qelib1 ``u1`` / OpenQASM 3 ``p``."""
    return [[1 + 0j, 0j], [0j, cmath.exp(1j * theta)]]


def u2_matrix(phi: float, lam: float) -> Matrix:
    return [
        [_SQRT1_2 + 0j, -cmath.exp(1j * lam) * _SQRT1_2],
        [cmath.exp(1j * phi) * _SQRT1_2, cmath.exp(1j * (phi + lam)) * _SQRT1_2],
    ]


def u3_matrix(theta: float, phi: float, lam: float) -> Matrix:
    cos = math.cos(theta / 2.0)
    sin = math.sin(theta / 2.0)
    return [
        [cos + 0j, -cmath.exp(1j * lam) * sin],
        [cmath.exp(1j * phi) * sin, cmath.exp(1j * (phi + lam)) * cos],
    ]


def rx_matrix(theta: float) -> Matrix:
    cos = math.cos(theta / 2.0)
    sin = math.sin(theta / 2.0)
    return [[cos + 0j, -1j * sin], [-1j * sin, cos + 0j]]


def ry_matrix(theta: float) -> Matrix:
    cos = math.cos(theta / 2.0)
    sin = math.sin(theta / 2.0)
    return [[cos + 0j, -sin + 0j], [sin + 0j, cos + 0j]]


def rz_matrix(theta: float) -> Matrix:
    """``rz`` differs from ``u1`` by the global phase ``e^{-i theta / 2}``.

    Irrelevant on its own, decisive inside a controlled decomposition — which is
    exactly why ``cu1`` lowers through ``u1`` and never through ``rz``.
    """
    return [[cmath.exp(-1j * theta / 2.0), 0j], [0j, cmath.exp(1j * theta / 2.0)]]


# ---------------------------------------------------------------- multi qubit

SWAP_MATRIX = [
    [1 + 0j, 0j, 0j, 0j],
    [0j, 0j, 1 + 0j, 0j],
    [0j, 1 + 0j, 0j, 0j],
    [0j, 0j, 0j, 1 + 0j],
]


def rzz_matrix(theta: float) -> Matrix:
    plus = cmath.exp(1j * theta / 2.0)
    minus = cmath.exp(-1j * theta / 2.0)
    return [
        [minus, 0j, 0j, 0j],
        [0j, plus, 0j, 0j],
        [0j, 0j, plus, 0j],
        [0j, 0j, 0j, minus],
    ]


class GateDef(object):
    """Everything LoomQ needs to know about one gate name."""

    __slots__ = ("name", "num_params", "num_qubits", "builder", "aliases")

    def __init__(
        self,
        name: str,
        num_params: int,
        num_qubits: int,
        builder: Callable[..., Matrix],
        aliases: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.num_params = num_params
        self.num_qubits = num_qubits
        self.builder = builder
        self.aliases = tuple(aliases)

    def matrix(self, params: Sequence[float]) -> Matrix:
        if len(params) != self.num_params:
            raise UnsupportedGateError(
                "gate %s takes %d parameter(s), got %d"
                % (self.name, self.num_params, len(params))
            )
        return self.builder(*params)

    @property
    def in_whitelist(self) -> bool:
        return self.name in WHITELIST


def _constant(matrix: Matrix) -> Callable[[], Matrix]:
    return lambda: matrix


_DEFINITIONS = (
    # ---- whitelist, single qubit
    GateDef("h", 0, 1, _constant(H_MATRIX)),
    GateDef("x", 0, 1, _constant(X_MATRIX)),
    GateDef("s", 0, 1, _constant(S_MATRIX)),
    GateDef("sdg", 0, 1, _constant(SDG_MATRIX)),
    GateDef("t", 0, 1, _constant(T_MATRIX)),
    GateDef("tdg", 0, 1, _constant(TDG_MATRIX)),
    GateDef("rz", 1, 1, rz_matrix),
    GateDef("ry", 1, 1, ry_matrix),
    # ---- whitelist, multi qubit
    GateDef("cx", 0, 2, _constant(controlled(X_MATRIX)), aliases=("CX", "cnot")),
    GateDef("cu1", 1, 2, lambda theta: controlled(u1_matrix(theta)), aliases=("cp", "cphase")),
    GateDef("swap", 0, 2, _constant(SWAP_MATRIX)),
    GateDef("ccx", 0, 3, _constant(controlled(X_MATRIX, 2)), aliases=("toffoli",)),
    # ---- rest of qelib1, accepted so the agent's output is never rejected
    GateDef("id", 0, 1, _constant(I_MATRIX), aliases=("iden", "u0")),
    GateDef("y", 0, 1, _constant(Y_MATRIX)),
    GateDef("z", 0, 1, _constant(Z_MATRIX)),
    GateDef("sx", 0, 1, _constant(SX_MATRIX)),
    GateDef("sxdg", 0, 1, _constant(SXDG_MATRIX)),
    GateDef("rx", 1, 1, rx_matrix),
    GateDef("u1", 1, 1, u1_matrix, aliases=("p", "phase")),
    GateDef("u2", 2, 1, u2_matrix),
    GateDef("u3", 3, 1, u3_matrix, aliases=("u", "U")),
    GateDef("cy", 0, 2, _constant(controlled(Y_MATRIX))),
    GateDef("cz", 0, 2, _constant(controlled(Z_MATRIX))),
    GateDef("ch", 0, 2, _constant(controlled(H_MATRIX))),
    GateDef("csx", 0, 2, _constant(controlled(SX_MATRIX))),
    GateDef("crx", 1, 2, lambda theta: controlled(rx_matrix(theta))),
    GateDef("cry", 1, 2, lambda theta: controlled(ry_matrix(theta))),
    GateDef("crz", 1, 2, lambda theta: controlled(rz_matrix(theta))),
    GateDef("cu3", 3, 2, lambda t, p, l: controlled(u3_matrix(t, p, l))),
    GateDef("rzz", 1, 2, rzz_matrix),
    GateDef("cswap", 0, 3, _constant(controlled(SWAP_MATRIX)), aliases=("fredkin",)),
)


GATES = {}  # type: Dict[str, GateDef]
_ALIASES = {}  # type: Dict[str, str]

for _definition in _DEFINITIONS:
    GATES[_definition.name] = _definition
    for _alias in _definition.aliases:
        _ALIASES[_alias] = _definition.name
del _definition


def canonical_name(name: str) -> str:
    """Map an alias (``cnot``, ``CX``, ``p``) onto its canonical gate name."""
    if name in GATES:
        return name
    if name in _ALIASES:
        return _ALIASES[name]
    lowered = name.lower()
    if lowered in GATES:
        return lowered
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    raise UnsupportedGateError(
        "unknown gate %r" % name,
        hint="LoomQ accepts the qelib1 standard library; evaluation circuits use "
        + ", ".join(WHITELIST),
    )


def lookup(name: str) -> GateDef:
    return GATES[canonical_name(name)]


def gate_matrix(name: str, params: Sequence[float] = ()) -> Matrix:
    return lookup(name).matrix(params)


def is_known(name: str) -> bool:
    try:
        canonical_name(name)
    except UnsupportedGateError:
        return False
    return True


def signature(name: str) -> Tuple[int, int]:
    """``(num_params, num_qubits)`` for a gate name."""
    definition = lookup(name)
    return definition.num_params, definition.num_qubits


__all__ = [
    "WHITELIST",
    "GATES",
    "GateDef",
    "Matrix",
    "canonical_name",
    "controlled",
    "dagger",
    "gate_matrix",
    "is_known",
    "lookup",
    "signature",
    "u1_matrix",
    "u2_matrix",
    "u3_matrix",
    "rx_matrix",
    "ry_matrix",
    "rz_matrix",
]
