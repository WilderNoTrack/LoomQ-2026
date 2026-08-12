"""A direct interpreter for the classical AST.

This is the semantic reference the generated assembly is checked against.  The
test suite runs both over every measurement injection of every generated case
and requires the final register states to agree exactly — the same comparison
the formal L3 evaluator performs, run locally against randomly generated
programs rather than the published example.
"""

from typing import Dict, Mapping, Sequence

from ..errors import HybridQasmError
from .ast import (
    Assign,
    BinaryOp,
    Comparison,
    Const,
    If,
    MeasurementBit,
    Node,
    Program,
    Variable,
)


def evaluate_expression(node: Node, variables: Dict[int, int], measurements: Mapping[int, int]) -> int:
    if isinstance(node, Const):
        return node.value
    if isinstance(node, Variable):
        return variables.get(node.index, 0)
    if isinstance(node, MeasurementBit):
        return int(measurements.get(node.index, 0))
    if isinstance(node, BinaryOp):
        left = evaluate_expression(node.left, variables, measurements)
        right = evaluate_expression(node.right, variables, measurements)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        raise HybridQasmError("unsupported operator %r" % node.op)
    raise HybridQasmError("cannot evaluate %r" % (node,))


def evaluate_condition(
    node: Comparison, variables: Dict[int, int], measurements: Mapping[int, int]
) -> bool:
    left = evaluate_expression(node.left, variables, measurements)
    right = evaluate_expression(node.right, variables, measurements)
    if node.op == "==":
        return left == right
    if node.op == "!=":
        return left != right
    raise HybridQasmError("unsupported comparison %r" % node.op)


def execute(program: Program, measurements: Mapping[int, int]) -> Dict[int, int]:
    """Run the classical program, returning ``{variable index: value}``."""
    variables = {}  # type: Dict[int, int]
    _run(program.body, variables, measurements)
    return variables


def _run(body: Sequence[Node], variables: Dict[int, int], measurements: Mapping[int, int]) -> None:
    for statement in body:
        if isinstance(statement, Assign):
            variables[statement.target.index] = evaluate_expression(
                statement.value, variables, measurements
            )
        elif isinstance(statement, If):
            if evaluate_condition(statement.condition, variables, measurements):
                _run(statement.then_body, variables, measurements)
            else:
                _run(statement.else_body, variables, measurements)
        elif isinstance(statement, Program):
            _run(statement.body, variables, measurements)
        else:  # pragma: no cover - defensive
            raise HybridQasmError("cannot execute %r" % (statement,))


def register_state(program: Program, measurements: Mapping[int, int]) -> Dict[str, int]:
    """Final state in ``TinyRISCVEmulator`` form: non-zero registers only."""
    variables = execute(program, measurements)
    state = {"x%d" % index: value for index, value in variables.items() if value != 0}
    for index, value in measurements.items():
        if value:
            state["x%d" % (10 + index)] = int(value)
    return state


__all__ = ["evaluate_condition", "evaluate_expression", "execute", "register_state"]
