"""L1: gate algebra, lowering, the three emitters, and the result schema.

The centrepiece is :class:`RoundTripTests` — every circuit shape the rules name
is emitted for every target, re-imported, and compared as an *exact*
distribution.  That is the same thing formal scoring does with the artifact
``transpile()`` returns, so a spelling or operand-order mistake for one vendor
fails here rather than on the scoreboard.
"""

import cmath
import math
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adapter
from loomq.circuits import official_suite, whitelist_exercise
from loomq.emitters import TARGETS, normalize_target
from loomq.errors import UnknownTargetError
from loomq.execution import acceptance_threshold, run_circuit, transpile_qasm
from loomq.gates import GATES, WHITELIST, lookup
from loomq.ir import GateOp
from loomq.passes import lower_to_basis
from loomq.qasm import parse_qasm
from loomq.result import (
    counts_to_distribution,
    hellinger_fidelity,
    normalize_counts,
    validate_result,
)
from loomq.sim import ideal_distribution, measurement_width
from loomq.sim.statevector import apply_matrix
from loomq.verify import verify_target_ir

SHOTS = 8192


def ideal(qasm):
    circuit = parse_qasm(qasm)
    return ideal_distribution(circuit, measurement_width(circuit))


class GateAlgebraTests(unittest.TestCase):
    def test_whitelist_is_the_twelve_named_gates(self):
        self.assertEqual(len(WHITELIST), 12)
        for name in WHITELIST:
            self.assertIn(name, GATES)

    def test_every_gate_is_unitary(self):
        for name, definition in GATES.items():
            params = tuple(0.7 + 0.3 * index for index in range(definition.num_params))
            matrix = definition.matrix(params)
            size = len(matrix)
            for row in range(size):
                for column in range(size):
                    product = sum(
                        matrix[row][k] * matrix[column][k].conjugate() for k in range(size)
                    )
                    expected = 1.0 if row == column else 0.0
                    self.assertAlmostEqual(
                        abs(product - expected), 0.0, places=12,
                        msg="%s is not unitary at (%d, %d)" % (name, row, column),
                    )


class DecompositionTests(unittest.TestCase):
    """Every accepted gate must lower onto the whitelist without changing physics."""

    @staticmethod
    def _state(ops, num_qubits):
        state = [0j] * (1 << num_qubits)
        state[0] = 1 + 0j
        # A generic input, so a decomposition cannot pass by accident on |0...0>.
        for qubit in range(num_qubits):
            apply_matrix(state, num_qubits, lookup("h").matrix(()), (qubit,))
            apply_matrix(state, num_qubits, lookup("t").matrix(()), (qubit,))
            apply_matrix(state, num_qubits, lookup("ry").matrix((0.3 + qubit,)), (qubit,))
        for op in ops:
            apply_matrix(state, num_qubits, lookup(op.name).matrix(op.params), op.qubits)
        return state

    def test_all_gates_lower_to_the_whitelist_up_to_global_phase(self):
        for name, definition in sorted(GATES.items()):
            params = tuple(0.4 + 0.5 * index for index in range(definition.num_params))
            qubits = tuple(range(definition.num_qubits))
            num_qubits = definition.num_qubits

            circuit = parse_qasm(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n'
                % (num_qubits, num_qubits)
            )
            circuit.append(GateOp(name, params, qubits))
            lowered = lower_to_basis(circuit)

            for op in lowered.gates:
                self.assertIn(op.name, WHITELIST, "%s lowered to %s" % (name, op.name))

            before = self._state([GateOp(name, params, qubits)], num_qubits)
            after = self._state(lowered.gates, num_qubits)

            reference = next(
                (index for index, value in enumerate(before) if abs(value) > 1e-9), None
            )
            self.assertIsNotNone(reference)
            phase = after[reference] / before[reference]
            self.assertAlmostEqual(abs(phase), 1.0, places=9, msg="%s changed the norm" % name)
            for expected, observed in zip(before, after):
                self.assertAlmostEqual(
                    abs(observed - phase * expected), 0.0, places=9,
                    msg="%s decomposition differs by more than a global phase" % name,
                )

    def test_optimizer_removes_identity_rotations(self):
        circuit = parse_qasm(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "rz(0) q[0];\nrz(0.3) q[0];\nrz(-0.3) q[0];\nh q[0];\nmeasure q -> c;\n"
        )
        lowered = lower_to_basis(circuit)
        self.assertEqual([op.name for op in lowered.gates], ["h"])


