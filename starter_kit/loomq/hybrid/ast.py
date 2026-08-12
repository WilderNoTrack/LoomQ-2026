"""The classical mini-language's abstract syntax tree.

Deliberately tiny — integer literals, ``r1..r9``, ``c[k]``, ``+ - == !=``,
``if``/``else`` and sequential assignment — and deliberately a real tree.  The
grammar is small enough that a pattern-matching "compiler" could fake the
published example, and the rules say outright that randomly generated cases will
punish exactly that.  Everything downstream (code generation, the reference
interpreter) walks this tree, so both agree by construction.
"""

from typing import List, Optional, Sequence

#: ``r1..r9`` map onto RISC-V ``x1..x9``.
FIRST_VARIABLE_REGISTER = 1
LAST_VARIABLE_REGISTER = 9

#: ``c[0], c[1], ...`` are injected into ``x10, x11, ...`` by the evaluator.
FIRST_MEASUREMENT_REGISTER = 10


class Node(object):
    __slots__ = ()


# ---------------------------------------------------------------- expressions


class Const(Node):
    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = int(value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Const(%d)" % self.value


class Variable(Node):
    """``rN`` — a classical variable living in ``xN``."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = int(index)

    @property
    def register(self) -> int:
        return self.index

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "r%d" % self.index


class MeasurementBit(Node):
    """``c[k]`` — a measurement outcome, injected into ``x(10 + k)``."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = int(index)

    @property
    def register(self) -> int:
        return FIRST_MEASUREMENT_REGISTER + self.index

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "c[%d]" % self.index


class BinaryOp(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Node, right: Node) -> None:
        self.op = op
        self.left = left
        self.right = right

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "(%r %s %r)" % (self.left, self.op, self.right)


class Comparison(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Node, right: Node) -> None:
        self.op = op  # '==' or '!='
        self.left = left
        self.right = right

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "(%r %s %r)" % (self.left, self.op, self.right)


# ----------------------------------------------------------------- statements


class Assign(Node):
    __slots__ = ("target", "value")

    def __init__(self, target: Variable, value: Node) -> None:
        self.target = target
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "%r = %r" % (self.target, self.value)


class If(Node):
    __slots__ = ("condition", "then_body", "else_body")

    def __init__(
        self,
        condition: Comparison,
        then_body: Sequence[Node],
        else_body: Optional[Sequence[Node]] = None,
    ) -> None:
        self.condition = condition
        self.then_body = list(then_body)
        self.else_body = list(else_body or [])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "If(%r, then=%r, else=%r)" % (self.condition, self.then_body, self.else_body)


class Program(Node):
    """The concatenated body of every ``classical { ... }`` block, in order."""

    __slots__ = ("body",)

    def __init__(self, body: Sequence[Node]) -> None:
        self.body = list(body)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Program(%r)" % (self.body,)


def measurement_bits(node: Node) -> List[int]:
    """Every ``c[k]`` index the program reads, sorted and de-duplicated."""
    found = set()  # type: set

    def walk(current: Node) -> None:
        if isinstance(current, MeasurementBit):
            found.add(current.index)
        elif isinstance(current, (BinaryOp, Comparison)):
            walk(current.left)
            walk(current.right)
        elif isinstance(current, Assign):
            walk(current.value)
        elif isinstance(current, If):
            walk(current.condition)
            for statement in current.then_body:
                walk(statement)
            for statement in current.else_body:
                walk(statement)
        elif isinstance(current, Program):
            for statement in current.body:
                walk(statement)

    walk(node)
    return sorted(found)


def assigned_variables(node: Node) -> List[int]:
    found = set()  # type: set

    def walk(current: Node) -> None:
        if isinstance(current, Assign):
            found.add(current.target.index)
        elif isinstance(current, If):
            for statement in current.then_body:
                walk(statement)
            for statement in current.else_body:
                walk(statement)
        elif isinstance(current, Program):
            for statement in current.body:
                walk(statement)

    walk(node)
    return sorted(found)


__all__ = [
    "Assign",
    "BinaryOp",
    "Comparison",
    "Const",
    "FIRST_MEASUREMENT_REGISTER",
    "FIRST_VARIABLE_REGISTER",
    "If",
    "LAST_VARIABLE_REGISTER",
    "MeasurementBit",
    "Node",
    "Program",
    "Variable",
    "assigned_variables",
    "measurement_bits",
]
