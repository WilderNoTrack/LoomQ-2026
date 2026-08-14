"""Qubit routing: the pass must change the circuit without changing the physics.

Every test here reduces to one question — does the routed circuit produce the
same measurement distribution as the original? Routing rewrites operand indices
and inserts swaps, so a sign error or a stale layout would silently permute the
outcomes. Comparing exact distributions catches that; comparing gate counts
would not.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loomq.circuits import bell, ghz, grover, qft, random_circuit
from loomq.errors import TranspileError
from loomq.ir import GateOp
from loomq.passes import lower_to_basis
from loomq.passes.routing import (
    CouplingMap,
    DEVICE_COUPLING,
    Layout,
    lower_for_routing,
    route,
    routed_gate_count,
)
from loomq.qasm import parse_qasm
from loomq.sim import ideal_distribution, measurement_width


def distribution(circuit, width=None):
    return ideal_distribution(circuit, width or measurement_width(circuit))


def two_qubit_ops(circuit):
    return [op for op in circuit.gates if len(op.qubits) == 2]


class CouplingMapTests(unittest.TestCase):
    def test_line_adjacency(self):
        line = CouplingMap.line(5)
        self.assertTrue(line.adjacent(0, 1))
        self.assertTrue(line.adjacent(3, 4))
        self.assertFalse(line.adjacent(0, 4))
        self.assertTrue(line.is_connected())

    def test_shortest_path(self):
        line = CouplingMap.line(5)
        self.assertEqual(line.path(0, 4), [0, 1, 2, 3, 4])
        self.assertEqual(line.path(4, 0), [4, 3, 2, 1, 0])
        self.assertEqual(len(line.path(1, 2)), 2)

    def test_grid_distances(self):
        grid = CouplingMap.grid(3, 3)
        distances = grid.distances()
        self.assertEqual(distances[0][8], 4)   # corner to opposite corner
        self.assertEqual(distances[0][4], 2)   # corner to centre

    def test_ring_is_shorter_than_line(self):
        self.assertEqual(CouplingMap.ring(6).distances()[0][5], 1)
        self.assertEqual(CouplingMap.line(6).distances()[0][5], 5)

    def test_disconnected_map_is_rejected(self):
        broken = CouplingMap([(0, 1), (2, 3)], 4)
        with self.assertRaises(TranspileError):
            route(parse_qasm(bell()), broken)

    def test_real_device_maps(self):
        """Read off each platform's own console, not invented."""
        self.assertEqual(DEVICE_COUPLING["gemini_vp"].size, 2)
        self.assertTrue(DEVICE_COUPLING["triangulum_vp"].adjacent(0, 2))
        line = DEVICE_COUPLING["superconductor_vp"]
        self.assertEqual(line.size, 8)
        self.assertFalse(line.adjacent(0, 7))


class LayoutTests(unittest.TestCase):
    def test_swap_exchanges_occupants(self):
        layout = Layout.identity(4)
        layout.swap(1, 2)
        self.assertEqual(layout.physical(1), 2)
        self.assertEqual(layout.physical(2), 1)
        self.assertEqual(layout.physical(0), 0)

    def test_swap_is_its_own_inverse(self):
        layout = Layout.identity(4)
        before = layout.as_list(4)
        layout.swap(0, 3)
        layout.swap(0, 3)
        self.assertEqual(layout.as_list(4), before)


class RoutingCorrectnessTests(unittest.TestCase):
    """The distribution must survive routing, on every topology."""

    def assert_routing_preserves(self, qasm, coupling):
        source = lower_for_routing(parse_qasm(qasm))
        width = measurement_width(source)
        expected = distribution(source, width)

        routed, layout = route(source, coupling)
        observed = distribution(routed, width)

        self.assertEqual(
            {key: round(value, 9) for key, value in observed.items()},
            {key: round(value, 9) for key, value in expected.items()},
            "routing changed the distribution",
        )
        for op in two_qubit_ops(routed):
            self.assertTrue(
                coupling.adjacent(*op.qubits),
                "%s acts on non-adjacent qubits %r after routing" % (op.name, op.qubits),
            )
        return routed, layout

    def test_bell_on_a_line(self):
        self.assert_routing_preserves(bell(), CouplingMap.line(2))

    def test_ghz5_on_a_line(self):
        """The worst case: a GHZ chain needs no swaps, a star would need many."""
        routed, _ = self.assert_routing_preserves(ghz(5), CouplingMap.line(5))
        self.assertGreaterEqual(len(routed.gates), 4)

    def test_qft4_on_a_line(self):
        """QFT couples every pair, so a line forces real swap traffic."""
        routed, _ = self.assert_routing_preserves(qft(4), CouplingMap.line(4))
        added = len(routed.gates) - len(lower_for_routing(parse_qasm(qft(4))).gates)
        self.assertGreater(added, 0, "QFT-4 on a line must need swaps")

    def test_grover3_on_a_line(self):
        self.assert_routing_preserves(grover(3), CouplingMap.line(3))

    def test_random_circuits_on_several_topologies(self):
        for seed in (11, 22, 33):
            qasm = random_circuit(4, 14, seed=seed)
            for coupling in (
                CouplingMap.line(4),
                CouplingMap.ring(4),
                CouplingMap.grid(2, 2),
                CouplingMap.full(4),
            ):
                self.assert_routing_preserves(qasm, coupling)

    def test_circuit_on_a_larger_device(self):
        """A 2-qubit program on an 8-qubit line still measures the right bits."""
        self.assert_routing_preserves(bell(), CouplingMap.line(8))

    def test_real_device_topologies(self):
        self.assert_routing_preserves(bell(), DEVICE_COUPLING["gemini_vp"])
        self.assert_routing_preserves(ghz(3), DEVICE_COUPLING["triangulum_vp"])
        self.assert_routing_preserves(ghz(5), DEVICE_COUPLING["superconductor_vp"])

    def test_fully_connected_needs_no_swaps(self):
        source = lower_for_routing(parse_qasm(qft(4)))
        routed, _ = route(source, CouplingMap.full(4))
        self.assertEqual(len(routed.gates), len(source.gates))

    def test_swap_cost_grows_with_distance(self):
        """A line should cost more swaps than a ring for the same circuit."""
        source = qft(5)
        on_line = routed_gate_count(lower_for_routing(parse_qasm(source)), CouplingMap.line(5))
        on_ring = routed_gate_count(lower_for_routing(parse_qasm(source)), CouplingMap.ring(5))
        self.assertGreaterEqual(on_line, on_ring)


class RoutingGuardTests(unittest.TestCase):
    def test_circuit_too_wide_for_the_device(self):
        with self.assertRaises(TranspileError):
            route(lower_for_routing(parse_qasm(ghz(5))), CouplingMap.line(3))

    def test_three_qubit_gates_must_be_lowered_first(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "ccx q[0],q[1],q[2];\nmeasure q -> c;\n"
        )
        with self.assertRaises(TranspileError) as caught:
            route(circuit, CouplingMap.line(3))
        self.assertIn("lowered", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
