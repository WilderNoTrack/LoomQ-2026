"""Stabilizer simulation must agree with the statevector, then go far beyond it.

Two obligations. Where both simulators can run, their distributions have to
match — that is what makes the fast path trustworthy. And where the statevector
cannot run at all, the tableau still has to produce the right answer, which is
the entire point of having it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loomq.circuits import bell, ghz, random_circuit
from loomq.errors import LoomQError
from loomq.passes import lower_to_basis
from loomq.qasm import parse_qasm
from loomq.result import counts_to_distribution, hellinger_fidelity
from loomq.sim import ideal_distribution, measurement_width
from loomq.sim.stabilizer import Tableau, is_clifford, sample_counts

SHOTS = 4000


class ClassificationTests(unittest.TestCase):
    def test_ghz_is_clifford(self):
        self.assertTrue(is_clifford(parse_qasm(ghz(5))))

    def test_bell_is_clifford(self):
        self.assertTrue(is_clifford(parse_qasm(bell())))

    def test_t_gate_is_not(self):
        self.assertFalse(is_clifford(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "h q[0];\nt q[0];\nmeasure q -> c;\n"
        )))

    def test_quarter_turn_rz_is_clifford_but_an_eighth_is_not(self):
        def circuit(angle):
            return parse_qasm(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
                "h q[0];\nrz(%r) q[0];\nmeasure q -> c;\n" % angle
            )

        import math

        self.assertTrue(is_clifford(circuit(math.pi / 2)))
        self.assertTrue(is_clifford(circuit(math.pi)))
        self.assertFalse(is_clifford(circuit(math.pi / 4)))

    def test_toffoli_is_not(self):
        self.assertFalse(is_clifford(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "ccx q[0],q[1],q[2];\nmeasure q -> c;\n"
        )))

    def test_non_clifford_is_refused_rather_than_approximated(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "t q[0];\nmeasure q -> c;\n"
        )
        with self.assertRaises(LoomQError):
            sample_counts(circuit, 10)


class AgreementTests(unittest.TestCase):
    """Where both simulators can run, they must agree."""

    def assert_agrees(self, qasm, shots=SHOTS, seed=7):
        circuit = parse_qasm(qasm)
        width = measurement_width(circuit)
        expected = ideal_distribution(circuit, width)
        counts = sample_counts(circuit, shots, width, seed=seed)
        self.assertEqual(sum(counts.values()), shots)
        fidelity = hellinger_fidelity(counts_to_distribution(counts), expected)
        self.assertGreaterEqual(
            fidelity, 0.97,
            "tableau disagrees with the statevector: %r vs %r" % (counts, expected),
        )

    def test_bell(self):
        self.assert_agrees(bell())

    def test_ghz_various_sizes(self):
        for size in (3, 4, 5, 6):
            self.assert_agrees(ghz(size))

    def test_deterministic_basis_state(self):
        counts = sample_counts(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "x q[0];\nx q[2];\nmeasure q -> c;\n"
        ), 200, seed=3)
        self.assertEqual(counts, {"101": 200})

    def test_uniform_superposition(self):
        self.assert_agrees(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "h q[0];\nh q[1];\nh q[2];\nmeasure q -> c;\n"
        )

    def test_swap_and_phase_gates(self):
        self.assert_agrees(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "h q[0];\ns q[0];\ncx q[0],q[1];\nswap q[1],q[2];\nsdg q[2];\n"
            "h q[2];\nmeasure q -> c;\n"
        )

    def test_random_clifford_circuits(self):
        import math

        for seed in range(6):
            source = random_circuit(4, 16, seed=seed)
            circuit = parse_qasm(source)
            # Keep only the ones that happen to be Clifford.
            if is_clifford(circuit):
                self.assert_agrees(source, shots=3000, seed=seed)

    def test_reset(self):
        counts = sample_counts(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "h q[0];\nreset q[0];\nmeasure q -> c;\n"
        ), 100, seed=5)
        self.assertEqual(counts, {"0": 100})


class ScaleTests(unittest.TestCase):
    """The reason this exists: sizes the statevector cannot reach."""

    def test_ghz_60_qubits(self):
        """2^60 amplitudes is impossible; a 60-qubit tableau is instant."""
        counts = sample_counts(parse_qasm(ghz(60)), 200, seed=11)
        self.assertEqual(sum(counts.values()), 200)
        for key in counts:
            self.assertIn(set(key), ({"0"}, {"1"}), "GHZ collapsed to a mixed string")
        self.assertEqual(len(counts), 2, "a 60-qubit GHZ must give exactly two outcomes")

    def test_ghz_120_qubits(self):
        counts = sample_counts(parse_qasm(ghz(120)), 40, seed=13)
        self.assertEqual(sum(counts.values()), 40)
        self.assertLessEqual(len(counts), 2)
        for key in counts:
            self.assertEqual(len(set(key)), 1)

    def test_entanglement_is_perfect_at_scale(self):
        """Every shot must be all-zeros or all-ones — never anything between."""
        counts = sample_counts(parse_qasm(ghz(40)), 300, seed=17)
        self.assertEqual(set(counts), {"0" * 40, "1" * 40})


class TableauTests(unittest.TestCase):
    def test_fresh_tableau_measures_zero(self):
        tableau = Tableau(4, seed=1)
        for qubit in range(4):
            self.assertEqual(tableau.measure(qubit), 0)

    def test_x_flips_the_outcome(self):
        tableau = Tableau(2, seed=1)
        tableau.pauli_x(1)
        self.assertEqual(tableau.measure(0), 0)
        self.assertEqual(tableau.measure(1), 1)

    def test_hadamard_then_measure_is_repeatable(self):
        """After collapse, re-measuring must give the same answer."""
        tableau = Tableau(1, seed=2)
        tableau.hadamard(0)
        first = tableau.measure(0)
        for _ in range(5):
            self.assertEqual(tableau.measure(0), first)


if __name__ == "__main__":
    unittest.main()
