"""Lower any accepted gate onto a declared basis.

Three layers, tried in order:

1. **Named identities** — the rewrites from the starter kit's
   ``gate_identities.md`` plus the usual qelib1 ones.  Short, exact, and the
   thing a reviewer can check by eye.
2. **ZYZ Euler decomposition** for any remaining single-qubit gate:
   ``U = e^{i alpha} Rz(beta) Ry(gamma) Rz(delta)``.
3. **The ABC construction** for any remaining singly-controlled gate:
   ``CU = u1(alpha)_c . A_t . CX . B_t . CX . C_t`` with ``ABC = I``.

Layers 2 and 3 are why LoomQ does not need a rewrite rule per vendor per gate:
anything expressible as a one-qubit unitary, or a control on one, lowers
automatically.  Where a rewrite differs from the original by a *global* phase
the difference is unobservable in any measurement, and that is called out at
each site below.
"""

import cmath
import math
from typing import Callable, Dict, Iterable, List, Sequence, Set, Tuple

from ..errors import TranspileError
from ..gates import Matrix, WHITELIST, lookup
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp

#: The twelve gates every LoomQ target is contractually able to accept.
WHITELIST_BASIS = frozenset(WHITELIST)

_MAX_DEPTH = 16
_TOLERANCE = 1e-12

Rule = Callable[[Sequence[float], Sequence[int]], List[GateOp]]


def _gate(name: str, params: Sequence[float], qubits: Sequence[int]) -> GateOp:
    return GateOp(name, params, qubits)


# ------------------------------------------------------------ named rewrites


