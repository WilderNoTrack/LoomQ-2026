"""L2: the agent, driven against a local OpenAI-compatible stub.

No network and no credentials are needed. The stub replaces the model's
*understanding* step with a fixed specification, which is exactly the seam the
design creates: everything after that step is deterministic, so it can be
asserted on. The tests check the two things scoring checks — that the reply
contains a program the official extractor can find, and that the program's
distribution is the one the request asked for.
"""

import json
import os
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loomq.agent import respond
from loomq.agent.llm import LLMClient, LLMConfig, extract_json
from loomq.agent.selection import constraints_from_text, format_answer, recommend
from loomq.agent.synthesis import canonical_family, synthesize
from loomq.capabilities import backend_ids
from loomq.errors import LLMConfigurationError
from loomq.qasm import parse_qasm
from loomq.sim import ideal_distribution, measurement_width

#: The official public extractor, copied from starter_kit/evaluator.py.
EXTRACT = re.compile(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", re.DOTALL | re.MULTILINE)

GOOD_BELL = ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[2];\n'
             'h q[0];\ncx q[0],q[1];\nmeasure q -> c;\n')


class _Stub(BaseHTTPRequestHandler):
    script = []       # list of (needle, reply); first match wins
    requests = []

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        user = payload["messages"][-1]["content"]
        content = "{}"
        for needle, reply in type(self).script:
            if needle in user:
                content = reply
                break
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AgentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.environment = {
            "LOOMQ_LLM_BASE_URL": "http://127.0.0.1:%d" % cls.server.server_port,
            "LOOMQ_LLM_API_KEY": "stub-key",
            "LOOMQ_LLM_MODEL": "stub-model",
            "LOOMQ_LLM_TIMEOUT_SECONDS": "20",
        }

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        _Stub.script = []
        _Stub.requests = []

    def ask(self, prompt, script):
        _Stub.script = script
        with mock.patch.dict(os.environ, self.environment, clear=False):
            return respond(prompt)

    def extracted_distribution(self, text):
        match = EXTRACT.search(text)
        self.assertIsNotNone(match, "the official extractor found no program in:\n" + text)
        circuit = parse_qasm(match.group(0))
        return ideal_distribution(circuit, measurement_width(circuit))


class GenerationTests(AgentTestCase):
    def test_ghz_is_synthesised_and_verified(self):
        result = self.ask(
            "generate a 3 qubit GHZ state and measure everything",
            [("GHZ", json.dumps({
                "task": "generate", "language": "en", "num_qubits": 3,
                "state": {"family": "ghz"}, "explanation": "GHZ entangles all three.",
                "qasm": GOOD_BELL,  # deliberately wrong; synthesis must win
            }))],
        )
        self.assertEqual(result.trace["route"], "synthesised")
        distribution = self.extracted_distribution(result.text)
        self.assertEqual({k: round(v, 6) for k, v in distribution.items()},
                         {"000": 0.5, "111": 0.5})

    def test_repair_rebuilds_from_the_stated_intent(self):
        result = self.ask(
            "I wanted a Bell state but this fails: H q[0]; CX q[0] q[1]",
            [("Bell", json.dumps({
                "task": "repair", "language": "en", "num_qubits": 2,
                "state": {"family": "bell", "variant": "phi_plus"},
                "explanation": "Registers were missing and the gate names were upper case.",
                "qasm": GOOD_BELL,
            }))],
        )
        distribution = self.extracted_distribution(result.text)
        self.assertEqual({k: round(v, 6) for k, v in distribution.items()},
                         {"00": 0.5, "11": 0.5})

    def test_w_state(self):
        result = self.ask(
            "give me a 3 qubit W state",
            [("W state", json.dumps({
                "task": "generate", "language": "en", "num_qubits": 3,
                "state": {"family": "w"}, "explanation": "One excitation, spread out.",
            }))],
        )
        distribution = self.extracted_distribution(result.text)
        self.assertEqual(len(distribution), 3)
        for key, value in distribution.items():
            self.assertEqual(key.count("1"), 1)
            self.assertAlmostEqual(value, 1.0 / 3.0, places=9)

    def test_basis_state_bit_order(self):
        result = self.ask(
            "prepare |101> and measure it",
            [("101", json.dumps({
                "task": "generate", "language": "en", "num_qubits": 3,
                "state": {"family": "basis", "basis_state": "101"}, "explanation": "",
            }))],
        )
        self.assertEqual(self.extracted_distribution(result.text), {"101": 1.0})

    def test_custom_circuit_is_repaired_then_verified(self):
        result = self.ask(
            "make me an unusual two qubit circuit that lands half on 00 and half on 11",
            [
                ("LoomQ diagnostic", GOOD_BELL),
                ("unusual", json.dumps({
                    "task": "generate", "language": "en", "num_qubits": 2,
                    "state": {"family": "custom"},
                    "expected_counts": {"00": 0.5, "11": 0.5},
                    "qasm": "OPENQASM 2.0;\nqreg q[2];\nH q[0];\nCX q[0] q[1];\n",
                    "explanation": "A custom circuit.",
                })),
            ],
        )
        self.assertEqual(result.trace["route"], "model+verified")
        self.assertTrue(result.trace.get("repairs"))
        self.assertEqual({k: round(v, 6) for k, v in
                          self.extracted_distribution(result.text).items()},
                         {"00": 0.5, "11": 0.5})

    def test_unrecoverable_circuit_never_leaks_raw_json(self):
        result = self.ask(
            "give me something nonsensical",
            [
                ("LoomQ diagnostic", "still broken"),
                ("nonsensical", json.dumps({
                    "task": "generate", "language": "en", "num_qubits": 2,
                    "state": {"family": "custom"},
                    "qasm": "OPENQASM 2.0;\nqreg q[2];\nnonsense q[0];\n",
                    "explanation": "This cannot be built.",
                })),
            ],
        )
        self.assertFalse(result.text.strip().startswith("{"))
        self.assertNotIn('"task"', result.text)

    def test_a_model_call_is_always_made(self):
        """A case only scores if the submission actually called the model."""
        self.ask("anything", [("anything", json.dumps({"task": "explain"}))])
        self.assertTrue(_Stub.requests)
        self.assertEqual(_Stub.requests[0]["temperature"], 0)
        self.assertIs(_Stub.requests[0]["stream"], False)


