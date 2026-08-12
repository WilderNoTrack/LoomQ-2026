"""A random Hybrid-QASM generator, matching the grammar in the rules.

Formal L3 scoring generates its own cases ("different branch structures,
different constants, different measurement-bit counts") and exhaustively injects
every measurement combination.  Rather than hope the published example is
representative, LoomQ generates the same shape locally and runs the same
exhaustive check — see :func:`loomq.hybrid.verify`.
"""

import random
from typing import List, Optional

_MAX_VARIABLE = 9


class HybridGenerator(object):
    def __init__(self, seed: int = 0, max_depth: int = 3, max_variables: int = 4) -> None:
        self.rng = random.Random(seed)
        self.max_depth = max_depth
        self.max_variables = min(max_variables, _MAX_VARIABLE)

    # ----------------------------------------------------------- expressions

    def constant(self) -> str:
        return str(self.rng.randint(-50, 200))

    def variable(self) -> str:
        return "r%d" % self.rng.randint(1, self.max_variables)

    def measurement(self, bits: int) -> str:
        return "c[%d]" % self.rng.randrange(bits)

    def atom(self, bits: int) -> str:
        choice = self.rng.random()
        if choice < 0.4:
            return self.constant()
        if choice < 0.75:
            return self.variable()
        return self.measurement(bits)

    def expression(self, bits: int, terms: Optional[int] = None) -> str:
        count = terms if terms is not None else self.rng.randint(1, 3)
        parts = [self.atom(bits)]
        for _ in range(count - 1):
            parts.append(self.rng.choice(("+", "-")))
            parts.append(self.atom(bits))
        return " ".join(parts)

    def condition(self, bits: int) -> str:
        return "%s %s %s" % (
            self.expression(bits, terms=self.rng.randint(1, 2)),
            self.rng.choice(("==", "!=")),
            self.expression(bits, terms=self.rng.randint(1, 2)),
        )

    # ------------------------------------------------------------ statements

    def assignment(self, bits: int, indent: str) -> List[str]:
        return ["%s%s = %s;" % (indent, self.variable(), self.expression(bits))]

    def statement(self, bits: int, depth: int, indent: str) -> List[str]:
        if depth < self.max_depth and self.rng.random() < 0.45:
            return self.conditional(bits, depth, indent)
        return self.assignment(bits, indent)

    def conditional(self, bits: int, depth: int, indent: str) -> List[str]:
        inner = indent + "  "
        lines = ["%sif (%s) {" % (indent, self.condition(bits))]
        for _ in range(self.rng.randint(1, 2)):
            lines.extend(self.statement(bits, depth + 1, inner))
        lines.append("%s}" % indent)
        if self.rng.random() < 0.7:
            lines[-1] = "%s} else {" % indent
            for _ in range(self.rng.randint(1, 2)):
                lines.extend(self.statement(bits, depth + 1, inner))
            lines.append("%s}" % indent)
        return lines

    # --------------------------------------------------------------- program

    def program(self, bits: Optional[int] = None) -> str:
        count = bits if bits is not None else self.rng.randint(1, 3)
        qubits = max(count, 1)

        lines = [
            "OPENQASM 2.0;",
            'include "qelib1.inc";',
            "qreg q[%d];" % qubits,
            "creg c[%d];" % count,
        ]
        for index in range(qubits):
            if self.rng.random() < 0.7:
                lines.append("h q[%d];" % index)
        for index in range(count):
            lines.append("measure q[%d] -> c[%d];" % (index, index))

        lines.append("classical {")
        for _ in range(self.rng.randint(1, 4)):
            lines.extend(self.statement(count, 0, "  "))
        lines.append("}")

        if self.rng.random() < 0.5 and qubits >= 2:
            lines.append("cx q[0], q[1];")
        if self.rng.random() < 0.3:
            lines.append("x q[0];")
        return "\n".join(lines) + "\n"


def generate(seed: int = 0, **kwargs) -> str:
    return HybridGenerator(seed=seed, **kwargs).program()


__all__ = ["HybridGenerator", "generate"]