def _rule_z(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    # Z = S.S exactly.
    return [_gate("s", (), qubits), _gate("s", (), qubits)]


def _rule_y(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    # X.Z = -i.Y — a global phase, invisible to every measurement.
    return [_gate("s", (), qubits), _gate("s", (), qubits), _gate("x", (), qubits)]


def _rule_rx(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    # H.Rz(t).H = Rx(t) exactly.
    return [
        _gate("h", (), qubits),
        _gate("rz", params, qubits),
        _gate("h", (), qubits),
    ]


def _rule_u1(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    # u1(t) = e^{i t/2} Rz(t): global phase only, since this is a top-level gate.
    return [_gate("rz", params, qubits)]


def _rule_u2(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    return _rule_u3((math.pi / 2.0, params[0], params[1]), qubits)


def _rule_u3(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    theta, phi, lam = params
    return [
        _gate("rz", (lam,), qubits),
        _gate("ry", (theta,), qubits),
        _gate("rz", (phi,), qubits),
    ]


def _rule_sx(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    return _rule_rx((math.pi / 2.0,), qubits)


def _rule_sxdg(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    return _rule_rx((-math.pi / 2.0,), qubits)


def _rule_cz(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    control, target = qubits
    return [
        _gate("h", (), (target,)),
        _gate("cx", (), (control, target)),
        _gate("h", (), (target,)),
    ]


def _rule_cy(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    # Y = S.X.Sdg, so CY = (I x S) CX (I x Sdg).
    control, target = qubits
    return [
        _gate("sdg", (), (target,)),
        _gate("cx", (), (control, target)),
        _gate("s", (), (target,)),
    ]


def _rule_swap(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    a, b = qubits
    return [
        _gate("cx", (), (a, b)),
        _gate("cx", (), (b, a)),
        _gate("cx", (), (a, b)),
    ]


def _rule_rzz(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    a, b = qubits
    return [
        _gate("cx", (), (a, b)),
        _gate("rz", params, (b,)),
        _gate("cx", (), (a, b)),
    ]


def _rule_cu1(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    """qelib1's cu1 identity, with ``u1`` written as ``rz``.

    Each ``u1 -> rz`` swap contributes a scalar; the three of them multiply to
    ``e^{i theta / 4}`` overall, which is global and therefore unobservable.
    Substituting ``rz`` one-for-one *inside* a controlled block would not be
    safe — here the whole ``cu1`` is replaced at once, so it is.
    """
    (theta,) = params
    a, b = qubits
    return [
        _gate("rz", (theta / 2.0,), (a,)),
        _gate("cx", (), (a, b)),
        _gate("rz", (-theta / 2.0,), (b,)),
        _gate("cx", (), (a, b)),
        _gate("rz", (theta / 2.0,), (b,)),
    ]


def _rule_ccx(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    """The standard qelib1 Toffoli: 6 two-qubit gates and 9 single-qubit gates."""
    a, b, c = qubits
    return [
        _gate("h", (), (c,)),
        _gate("cx", (), (b, c)),
        _gate("tdg", (), (c,)),
        _gate("cx", (), (a, c)),
        _gate("t", (), (c,)),
        _gate("cx", (), (b, c)),
        _gate("tdg", (), (c,)),
        _gate("cx", (), (a, c)),
        _gate("t", (), (b,)),
        _gate("t", (), (c,)),
        _gate("h", (), (c,)),
        _gate("cx", (), (a, b)),
        _gate("t", (), (a,)),
        _gate("tdg", (), (b,)),
        _gate("cx", (), (a, b)),
    ]


def _rule_cswap(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    control, a, b = qubits
    return [
        _gate("cx", (), (b, a)),
        _gate("ccx", (), (control, a, b)),
        _gate("cx", (), (b, a)),
    ]


def _rule_sdg(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    """``S^3 = S-dagger`` exactly, since ``S^4 = I``.

    OriginIR's own parser has no ``SDAG`` token even though the competition's IR
    contract lists one, so the Origin target lowers it away and stays runnable
    on Origin's SDK as well as parseable by the evaluator.
    """
    return [_gate("s", (), qubits)] * 3


def _rule_tdg(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    """``S^3 . T = T-dagger``: ``diag(1, i^3 . e^{i pi/4}) = diag(1, e^{-i pi/4})``."""
    return [_gate("s", (), qubits)] * 3 + [_gate("t", (), qubits)]


def _rule_ry_fallback(params: Sequence[float], qubits: Sequence[int]) -> List[GateOp]:
    """``ry`` via ``rz`` for the rare backend that lacks it (gate_identities.md)."""
    return [
        _gate("sdg", (), qubits),
        _gate("h", (), qubits),
        _gate("rz", params, qubits),
        _gate("h", (), qubits),
        _gate("s", (), qubits),
    ]


_NAMED_RULES = {
    "id": lambda params, qubits: [],
    "z": _rule_z,
    "y": _rule_y,
    "rx": _rule_rx,
    "u1": _rule_u1,
    "u2": _rule_u2,
    "u3": _rule_u3,
    "sx": _rule_sx,
    "sxdg": _rule_sxdg,
    "cz": _rule_cz,
    "cy": _rule_cy,
    "swap": _rule_swap,
    "rzz": _rule_rzz,
    "cu1": _rule_cu1,
    "ccx": _rule_ccx,
    "cswap": _rule_cswap,
    "ry": _rule_ry_fallback,
    "sdg": _rule_sdg,
    "tdg": _rule_tdg,
}  # type: Dict[str, Rule]


# ------------------------------------------------------------ generic layers


def zyz_angles(matrix: Matrix) -> Tuple[float, float, float, float]:
    """``(alpha, beta, gamma, delta)`` with ``U = e^{i a} Rz(b) Ry(g) Rz(d)``."""
    determinant = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
    alpha = cmath.phase(determinant) / 2.0
    scale = cmath.exp(-1j * alpha)
    v00 = matrix[0][0] * scale
    v10 = matrix[1][0] * scale
    v11 = matrix[1][1] * scale

    gamma = 2.0 * math.atan2(abs(v10), abs(v00))
    if abs(v10) < _TOLERANCE:
        beta = 2.0 * cmath.phase(v11)
        delta = 0.0
    elif abs(v00) < _TOLERANCE:
        beta = 2.0 * cmath.phase(v10)
        delta = 0.0
    else:
        beta = cmath.phase(v11) + cmath.phase(v10)
        delta = cmath.phase(v11) - cmath.phase(v10)
    return alpha, beta, gamma, delta


def _single_qubit_generic(op: GateOp) -> List[GateOp]:
    _, beta, gamma, delta = zyz_angles(lookup(op.name).matrix(op.params))
    qubits = op.qubits
    return [
        _gate("rz", (delta,), qubits),
        _gate("ry", (gamma,), qubits),
        _gate("rz", (beta,), qubits),
    ]


def _controlled_generic(op: GateOp) -> List[GateOp]:
    """ABC construction for a gate that is one control plus one target."""
    full = lookup(op.name).matrix(op.params)
    inner = [[full[2][2], full[2][3]], [full[3][2], full[3][3]]]
    alpha, beta, gamma, delta = zyz_angles(inner)
    control, target = op.qubits
    return [
        # C = Rz((delta - beta) / 2)
        _gate("rz", ((delta - beta) / 2.0,), (target,)),
        _gate("cx", (), (control, target)),
        # B = Ry(-gamma / 2) Rz(-(delta + beta) / 2)
        _gate("rz", (-(delta + beta) / 2.0,), (target,)),
        _gate("ry", (-gamma / 2.0,), (target,)),
        _gate("cx", (), (control, target)),
        # A = Rz(beta) Ry(gamma / 2)
        _gate("ry", (gamma / 2.0,), (target,)),
        _gate("rz", (beta,), (target,)),
        # The conditional phase: u1(alpha) on the control, written as rz(alpha)
        # since the difference is again a global scalar.
        _gate("rz", (alpha,), (control,)),
    ]


def _is_controlled_single(name: str) -> bool:
    definition = lookup(name)
    if definition.num_qubits != 2:
        return False
    matrix = definition.matrix((0.0,) * definition.num_params)
    for row in range(2):
        for column in range(4):
            expected = 1.0 if row == column else 0.0
            if abs(matrix[row][column] - expected) > 1e-9:
                return False
    return True


# ---------------------------------------------------------------- entrypoint


def _expand(op: GateOp, basis: Set[str], depth: int) -> List[GateOp]:
    if op.name in basis:
        return [op]
    if depth > _MAX_DEPTH:
        raise TranspileError(
            "gate %r could not be lowered onto the basis %s"
            % (op.name, ", ".join(sorted(basis)))
        )

    rule = _NAMED_RULES.get(op.name)
    if rule is not None:
        replacement = rule(op.params, op.qubits)
    else:
        definition = lookup(op.name)
        if definition.num_qubits == 1:
            replacement = _single_qubit_generic(op)
        elif _is_controlled_single(op.name):
            replacement = _controlled_generic(op)
        else:
            raise TranspileError(
                "LoomQ has no rewrite for %r onto the basis %s"
                % (op.name, ", ".join(sorted(basis)))
            )

    lowered = []  # type: List[GateOp]
    for produced in replacement:
        lowered.extend(_expand(produced, basis, depth + 1))
    return lowered


def lower_to_basis(
    circuit: Circuit,
    basis: Iterable[str] = WHITELIST_BASIS,
    optimize: bool = True,
) -> Circuit:
    """Rewrite ``circuit`` so every gate name is in ``basis``.

    Measurements, resets, barriers and classical conditions pass through
    untouched; only the unitary content is rewritten.  The peephole pass in
    :mod:`loomq.passes.optimize` then clears the identity rotations generic
    decomposition leaves behind; it is exact, and can be turned off to inspect
    the raw expansion.
    """
    allowed = set(basis)
    if not allowed:
        raise TranspileError("the target basis is empty")

    lowered = circuit.copy_empty()
    for op in circuit.ops:
        if isinstance(op, GateOp):
            for produced in _expand(op, allowed, 0):
                lowered.append(produced)
        elif isinstance(op, ConditionalOp) and isinstance(op.body, GateOp):
            for produced in _expand(op.body, allowed, 0):
                lowered.append(ConditionalOp(op.clbits, op.value, produced))
        elif isinstance(op, (MeasureOp, ResetOp, BarrierOp, ConditionalOp)):
            lowered.append(op)
        else:  # pragma: no cover - defensive
            raise TranspileError("cannot lower operation %r" % (op,))

    if optimize:
        # Two stages: the cheap structural pass first (identity rotations,
        # adjacent same-axis merges), then the commutation-aware one that
        # cancels pairs through disjoint gates and resynthesises single-qubit
        # runs. Both are exact; the tests compare full statevectors.
        from .optimize import optimize as _structural
        from .peephole import peephole as _peephole

        return _peephole(_structural(lowered))
    return lowered


__all__ = ["WHITELIST_BASIS", "lower_to_basis", "zyz_angles"]