class SelectionTests(AgentTestCase):
    def test_fifteen_qubits_no_queue(self):
        result = self.ask(
            "I need to run a 15 qubit circuit with zero queue time, which platform?",
            [("15", json.dumps({
                "task": "select_backend", "language": "en",
                "constraints": {"min_qubits": 15, "no_queue": True},
            }))],
        )
        for expected in ("spinq_taurus_simulator", "originq_local_simulator",
                         "braket_local_simulator"):
            self.assertIn(expected, result.text)
        self.assertNotIn("spinq_cloud_qpu", result.text)

    def test_free_hardware(self):
        result = self.ask(
            "run a 5 qubit circuit on real hardware without paying",
            [("real hardware", json.dumps({
                "task": "select_backend", "language": "en",
                "constraints": {"min_qubits": 5, "kind": "hardware", "max_cost": "free_quota"},
            }))],
        )
        self.assertIn("spinq_cloud_qpu", result.text)
        self.assertIn("originq_wukong", result.text)
        self.assertNotIn("braket_cloud", result.text)

    def test_impossible_request_is_answered_honestly(self):
        result = self.ask(
            "a 50 qubit circuit with no queue",
            [("50", json.dumps({
                "task": "select_backend", "language": "en",
                "constraints": {"min_qubits": 50, "no_queue": True},
            }))],
        )
        self.assertIn("originq_wukong", result.text)   # the closest alternative
        self.assertIn("no_queue", result.text)          # and what to relax

    def test_table_filtering_without_a_model(self):
        matches, _ = recommend({"min_qubits": 15, "no_queue": True})
        self.assertEqual(
            sorted(entry["id"] for entry in matches),
            ["braket_local_simulator", "originq_local_simulator", "spinq_taurus_simulator"],
        )

    def test_local_constraint_reading_covers_both_languages(self):
        self.assertEqual(
            constraints_from_text("我需要 15 比特，零排队"),
            {"min_qubits": 15, "no_queue": True},
        )
        english = constraints_from_text("a 12-qubit job on real hardware, free please")
        self.assertEqual(english["min_qubits"], 12)
        self.assertEqual(english["kind"], "hardware")
        self.assertEqual(english["max_cost"], "free_quota")

    def test_every_answer_names_a_canonical_id(self):
        known = set(backend_ids())
        for constraints in (
            {"min_qubits": 2},
            {"kind": "simulator"},
            {"kind": "hardware", "max_cost": "free_quota"},
            {"no_queue": True, "min_qubits": 20},
        ):
            answer = format_answer(constraints, "en")
            self.assertTrue(
                any(name in answer for name in known),
                "no canonical backend id in: %s" % answer,
            )


class OfflineTests(unittest.TestCase):
    def test_missing_configuration_is_reported_without_the_key(self):
        with mock.patch.dict(os.environ, {"SECRET_TOKEN": "do-not-echo"}, clear=True):
            result = respond("做一个贝尔态")
        self.assertNotIn("do-not-echo", result.text)
        self.assertIn("LOOMQ_LLM_BASE_URL", result.text)

    def test_backend_questions_still_work_offline(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = respond("I need 15 qubits with no queue")
        self.assertIn("braket_local_simulator", result.text)

    def test_configuration_errors_never_contain_the_credential(self):
        with mock.patch.dict(
            os.environ,
            {"LOOMQ_LLM_BASE_URL": "http://x", "LOOMQ_LLM_API_KEY": "sk-super-secret",
             "LOOMQ_LLM_TIMEOUT_SECONDS": "not-a-number"},
            clear=True,
        ):
            from loomq.agent.llm import load_config

            with self.assertRaises(LLMConfigurationError) as caught:
                load_config()
        self.assertNotIn("sk-super-secret", str(caught.exception))

    def test_redacted_config_hides_the_key(self):
        config = LLMConfig("http://x", "sk-secret-value", "m", 10.0, 100)
        self.assertNotIn("sk-secret-value", json.dumps(config.redacted()))


class HelperTests(unittest.TestCase):
    def test_family_aliases(self):
        self.assertEqual(canonical_family("maximally-entangled"), "ghz")
        self.assertEqual(canonical_family("EPR"), "bell")
        self.assertEqual(canonical_family("equal superposition"), "uniform")
        self.assertIsNone(canonical_family("something else"))

    def test_synthesis_declines_unknown_families(self):
        self.assertIsNone(synthesize({"family": "custom", "num_qubits": 2}))

    def test_json_extraction_survives_fences_and_prose(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('sure!\n{"a": {"b": 2}}\nhope that helps'),
                         {"a": {"b": 2}})


if __name__ == "__main__":
    unittest.main()
