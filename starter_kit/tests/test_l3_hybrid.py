"""L3: Hybrid-QASM parsing, RISC-V code generation, and exhaustive verification.

Formal scoring generates its own programs and injects every measurement
combination, so the important test here is not the published example — it is
:meth:`FuzzTests.test_random_programs`, which does the same thing locally
against the official emulator.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adapter
from loomq.errors import HybridQasmError
from loomq.hybrid import compile_hybrid, load_emulator, verify, verify_all
from loomq.hybrid.ast import measurement_bits
from loomq.hybrid.fuzz import generate
from loomq.hybrid.parser import parse_hybrid

EXAMPLE = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {                 // classical control block
  if (c[0] == 1) {
    r1 = 100;
  } else {
    r1 = 10;
  }
  r1 = r1 + 5;
}
cx q[0], q[1];
"""

PUBLIC_CASE = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical { if (c[0] == 1) { r1 = 7; } else { r1 = 3; } }
"""


class ContractTests(unittest.TestCase):
    def test_returns_operations_and_assembly(self):
        operations, assembly = compile_hybrid(EXAMPLE)
        self.assertIsInstance(operations, list)
        self.assertIsInstance(assembly, str)
        self.assertTrue(assembly.strip())
        self.assertTrue(all(isinstance(line, str) for line in operations))

    def test_quantum_operations_are_valid_qasm_statements(self):
        operations, _ = compile_hybrid(EXAMPLE)
        self.assertEqual(
            operations, ["h q[0];", "measure q[0] -> c[0];", "cx q[0], q[1];"]
        )

    def test_adapter_entry_point(self):
        operations, assembly = adapter.compile_hybrid(PUBLIC_CASE)
        self.assertEqual(operations, ["measure q[0] -> c[0];"])
        self.assertIn("li", assembly)

    def test_public_evaluator_case(self):
        """The exact check `evaluator.py --level l3` performs."""
        emulator_class = load_emulator()
        _, assembly = compile_hybrid(PUBLIC_CASE)
        for measured, expected in ((0, 3), (1, 7)):
            emulator = emulator_class()
            emulator.load_program(assembly)
            emulator.set_register("x10", measured)
            self.assertEqual(emulator.execute().get("x1", 0), expected)

    def test_example_from_the_rules(self):
        ok, records = verify_all(EXAMPLE)
        self.assertTrue(ok, records)
        by_injection = {tuple(sorted(r["injection"].items())): r for r in records}
        self.assertEqual(by_injection[((0, 0),)]["actual"].get("x1"), 15)
        self.assertEqual(by_injection[((0, 1),)]["actual"].get("x1"), 105)


class CodeGenerationTests(unittest.TestCase):
    def assert_state(self, source, injection, expected):
        records = verify(source, bits=sorted(injection))
        for record in records:
            if record["injection"] == injection:
                self.assertTrue(record["match"], record)
                for name, value in expected.items():
                    self.assertEqual(record["actual"].get(name, 0), value, record)
                return
        self.fail("injection %r was not exercised" % (injection,))

    def wrap(self, body, bits=1):
        return (
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n' % (bits, bits)
            + "".join("measure q[%d] -> c[%d];\n" % (i, i) for i in range(bits))
            + "classical {\n%s\n}\n" % body
        )

    def test_measurement_registers_are_not_clobbered(self):
        """c[0] is read twice; x10 must still hold the injected value."""
        self.assert_state(
            self.wrap("r1 = c[0] + c[0];\nr2 = c[0];"), {0: 1}, {"x1": 2, "x2": 1, "x10": 1}
        )

    def test_self_referencing_assignment(self):
        self.assert_state(self.wrap("r1 = 10;\nr1 = r1 + 5;"), {0: 0}, {"x1": 15})

    def test_destination_aliases_a_read_operand(self):
        """`r1 = r2 - r1` must read the old r1 before the destination is written."""
        self.assert_state(
            self.wrap("r1 = 4;\nr2 = 10;\nr1 = r2 - r1;"), {0: 0}, {"x1": 6, "x2": 10}
        )

    def test_negative_and_folded_constants(self):
        self.assert_state(self.wrap("r1 = 5 - 12;\nr2 = 1 + 2 + 3;"), {0: 0}, {"x1": -7, "x2": 6})

    def test_not_equal_comparison(self):
        source = self.wrap("if (c[0] != 0) { r1 = 1; } else { r1 = 2; }")
        self.assert_state(source, {0: 0}, {"x1": 2})
        self.assert_state(source, {0: 1}, {"x1": 1})

    def test_nested_and_else_if(self):
        source = self.wrap(
            "if (c[0] == 1) {\n"
            "  if (c[1] == 1) { r1 = 3; } else { r1 = 2; }\n"
            "} else if (c[1] == 1) { r1 = 1; } else { r1 = 0; }",
            bits=2,
        )
        self.assert_state(source, {0: 1, 1: 1}, {"x1": 3})
        self.assert_state(source, {0: 1, 1: 0}, {"x1": 2})
        self.assert_state(source, {0: 0, 1: 1}, {"x1": 1})

    def test_expression_comparison(self):
        source = self.wrap("r1 = 3;\nif (r1 + c[0] == 4) { r2 = 9; } else { r2 = 8; }")
        self.assert_state(source, {0: 1}, {"x2": 9})
        self.assert_state(source, {0: 0}, {"x2": 8})

    def test_scratch_registers_are_cleared(self):
        """Leftover temporaries would show up as spurious registers in the result."""
        _, assembly = compile_hybrid(
            self.wrap("if (c[0] == 1) { r1 = 5 - c[0]; } else { r1 = 0; }")
        )
        emulator_class = load_emulator()
        emulator = emulator_class()
        emulator.load_program(assembly)
        emulator.set_register("x10", 1)
        state = emulator.execute()
        self.assertEqual(set(state) - {"x10"}, {"x1"})

    def test_multiple_classical_blocks_run_in_order(self):
        source = (
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "measure q[0] -> c[0];\n"
            "classical { r1 = 1; }\nx q[0];\nclassical { r1 = r1 + 41; }\n"
        )
        self.assert_state(source, {0: 0}, {"x1": 42})

    def test_only_the_used_measurement_bits_are_reported(self):
        _, program = parse_hybrid(self.wrap("r1 = c[1];", bits=2))
        self.assertEqual(measurement_bits(program), [1])


class ErrorTests(unittest.TestCase):
    def test_unterminated_block(self):
        with self.assertRaises(HybridQasmError):
            compile_hybrid('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nclassical { r1 = 1;\n')

    def test_register_out_of_range(self):
        with self.assertRaises(HybridQasmError):
            compile_hybrid(
                'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nclassical { r12 = 1; }\n'
            )

    def test_condition_must_be_a_comparison(self):
        with self.assertRaises(HybridQasmError):
            compile_hybrid(
                'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nclassical { if (c[0]) { r1 = 1; } }\n'
            )

    def test_quantum_half_is_validated_too(self):
        with self.assertRaises(Exception):
            compile_hybrid(
                'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nh q[9];\nclassical { r1 = 1; }\n'
            )


class FuzzTests(unittest.TestCase):
    CASES = int(os.environ.get("LOOMQ_FUZZ_CASES", "150"))

    def test_random_programs(self):
        """Randomly generated programs, every measurement injection, no exceptions."""
        for seed in range(self.CASES):
            source = generate(seed=seed)
            ok, records = verify_all(source)
            self.assertTrue(ok, "seed %d failed:\n%s\n%r" % (seed, source, records))

    def test_deeper_nesting(self):
        from loomq.hybrid.fuzz import HybridGenerator

        for seed in range(40):
            source = HybridGenerator(seed=seed, max_depth=4, max_variables=6).program(bits=3)
            ok, records = verify_all(source)
            self.assertTrue(ok, "seed %d failed:\n%s\n%r" % (seed, source, records))


if __name__ == "__main__":
    unittest.main()
