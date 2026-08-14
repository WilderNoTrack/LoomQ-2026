"""Optimisation must remove gates without moving a single amplitude.

Counts would not catch a phase error, so every correctness test here compares
full statevectors up to global phase. The savings tests then check the pass
actually earns its place — an optimiser that is safe but never fires is just
latency.
"""

import cmath
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loomq.circuits import bell, ghz, grover, qft, random_circuit, whitelist_exercise
from loomq.gates import lookup
from loomq.ir import GateOp
from loomq.passes import lower_to_basis
from loomq.passes.peephole import peephole, savings
from loomq.qasm import parse_qasm
from loomq.sim.statevector import apply_matrix


def statevector(circuit):
    """Unitary evolution from a non-trivial input, so phases are exercised."""
    num_qubits = circuit.num_qubits
    state = [0j] * (1 << num_qubits)
    state[0] = 1 + 0j
    for qubit in range(num_qubits):
        apply_matrix(state, num_qubits, lookup("h").matrix(()), (qubit,))
        apply_matrix(state, num_qubits, lookup("t").matrix(()), (qubit,))
        apply_matrix(state, num_qubits, lookup("ry").matrix((0.4 + qubit,)), (qubit,))
    for op in circuit.gates:
        apply_matrix(state, num_qubits, lookup(op.name).matrix(op.params), op.qubits)
    return state


class EquivalenceTests(unittest.TestCase):
    def assert_same_unitary(self, before, after, label=""):
        first = statevector(before)
        second = statevector(after)
        reference = next(
            (index for index, value in enumerate(first) if abs(value) > 1e-9), None
        )
        self.assertIsNotNone(reference, "degenerate test state")
        phase = second[reference] / first[reference]
        self.assertAlmostEqual(abs(phase), 1.0, places=9, msg="%s changed the norm" % label)
        for expected, observed in zip(first, second):
            self.assertAlmostEqual(
                abs(observed - phase * expected), 0.0, places=9,
                msg="%s changed the state by more than a global phase" % label,
            )

    def test_official_circuits_survive(self):
        for name, source in (
            ("bell", bell()), ("ghz3", ghz(3)), ("ghz5", ghz(5)),
            ("qft4", qft(4)), ("grover3", grover(3)),
            ("whitelist", whitelist_exercise()),
        ):
            lowered = lower_to_basis(parse_qasm(source))
            self.assert_same_unitary(lowered, peephole(lowered), name)

    def test_random_circuits_survive(self):
        for seed in range(12):
            lowered = lower_to_basis(parse_qasm(random_circuit(4, 18, seed=seed)))
            self.assert_same_unitary(lowered, peephole(lowered), "random-%d" % seed)

    def test_measurements_and_order_are_preserved(self):
        circuit = lower_to_basis(parse_qasm(ghz(3)))
        optimised = peephole(circuit)
        self.assertEqual(
            [(op.qubit, op.clbit) for op in circuit.measurements],
            [(op.qubit, op.clbit) for op in optimised.measurements],
        )


class CancellationTests(unittest.TestCase):
    def build(self, body, qubits=2):
        return lower_to_basis(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n%s\n'
            % (qubits, qubits, body)
        ))

    def test_self_inverse_pair(self):
        self.assertEqual(len(peephole(self.build("h q[0];\nh q[0];")).gates), 0)

    def test_inverse_pair(self):
        self.assertEqual(len(peephole(self.build("s q[0];\nsdg q[0];")).gates), 0)

    def test_cx_pair(self):
        self.assertEqual(len(peephole(self.build("cx q[0],q[1];\ncx q[0],q[1];")).gates), 0)

    def test_cancellation_through_a_disjoint_gate(self):
        """`h q[0]; x q[1]; h q[0];` — the x does not touch q[0]."""
        optimised = peephole(self.build("h q[0];\nx q[1];\nh q[0];"))
        self.assertEqual([op.name for op in optimised.gates], ["x"])

    def test_diagonal_gates_commute_past_each_other(self):
        optimised = peephole(self.build("t q[0];\nrz(0.3) q[0];\ntdg q[0];", qubits=1))
        self.assertLessEqual(len(optimised.gates), 1)

    def test_non_commuting_gate_blocks_cancellation(self):
        """`h q[0]; x q[0]; h q[0];` is not the identity and must survive."""
        optimised = peephole(self.build("h q[0];\nx q[0];\nh q[0];", qubits=1))
        self.assertGreater(len(optimised.gates), 0)
        EquivalenceTests().assert_same_unitary(
            self.build("h q[0];\nx q[0];\nh q[0];", qubits=1), optimised
        )

    def test_opposite_rotations_cancel(self):
        optimised = peephole(self.build("rz(0.7) q[0];\nrz(-0.7) q[0];", qubits=1))
        self.assertEqual(len(optimised.gates), 0)


class SavingsTests(unittest.TestCase):
    """The pass has to actually shrink the circuits it will meet on hardware.

    ``lower_to_basis`` runs this pass itself, so these measure it in isolation
    by lowering with ``optimize=False`` first.
    """

    @staticmethod
    def raw(source):
        return lower_to_basis(parse_qasm(source), optimize=False)

    def test_lowered_toffoli_shrinks(self):
        circuit = self.raw(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "ccx q[0],q[1],q[2];\nccx q[0],q[1],q[2];\nmeasure q -> c;\n"
        )
        report = savings(circuit, peephole(circuit))
        self.assertGreater(report["removed"], 0, report)

    def test_two_toffolis_cancel_completely(self):
        """ccx is self-inverse, so the pair must vanish end to end."""
        circuit = lower_to_basis(parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "ccx q[0],q[1],q[2];\nccx q[0],q[1],q[2];\nmeasure q -> c;\n"
        ))
        self.assertEqual(len(circuit.gates), 0)

    def test_grover_shrinks(self):
        circuit = self.raw(grover(3))
        report = savings(circuit, peephole(circuit))
        self.assertGreater(report["removed"], 0, report)

    def test_pipeline_applies_it(self):
        """`lower_to_basis` must run the pass, not merely offer it."""
        raw = self.raw(grover(3))
        pipeline = lower_to_basis(parse_qasm(grover(3)))
        self.assertLess(len(pipeline.gates), len(raw.gates))

    def test_single_qubit_run_collapses_to_at_most_three(self):
        circuit = self.raw(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "h q[0];\nt q[0];\ns q[0];\nry(0.3) q[0];\nrz(0.9) q[0];\nh q[0];\n"
        )
        optimised = peephole(circuit)
        self.assertLessEqual(len(optimised.gates), 3)
        EquivalenceTests().assert_same_unitary(circuit, optimised)

    def test_idempotent(self):
        circuit = self.raw(qft(4))
        once = peephole(circuit)
        twice = peephole(once)
        self.assertEqual(len(once.gates), len(twice.gates))


if __name__ == "__main__":
    unittest.main()
