#!/usr/bin/env python3
"""LoomQ submission adapter — the four functions the evaluator calls.

This file is intentionally a thin façade.  All behaviour lives in the ``loomq``
package next to it:

    loomq/qasm       OpenQASM 2.0 front end
    loomq/passes     gate lowering onto the twelve-gate basis
    loomq/emitters   native IR for spinq / originq / braket
    loomq/backends   vendor SDK adapters + the reference simulator
    loomq/execution  the pipeline that ties those together
    loomq/agent      L2: the natural-language agent
    loomq/hybrid     L3: Hybrid-QASM -> quantum ops + RISC-V assembly

Keeping the contract surface separate from the implementation means the
evaluator's import never drags in an optional dependency, and a reviewer can see
the whole submission interface on one screen.
"""

import os
import sys
from typing import Any, Dict, List, Tuple

# The evaluator imports this module either as ``starter_kit.adapter`` or as a
# top-level ``adapter``; make ``loomq`` importable in both cases.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from loomq.execution import run_circuit, transpile_qasm  # noqa: E402

SUPPORTED_TARGETS = ("spinq", "originq", "braket")


def transpile(qasm_str: str, target: str) -> str:
    """Translate OpenQASM 2.0 into the target backend's native representation.

    ``spinq`` -> OpenQASM 2.0, ``braket`` -> OpenQASM 3, ``originq`` -> OriginIR,
    each a complete and executable program as defined by
    ``starter_kit/target_ir_contract.md``.
    """
    return transpile_qasm(qasm_str, target)


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute a circuit and return the unified result schema from the rules."""
    return run_circuit(qasm_str, target, shots)


def agent_chat(prompt: str) -> str:
    """L2 entry point, configured entirely from the ``LOOMQ_LLM_*`` environment."""
    from loomq.agent import agent_chat as _agent_chat

    return _agent_chat(prompt)


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """L3 entry point. Return quantum operations and RISC-V assembly."""
    from loomq.hybrid import compile_hybrid as _compile_hybrid

    return _compile_hybrid(hybrid_qasm_str)
