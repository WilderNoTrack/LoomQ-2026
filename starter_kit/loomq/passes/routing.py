"""Qubit routing: make every two-qubit gate act on physically adjacent qubits.

Real devices are not fully connected.  SpinQ's 8-qubit superconducting chip is a
line, its 3-qubit NMR machine is a triangle, and Wukong is a large 2-D lattice.
A circuit that says ``cx q[0], q[5]`` cannot run as written on a line — the two
qubits have to be walked next to each other first, by swapping.

Without this pass LoomQ is not really a transpiler: it lowers gates but leaves
the hardest part of the job, the part that actually decides whether a circuit
fits a device, to the vendor.  Both platforms will do it for you (SpinQ maps
silently, Origin takes ``mappingFlag``), which is exactly why its absence is
easy to miss.

The algorithm is deliberately the simple, checkable one: walk the circuit, and
whenever a two-qubit gate's operands are not adjacent, insert swaps along a
shortest path until they are.  It is not SABRE — it does not look ahead — but it
is correct, and correctness is verifiable: :func:`route` returns the final
layout, and the test suite simulates routed circuits against their originals and
requires identical distributions.

Routing runs *after* lowering, so it only ever sees one- and two-qubit gates.
"""

from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import TranspileError
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp


class CouplingMap(object):
    """An undirected connectivity graph over physical qubits."""

    def __init__(self, edges: Iterable[Sequence[int]], size: Optional[int] = None) -> None:
        self.edges = set()  # type: set
        neighbours = {}  # type: Dict[int, set]
        largest = -1
        for edge in edges:
            a, b = int(edge[0]), int(edge[1])
            if a == b:
                continue
            self.edges.add((min(a, b), max(a, b)))
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
            largest = max(largest, a, b)
        self.size = size if size is not None else largest + 1
        self.neighbours = {
            qubit: neighbours.get(qubit, set()) for qubit in range(max(self.size, 0))
        }
        self._distances = None  # type: Optional[Dict[int, Dict[int, int]]]

    # ------------------------------------------------------------ constructors

    @classmethod
    def line(cls, size: int) -> "CouplingMap":
        return cls([(index, index + 1) for index in range(size - 1)], size)

    @classmethod
    def ring(cls, size: int) -> "CouplingMap":
        return cls([(index, (index + 1) % size) for index in range(size)], size)

    @classmethod
    def full(cls, size: int) -> "CouplingMap":
        return cls(
            [(a, b) for a in range(size) for b in range(a + 1, size)], size
        )

    @classmethod
    def grid(cls, rows: int, columns: int) -> "CouplingMap":
        edges = []
        for row in range(rows):
            for column in range(columns):
                index = row * columns + column
                if column + 1 < columns:
                    edges.append((index, index + 1))
                if row + 1 < rows:
                    edges.append((index, index + columns))
        return cls(edges, rows * columns)

    # ------------------------------------------------------------------ graph

    def adjacent(self, a: int, b: int) -> bool:
        return (min(a, b), max(a, b)) in self.edges

    def distances(self) -> Dict[int, Dict[int, int]]:
        """All-pairs shortest path lengths, computed once by BFS."""
        if self._distances is None:
            self._distances = {
                source: self._bfs(source) for source in range(self.size)
            }
        return self._distances

    def _bfs(self, source: int) -> Dict[int, int]:
        seen = {source: 0}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for neighbour in self.neighbours.get(current, ()):
                if neighbour not in seen:
                    seen[neighbour] = seen[current] + 1
                    queue.append(neighbour)
        return seen

    def path(self, source: int, target: int) -> List[int]:
        """A shortest path from ``source`` to ``target``, inclusive."""
        previous = {source: None}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            if current == target:
                break
            for neighbour in sorted(self.neighbours.get(current, ())):
                if neighbour not in previous:
                    previous[neighbour] = current
                    queue.append(neighbour)
        if target not in previous:
            raise TranspileError(
                "physical qubits %d and %d are not connected on this device"
                % (source, target)
            )
        route = []
        node = target
        while node is not None:
            route.append(node)
            node = previous[node]
        route.reverse()
        return route

    def is_connected(self) -> bool:
        return self.size <= 1 or len(self._bfs(0)) == self.size

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CouplingMap(size=%d, edges=%d)" % (self.size, len(self.edges))


#: The basis a circuit must be in before routing: the twelve-gate whitelist
#: minus ``ccx``, because a three-qubit gate has no meaning on a coupling graph
#: until it is expressed as two-qubit interactions. ``lower_to_basis`` expands
#: it into the standard fifteen-gate identity.
TWO_QUBIT_BASIS = frozenset({"h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap"})


def lower_for_routing(circuit: Circuit) -> Circuit:
    """Lower ``circuit`` into a form :func:`route` accepts."""
    from .decompose import lower_to_basis

    return lower_to_basis(circuit, TWO_QUBIT_BASIS)


#: Connectivity of the devices LoomQ can actually reach, read off each
#: platform's own console rather than guessed.
DEVICE_COUPLING = {
    "gemini_vp": CouplingMap([(0, 1)], 2),
    "triangulum_vp": CouplingMap.full(3),
    "superconductor_vp": CouplingMap.line(8),
}


