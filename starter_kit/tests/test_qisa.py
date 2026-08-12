"""Bonus: the LoomQ-Q quantum RISC-V extension.

Four things have to hold for the extension to be more than a nice idea:

1. the encoding round-trips — every instruction in the table survives
   ``encode`` then ``decode`` with its operands in the right fields;
2. the bit layout is the one the specification claims, checked against words
   computed by hand;
3. mnemonics and raw ``.word`` directives execute identically, which is what
   makes the encoding load-bearing rather than decorative;
4. a compiled Hybrid-QASM program means the same thing on the extended emulator
   as it does on the reference simulator — including that a classical branch
   follows the measurement that actually happened.
"""

import collections
import os
import sys
import unittest

_KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _KIT)

from loomq.hybrid.parser import split_classical_blocks
from loomq.qasm import parse_qasm
from loomq.qisa.assembler import assemble_line, listing, to_words
from loomq.qisa.compile import ANGLE_REGISTER, QUBIT_REGISTERS, compile_unified
from loomq.qisa.isa import (
    ANGLE_SCALE,
    OPCODE_CUSTOM_0,
    SPEC,
    decode,
    encode,
    is_quantum_word,
    quantize_angle,
    radians,
)
from loomq.result import hellinger_fidelity
from loomq.sim import ideal_distribution, measurement_width
from riscv_emulator_loomq import QuantumRISCVEmulator, run

EXAMPLES = os.path.join(_KIT, "examples")


def example(name):
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as handle:
        return handle.read()