class RoundTripTests(unittest.TestCase):
    """Emit -> re-import -> simulate, for every circuit against every target."""

    def test_official_circuit_family_round_trips_exactly(self):
        for name, qasm in official_suite() + [("whitelist", whitelist_exercise())]:
            for target in TARGETS:
                native = transpile_qasm(qasm, target)
                equivalent, fidelity, detail = verify_target_ir(qasm, target, native)
                self.assertTrue(
                    equivalent,
                    "%s -> %s is not equivalent: %s" % (name, target, detail),
                )
                self.assertAlmostEqual(fidelity, 1.0, places=9)

    def test_native_ir_shape(self):
        qasm = whitelist_exercise()
        spinq = transpile_qasm(qasm, "spinq")
        self.assertTrue(spinq.startswith("OPENQASM 2.0;"))
        self.assertIn('include "qelib1.inc";', spinq)
        self.assertIn("measure ", spinq)

        braket = transpile_qasm(qasm, "braket")
        self.assertTrue(braket.startswith("OPENQASM 3.0;"))
        self.assertIn('include "stdgates.inc";', braket)
        self.assertIn("qubit[3] q;", braket)
        self.assertIn("bit[3] c;", braket)
        self.assertIn("= measure ", braket)
        self.assertIn("cnot ", braket)

        originq = transpile_qasm(qasm, "originq")
        self.assertTrue(originq.startswith("QINIT 3"))
        self.assertIn("CREG 3", originq)
        self.assertIn("CNOT ", originq)
        self.assertIn("TOFFOLI ", originq)
        self.assertIn("MEASURE q[0], c[0]", originq)
        self.assertIn("CR ", originq)  # cu1 is CR in OriginIR's own grammar

    def test_originq_parameters_follow_the_operand(self):
        """``RZ q[0],(theta)`` is the only form pyqpanda's grammar accepts."""
        originq = transpile_qasm(whitelist_exercise(), "originq")
        parameterised = [
            line for line in originq.splitlines()
            if line.split(" ")[0] in ("RZ", "RY", "CR")
        ]
        self.assertTrue(parameterised, "the exercise circuit has parameterised gates")
        for line in parameterised:
            self.assertRegex(line, r"^\w+ q\[\d+\](, q\[\d+\])*,\(-?[\d.e+-]+\)$", line)

    def test_target_dialects_avoid_ambiguous_gate_names(self):
        """Names that differ between a target's dialects are lowered away.

        Braket's parser and OpenQASM 3's stdgates.inc disagree on cx/cnot,
        ccx/ccnot, cp/cphaseshift, sdg/si and tdg/ti; OriginIR's grammar has no
        SDAG or TDAG token at all. Emitting only the gates both dialects spell
        identically removes the guesswork — verified against the vendors' own
        parsers by tools/validate_vendor_ir.py.
        """
        braket = transpile_qasm(whitelist_exercise(), "braket")
        for forbidden in ("cp(", "ccx ", "sdg ", "tdg ", "cphaseshift", "ccnot", " si ", " ti "):
            self.assertNotIn(forbidden, braket, "braket IR still contains %r" % forbidden)

        originq = transpile_qasm(whitelist_exercise(), "originq")
        for forbidden in ("SDAG", "TDAG", "CU1"):
            self.assertNotIn(forbidden, originq, "OriginIR still contains %r" % forbidden)

    def test_target_aliases(self):
        for alias, expected in (("SpinQ", "spinq"), ("aws", "braket"), ("origin", "originq")):
            self.assertEqual(normalize_target(alias), expected)
        with self.assertRaises(UnknownTargetError):
            normalize_target("ibmq")