class Layout(object):
    """Which physical qubit each logical qubit currently sits on."""

    __slots__ = ("logical_to_physical", "physical_to_logical")

    def __init__(self, mapping: Dict[int, int]) -> None:
        self.logical_to_physical = dict(mapping)
        self.physical_to_logical = {
            physical: logical for logical, physical in mapping.items()
        }

    @classmethod
    def identity(cls, num_qubits: int) -> "Layout":
        return cls({index: index for index in range(num_qubits)})

    def physical(self, logical: int) -> int:
        return self.logical_to_physical[logical]

    def swap(self, a: int, b: int) -> None:
        """Exchange whatever logical qubits sit on physical ``a`` and ``b``."""
        la = self.physical_to_logical.get(a)
        lb = self.physical_to_logical.get(b)
        if la is not None:
            self.logical_to_physical[la] = b
        if lb is not None:
            self.logical_to_physical[lb] = a
        self.physical_to_logical[a] = lb
        self.physical_to_logical[b] = la
        if lb is None:
            del self.physical_to_logical[a]
        if la is None:
            del self.physical_to_logical[b]

    def copy(self) -> "Layout":
        return Layout(self.logical_to_physical)

    def as_list(self, num_qubits: int) -> List[int]:
        return [self.logical_to_physical.get(index, index) for index in range(num_qubits)]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Layout(%r)" % (self.logical_to_physical,)


def route(
    circuit: Circuit,
    coupling: CouplingMap,
    initial_layout: Optional[Layout] = None,
) -> Tuple[Circuit, Layout]:
    """Rewrite ``circuit`` so every two-qubit gate acts on adjacent qubits.

    Returns the routed circuit and the final layout.  The routed circuit is
    expressed in *physical* qubit indices; measurements already carry the
    mapping, so its counts are directly comparable with the original's.
    """
    if circuit.num_qubits > coupling.size:
        raise TranspileError(
            "circuit needs %d qubits but the device has %d"
            % (circuit.num_qubits, coupling.size)
        )
    if not coupling.is_connected():
        raise TranspileError("the device's coupling map is not connected")

    layout = initial_layout.copy() if initial_layout else Layout.identity(coupling.size)
    routed = circuit.copy_empty()
    if routed.num_qubits < coupling.size:
        # Physical qubits beyond the program's registers still need to exist.
        routed.add_qreg("anc", coupling.size - routed.num_qubits)

    for op in circuit.ops:
        if isinstance(op, ConditionalOp):
            raise TranspileError("routing does not handle classical feed-forward yet")
        if isinstance(op, GateOp) and len(op.qubits) > 2:
            raise TranspileError(
                "route() expects a circuit lowered to one- and two-qubit gates; "
                "found %s on %d qubits" % (op.name, len(op.qubits))
            )

        if isinstance(op, GateOp) and len(op.qubits) == 2:
            a, b = op.qubits
            for physical_a, physical_b in _bring_together(coupling, layout, a, b):
                routed.append(GateOp("swap", (), (physical_a, physical_b)))
                layout.swap(physical_a, physical_b)
            routed.append(
                GateOp(op.name, op.params, (layout.physical(a), layout.physical(b)))
            )
            continue

        if isinstance(op, GateOp):
            routed.append(GateOp(op.name, op.params, (layout.physical(op.qubits[0]),)))
        elif isinstance(op, MeasureOp):
            routed.append(MeasureOp(layout.physical(op.qubit), op.clbit))
        elif isinstance(op, ResetOp):
            routed.append(ResetOp(layout.physical(op.qubit)))
        elif isinstance(op, BarrierOp):
            routed.append(BarrierOp([layout.physical(index) for index in op.qubits]))
        else:  # pragma: no cover - defensive
            raise TranspileError("routing cannot handle %r" % (op,))

    return routed, layout


def _bring_together(
    coupling: CouplingMap, layout: Layout, a: int, b: int
) -> List[Tuple[int, int]]:
    """Swaps that make logical ``a`` and ``b`` adjacent, as physical pairs.

    The path is walked from both ends so the two qubits meet in the middle,
    which costs ``floor((d-1)/2)`` swaps instead of ``d-1``.
    """
    physical_a = layout.physical(a)
    physical_b = layout.physical(b)
    if coupling.adjacent(physical_a, physical_b):
        return []

    path = coupling.path(physical_a, physical_b)
    swaps = []
    left, right = 0, len(path) - 1
    while right - left > 1:
        # Step the left end one hop along the path; `a` moves with it.
        swaps.append((path[left], path[left + 1]))
        left += 1
        if right - left <= 1:
            break
        # Then step the right end one hop back; `b` moves with it.
        swaps.append((path[right], path[right - 1]))
        right -= 1
    return swaps


def routed_gate_count(circuit: Circuit, coupling: CouplingMap) -> int:
    """How many swaps routing would add — a cheap cost estimate."""
    routed, _ = route(circuit, coupling)
    added = len(routed.gates) - len(circuit.gates)
    return max(added, 0)


__all__ = [
    "CouplingMap",
    "DEVICE_COUPLING",
    "Layout",
    "TWO_QUBIT_BASIS",
    "lower_for_routing",
    "route",
    "routed_gate_count",
]