class EncodingTests(unittest.TestCase):
    def test_every_instruction_round_trips(self):
        for mnemonic, entry in sorted(SPEC.items()):
            operands = tuple(range(1, len(entry.operands) + 1))
            word = encode(mnemonic, *operands)
            self.assertEqual(word & 0x7F, OPCODE_CUSTOM_0, mnemonic)
            instruction = decode(word)
            self.assertEqual(instruction.mnemonic, mnemonic)
            self.assertEqual(tuple(instruction.operands()), operands, mnemonic)

    def test_opcode_space_is_custom_0(self):
        """RISC-V reserves 0b0001011 for non-standard extensions."""
        self.assertEqual(OPCODE_CUSTOM_0, 0b0001011)
        for mnemonic, entry in SPEC.items():
            word = encode(mnemonic, *([0] * len(entry.operands)))
            self.assertTrue(is_quantum_word(word), mnemonic)

    def test_field_layout_matches_the_specification(self):
        """Words computed by hand from QISA.md section 3."""
        # 0000000 11101 11100 011 00000 0001011
        self.assertEqual(encode("qcx", 28, 29), 0x01DE300B)
        self.assertEqual(encode("qinit", 28), 0x000E000B)
        self.assertEqual(encode("qh", 28), 0x000E100B)
        self.assertEqual(encode("qmeas", 10, 28), 0x000E550B)
        self.assertEqual(encode("qccx", 28, 29, 30), 0x01DE4F0B)

    def test_decoder_rejects_foreign_opcodes(self):
        with self.assertRaises(ValueError):
            decode(0x00000013)  # a base-ISA addi
        with self.assertRaises(ValueError):
            decode(0x7F00000B | (0x7F << 25))  # custom-0 but no such funct7

    def test_operand_validation(self):
        with self.assertRaises(ValueError):
            encode("qh", 32)
        with self.assertRaises(ValueError):
            encode("qh")
        with self.assertRaises(ValueError):
            encode("qnope", 1)

    def test_angle_fixed_point(self):
        import math

        self.assertEqual(quantize_angle(math.pi), ANGLE_SCALE)
        self.assertEqual(quantize_angle(math.pi / 2), ANGLE_SCALE // 2)
        self.assertEqual(quantize_angle(0.0), 0)
        for value in (0.0, 0.3, -1.2, math.pi / 4):
            self.assertAlmostEqual(radians(quantize_angle(value)), value, places=3)

    def test_assembler_recognises_both_forms(self):
        self.assertEqual(assemble_line("qh x28"), 0x000E100B)
        self.assertEqual(assemble_line("  .word 0x000E100B  # comment"), 0x000E100B)
        self.assertIsNone(assemble_line("li x1, 5"))
        self.assertIsNone(assemble_line("# just a comment"))


class ExtendedEmulatorTests(unittest.TestCase):
    def test_classical_semantics_are_unchanged(self):
        """The fork adds instructions; it must not reinterpret existing ones."""
        program = """
        li x1, 5
        li x2, 10
        beq x1, x2, EQUAL
        add x3, x1, x2
        j END
        EQUAL:
        sub x3, x2, x1
        END:
        addi x3, x3, 1
        """
        self.assertEqual(run(program).get("x3"), 16)

    def test_measurement_collapses_and_writes_a_register(self):
        program = """
        li x28, 1
        qinit x28
        li x28, 0
        qx x28
        qmeas x10, x28
        """
        for seed in range(5):
            self.assertEqual(run(program, seed=seed).get("x10"), 1)

    def test_entangled_qubits_always_agree(self):
        program = """
        li x28, 2
        qinit x28
        li x28, 0
        qh x28
        li x29, 1
        qcx x28, x29
        qmeas x10, x28
        qmeas x11, x29
        bne x10, x11, DISAGREE
        li x1, 1
        j END
        DISAGREE:
        li x1, 999
        END:
        li x28, 0
        li x29, 0
        """
        seen = set()
        for seed in range(60):
            state = run(program, seed=seed)
            self.assertEqual(state.get("x1"), 1, "entangled qubits disagreed: %r" % state)
            seen.add(state.get("x10", 0))
        self.assertEqual(seen, {0, 1}, "the measurement never varied")

    def test_qubit_index_out_of_range_is_reported(self):
        with self.assertRaises(ValueError):
            run("li x28, 1\nqinit x28\nli x28, 5\nqh x28\n")

    def test_gate_before_qinit_is_reported(self):
        with self.assertRaises(RuntimeError):
            run("li x28, 0\nqh x28\n")


class UnifiedStreamTests(unittest.TestCase):
    SHOTS = 400

    def setUp(self):
        self.source = example("hybrid_bell.hqasm")
        self.assembly = compile_unified(self.source)

    def test_stream_contains_both_halves(self):
        text = self.assembly
        self.assertIn("qinit", text)
        self.assertIn("qmeas x10", text)   # c[0] lands where L3 injects it
        self.assertIn("bne x10", text)     # and the branch reads it
        self.assertIn("qcx", text)

    def test_branch_follows_the_actual_measurement(self):
        outcomes = collections.Counter()
        for seed in range(self.SHOTS):
            state = run(self.assembly, seed=seed)
            c0 = state.get("x10", 0)
            c1 = state.get("x11", 0)
            outcomes[c0] += 1
            self.assertEqual(c0, c1, "entangled qubits disagreed: %r" % state)
            self.assertEqual(state.get("x1", 0), 105 if c0 else 15, state)
            self.assertEqual(state.get("x2", 0), 0, state)
        self.assertEqual(set(outcomes), {0, 1}, "the measurement never varied")

    def test_scratch_registers_are_cleared(self):
        for seed in range(20):
            state = run(self.assembly, seed=seed)
            self.assertFalse(
                set(state) - {"x1", "x2", "x10", "x11"},
                "unexpected registers survived: %r" % state,
            )

    def test_word_form_executes_identically(self):
        words = to_words(self.assembly)
        self.assertIn(".word 0x", words)
        for seed in range(10):
            self.assertEqual(run(self.assembly, seed=seed), run(words, seed=seed))

    def test_listing_renders(self):
        text = listing(self.assembly)
        self.assertIn("f3=0b", text)
        self.assertIn("qmeas", text)

    def test_whitelist_program_matches_the_reference_distribution(self):
        source = example("hybrid_whitelist.hqasm")
        assembly = compile_unified(source)

        quantum_source, _ = split_classical_blocks(source)
        circuit = parse_qasm(quantum_source)
        width = measurement_width(circuit)
        expected = ideal_distribution(circuit, width)

        shots = 3000
        counts = collections.Counter()
        for seed in range(shots):
            state = run(assembly, seed=seed)
            value = sum(1 << bit for bit in range(width) if state.get("x%d" % (10 + bit), 0))
            key = "".join("1" if (value >> b) & 1 else "0" for b in range(width - 1, -1, -1))
            counts[key] += 1
            # the nested classical block must agree with the bits that landed
            c0 = (value >> 0) & 1
            c2 = (value >> 2) & 1
            self.assertEqual(state.get("x1", 0), (3 if c2 else 2) if c0 else (1 if c2 else 0))
            self.assertEqual(state.get("x2", 0), bin(value).count("1"))

        observed = {key: value / float(shots) for key, value in counts.items()}
        fidelity = hellinger_fidelity(observed, expected)
        self.assertGreaterEqual(
            fidelity, 0.97, "sampled %r against reference %r" % (observed, expected)
        )

    def test_register_budget_is_respected(self):
        """Quantum operands must never land on a classical variable register."""
        for register in QUBIT_REGISTERS + (ANGLE_REGISTER,):
            self.assertGreater(register, 19)
            self.assertLessEqual(register, 31)


if __name__ == "__main__":
    unittest.main()
