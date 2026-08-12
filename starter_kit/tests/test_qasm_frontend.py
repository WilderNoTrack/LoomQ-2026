"""OpenQASM 2.0 front end: what it accepts, what it refuses, and how it explains."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loomq.errors import QasmError
from loomq.ir import ConditionalOp, GateOp, MeasureOp
from loomq.qasm import parse_qasm
from loomq.sim import ideal_distribution, measurement_width


def distribution(source):
    circuit = parse_qasm(source)
    return ideal_distribution(circuit, measurement_width(circuit))


class ParserTests(unittest.TestCase):
    def test_bell_state(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[2];\n'
            "h q[0];\ncx q[0],q[1];\nmeasure q -> c;\n"
        )
        self.assertEqual(circuit.num_qubits, 2)
        self.assertEqual(len(circuit.gates), 2)
        self.assertEqual(len(circuit.measurements), 2)

    def test_counts_key_is_little_endian(self):
        """The rightmost character of a counts key is c[0], as the rules fix it."""
        result = distribution(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "x q[0];\nmeasure q -> c;\n"
        )
        self.assertEqual(result, {"001": 1.0})

    def test_register_broadcast(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
            "h q;\nmeasure q -> c;\n"
        )
        self.assertEqual(len(circuit.gates), 3)

    def test_multiple_registers_flatten_in_declaration_order(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg a[2];\nqreg b[1];\ncreg c[3];\n'
            "x b[0];\nmeasure a[0] -> c[0];\nmeasure a[1] -> c[1];\nmeasure b[0] -> c[2];\n"
        )
        self.assertEqual(circuit.num_qubits, 3)
        self.assertEqual(circuit.gates[0].qubits, (2,))
        self.assertEqual(circuit.qubit_label(2), "b[0]")
        self.assertEqual(
            ideal_distribution(circuit, measurement_width(circuit)), {"100": 1.0}
        )

    def test_mismatched_broadcast_is_rejected(self):
        with self.assertRaises(QasmError) as caught:
            parse_qasm(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[3];\n'
                "measure q -> c;\n"
            )
        self.assertIn("different sizes", str(caught.exception))

    def test_user_defined_gates_are_inlined(self):
        result = distribution(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
            "gate mybell a, b { h a; cx a, b; }\n"
            "qreg q[2];\ncreg c[2];\nmybell q[0], q[1];\nmeasure q -> c;\n"
        )
        self.assertEqual(result, {"00": 0.5, "11": 0.5})

    def test_parameterised_user_gate(self):
        result = distribution(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
            "gate spin(theta) a { ry(theta) a; }\n"
            "qreg q[1];\ncreg c[1];\nspin(pi) q[0];\nmeasure q -> c;\n"
        )
        self.assertAlmostEqual(result.get("1", 0.0), 1.0, places=9)

    def test_angle_expressions(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "rz(2*pi/4 + 0.5) q[0];\nmeasure q -> c;\n"
        )
        self.assertAlmostEqual(circuit.gates[0].params[0], math.pi / 2 + 0.5, places=12)

    def test_feed_forward_is_parsed_and_simulated(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[2];\n'
            "h q[0];\nmeasure q[0] -> c[0];\nif (c == 1) x q[1];\nmeasure q[1] -> c[1];\n"
        )
        self.assertTrue(any(isinstance(op, ConditionalOp) for op in circuit.ops))
        result = ideal_distribution(circuit, measurement_width(circuit))
        self.assertEqual({key: round(value, 9) for key, value in result.items()},
                         {"00": 0.5, "11": 0.5})

    def test_reset_collapses_to_zero(self):
        result = distribution(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "x q[0];\nreset q[0];\nmeasure q -> c;\n"
        )
        self.assertEqual(result, {"0": 1.0})

    def test_case_and_alias_tolerance(self):
        """`CX`/`cnot` are accepted; the agent's job is helping, not gatekeeping."""
        result = distribution(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[2];\n'
            "H q[0];\ncnot q[0],q[1];\nmeasure q -> c;\n"
        )
        self.assertEqual(result, {"00": 0.5, "11": 0.5})

    def test_comments_are_ignored(self):
        result = distribution(
            'OPENQASM 2.0;\n// line comment\n/* block\ncomment */\n'
            'include "qelib1.inc";\nqreg q[1];\ncreg c[1];\nx q[0]; // trailing\n'
            "measure q -> c;\n"
        )
        self.assertEqual(result, {"1": 1.0})


class DiagnosticTests(unittest.TestCase):
    """Error messages are a product feature here, so they are tested like one."""

    def assert_message(self, source, *fragments):
        with self.assertRaises(QasmError) as caught:
            parse_qasm(source)
        text = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, text)
        return text

    def test_undeclared_register_names_the_register(self):
        self.assert_message("OPENQASM 2.0;\nqreg q[1];\nh r[0];\n", "undeclared", "'r'")

    def test_out_of_range_index_reports_the_bound(self):
        self.assert_message(
            "OPENQASM 2.0;\nqreg q[2];\nh q[5];\n", "q[5]", "0..1"
        )

    def test_wrong_arity_suggests_the_operand_order(self):
        self.assert_message(
            "OPENQASM 2.0;\nqreg q[2];\ncx q[0];\n", "2 qubit(s)", "control, target"
        )

    def test_unknown_gate_lists_where_to_look(self):
        self.assert_message("OPENQASM 2.0;\nqreg q[1];\nfoo q[0];\n", "unknown gate", "'foo'")

    def test_missing_register_declaration(self):
        self.assert_message("OPENQASM 2.0;\nh q[0];\n", "undeclared")

    def test_empty_program_shows_a_minimal_example(self):
        self.assert_message("   ", "empty", "OPENQASM 2.0;")

    def test_error_carries_a_line_number(self):
        text = self.assert_message(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\nbogus q[0];\n',
            "line 5",
        )
        self.assertIn("bogus", text)

    def test_duplicate_register(self):
        self.assert_message("OPENQASM 2.0;\nqreg q[1];\nqreg q[2];\n", "declared twice")

    def test_same_qubit_twice_in_one_gate(self):
        self.assert_message(
            "OPENQASM 2.0;\nqreg q[2];\ncx q[0], q[0];\n", "same qubit"
        )


if __name__ == "__main__":
    unittest.main()