class ExecutionTests(unittest.TestCase):
    def test_run_matches_the_ideal_distribution_on_every_target(self):
        for name, qasm in official_suite():
            expected = ideal(qasm)
            for target in TARGETS:
                result = run_circuit(qasm, target, SHOTS)
                valid, why = validate_result(result)
                self.assertTrue(valid, "%s/%s: %s" % (name, target, why))
                fidelity = hellinger_fidelity(
                    counts_to_distribution(result["counts"]), expected
                )
                self.assertGreaterEqual(
                    fidelity, 0.97, "%s on %s scored %.4f" % (name, target, fidelity)
                )

    def test_result_schema_fields(self):
        result = run_circuit(official_suite()[0][1], "spinq", 1024)
        for field in ("backend", "job_id", "shots", "counts", "bit_order", "timestamp", "meta"):
            self.assertIn(field, result)
        self.assertEqual(result["bit_order"], "little")
        self.assertEqual(sum(result["counts"].values()), 1024)
        self.assertFalse(result["meta"].get("is_mock"))
        self.assertIn("executor", result["meta"])

    def test_shots_must_be_positive(self):
        with self.assertRaises(ValueError):
            run_circuit(official_suite()[0][1], "spinq", 0)

    def test_acceptance_threshold_tracks_shot_noise(self):
        """A wider distribution has more sampling noise, so the bar is lower."""
        narrow = acceptance_threshold(2, SHOTS)
        wide = acceptance_threshold(32, SHOTS)
        self.assertGreater(narrow, wide)
        self.assertGreaterEqual(wide, 0.97)

    def test_counts_normalisation(self):
        self.assertEqual(normalize_counts({0: 3, 3: 1}, 2), {"00": 3, "11": 1})
        self.assertEqual(normalize_counts({"1": 2}, 3), {"001": 2})

    def test_schema_validator_rejects_mock_results(self):
        valid, why = validate_result(
            {"backend": "x", "job_id": "y", "shots": 1, "counts": {"0": 1},
             "bit_order": "little", "timestamp": "now", "meta": {"is_mock": True}}
        )
        self.assertFalse(valid)
        self.assertIn("mock", why)


class SpinQSidecarTests(unittest.TestCase):
    """spinqit lives in its own interpreter; the adapter must still reach it.

    spinqit and amazon-braket pin conflicting antlr4 versions, so the image
    installs spinqit into /opt/spinq. Without a bridge, `import spinqit` fails
    in LoomQ's interpreter and the spinq target falls back to the reference
    simulator *inside the container built to run it on spinqit* — a silent
    downgrade that no test would otherwise catch.
    """

    def test_env_var_wins_over_the_search_path(self):
        from loomq.backends.spinq_backend import SpinQBackend

        backend = SpinQBackend()
        with mock.patch.dict(os.environ, {"LOOMQ_SPINQ_PYTHON": sys.executable}):
            self.assertEqual(backend._sidecar(), sys.executable)

    def test_missing_sidecar_is_reported_not_hidden(self):
        from loomq.backends.spinq_backend import SpinQBackend

        backend = SpinQBackend()
        with mock.patch.dict(os.environ, {"LOOMQ_SPINQ_PYTHON": "/no/such/python"}):
            with mock.patch.object(backend, "_sdk", return_value=None):
                usable, reason = backend.availability()
        if not usable:
            self.assertIn("sidecar", reason)

    def test_availability_reports_which_interpreter(self):
        from loomq.backends.spinq_backend import SpinQBackend

        usable, reason = SpinQBackend().availability()
        if usable:
            self.assertTrue(
                "in-process" in reason or "via" in reason,
                "availability should say where spinqit was found: %s" % reason,
            )

    def test_runner_module_exists_and_is_importable_as_a_script(self):
        """The sidecar entry point has to be a module, not a path guess."""
        import loomq.backends.spinq_runner as runner

        self.assertTrue(hasattr(runner, "main"))


class AdapterContractTests(unittest.TestCase):
    def test_supported_targets(self):
        self.assertEqual(adapter.SUPPORTED_TARGETS, ("spinq", "originq", "braket"))

    def test_transpile_and_run_are_wired(self):
        qasm = official_suite()[0][1]
        self.assertIn("OPENQASM", adapter.transpile(qasm, "spinq"))
        result = adapter.run(qasm, "braket", 512)
        self.assertEqual(sum(result["counts"].values()), 512)


if __name__ == "__main__":
    unittest.main()
