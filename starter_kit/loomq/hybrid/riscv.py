"""Code generation for ``TinyRISCVEmulator``'s instruction subset.

Only ``li, add, sub, addi, beq, bne, j`` exist, so everything else is built:
comparisons become branch-around skeletons, ``mv`` becomes ``addi rd, rs, 0``,
and constant folding keeps ``r1 = r1 + 5`` a single ``addi``.

Two register-allocation rules make the output safe to hand to the evaluator:

*Measurement registers are read-only.*  ``x10, x11, ...`` carry the injected
measurement values, so scratch registers are allocated from ``x31`` downwards
and never overlap them.

*Scratch is zeroed on exit.*  ``TinyRISCVEmulator.execute()`` reports every
non-zero register, and the evaluator compares that dictionary against a
reference interpreter.  Leftover temporaries would show up as spurious entries,
so the epilogue clears every scratch register the program touched — the final
state contains ``x1..x9`` and the injected measurement registers, nothing else.

Evaluation order matters for correctness too: the right operand of a binary
expression is materialised into a temporary *before* the destination register is
written, so ``r1 = r2 + r1`` cannot lose ``r1``.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import HybridQasmError
from .ast import (
    Assign,
    BinaryOp,
    Comparison,
    Const,
    FIRST_MEASUREMENT_REGISTER,
    If,
    LAST_VARIABLE_REGISTER,
    MeasurementBit,
    Node,
    Program,
    Variable,
    measurement_bits,
)

#: Highest register the emulator implements.
TOP_REGISTER = 31


class _Allocator(object):
    """Hands out scratch registers from the top of the file downwards."""

    def __init__(self, reserved_top: int) -> None:
        if reserved_top >= TOP_REGISTER:
            raise HybridQasmError(
                "no scratch registers left: measurement bits already reach x%d" % reserved_top
            )
        self.pool = list(range(TOP_REGISTER, reserved_top, -1))
        self.used = []  # type: List[int]

    def allocate(self) -> int:
        if not self.pool:
            raise HybridQasmError("classical expression is too deeply nested to compile")
        register = self.pool.pop(0)
        if register not in self.used:
            self.used.append(register)
        return register

    def release(self, register: int) -> None:
        self.pool.insert(0, register)


class _Generator(object):
    def __init__(self, program: Program) -> None:
        bits = measurement_bits(program)
        reserved_top = FIRST_MEASUREMENT_REGISTER + max(bits) if bits else LAST_VARIABLE_REGISTER
        self.allocator = _Allocator(max(reserved_top, LAST_VARIABLE_REGISTER))
        self.lines = []  # type: List[str]
        self.label_counter = 0

    # ------------------------------------------------------------- emission

    def emit(self, text: str) -> None:
        self.lines.append("    " + text)

    def label(self, name: str) -> None:
        self.lines.append("%s:" % name)

    def next_labels(self) -> Tuple[str, str]:
        self.label_counter += 1
        index = self.label_counter
        return "LQ_ELSE_%d" % index, "LQ_END_%d" % index

    # ----------------------------------------------------------- expressions

    def source_register(self, node: Node) -> Optional[int]:
        """The register a leaf already lives in, if it is a leaf."""
        if isinstance(node, Variable):
            return node.register
        if isinstance(node, MeasurementBit):
            return node.register
        return None

    def constant_value(self, node: Node) -> Optional[int]:
        """Fold a constant-only subtree so ``r1 + 2 + 3`` becomes one ``addi``."""
        if isinstance(node, Const):
            return node.value
        if isinstance(node, BinaryOp):
            left = self.constant_value(node.left)
            right = self.constant_value(node.right)
            if left is None or right is None:
                return None
            return left + right if node.op == "+" else left - right
        return None

    def emit_expression(self, node: Node, destination: int) -> None:
        constant = self.constant_value(node)
        if constant is not None:
            self.emit("li x%d, %d" % (destination, constant))
            return

        register = self.source_register(node)
        if register is not None:
            if register != destination:  # `r1 = r1` needs no instruction at all
                self.emit("addi x%d, x%d, 0" % (destination, register))
            return

        if isinstance(node, BinaryOp):
            right_constant = self.constant_value(node.right)
            if right_constant is not None:
                self.emit_expression(node.left, destination)
                offset = right_constant if node.op == "+" else -right_constant
                if offset:
                    self.emit("addi x%d, x%d, %d" % (destination, destination, offset))
                return

            # Materialise the right operand first: the destination may alias a
            # variable the left operand still has to read.
            temporary = self.allocator.allocate()
            self.emit_expression(node.right, temporary)
            self.emit_expression(node.left, destination)
            mnemonic = "add" if node.op == "+" else "sub"
            self.emit("%s x%d, x%d, x%d" % (mnemonic, destination, destination, temporary))
            self.allocator.release(temporary)
            return

        raise HybridQasmError("cannot compile expression %r" % (node,))

    def emit_operand(self, node: Node) -> Tuple[int, Optional[int]]:
        """``(register, temporary_to_release)`` holding ``node``'s value."""
        constant = self.constant_value(node)
        if constant == 0:
            return 0, None  # x0 is hard-wired to zero
        register = self.source_register(node)
        if register is not None:
            return register, None
        temporary = self.allocator.allocate()
        self.emit_expression(node, temporary)
        return temporary, temporary

    # ------------------------------------------------------------ statements

    def emit_statements(self, body: Sequence[Node]) -> None:
        for statement in body:
            self.emit_statement(statement)

    def emit_statement(self, statement: Node) -> None:
        if isinstance(statement, Assign):
            self.emit_expression(statement.value, statement.target.register)
            return
        if isinstance(statement, If):
            self.emit_if(statement)
            return
        if isinstance(statement, Program):
            self.emit_statements(statement.body)
            return
        raise HybridQasmError("cannot compile statement %r" % (statement,))

    def emit_if(self, statement: If) -> None:
        condition = statement.condition
        if not isinstance(condition, Comparison):  # pragma: no cover - parser guarantees
            raise HybridQasmError("if-condition must be a comparison")

        else_label, end_label = self.next_labels()
        left, left_temp = self.emit_operand(condition.left)
        right, right_temp = self.emit_operand(condition.right)

        # Branch to the else arm when the condition is false.
        branch = "bne" if condition.op == "==" else "beq"
        self.emit("%s x%d, x%d, %s" % (branch, left, right, else_label))

        for temporary in (right_temp, left_temp):
            if temporary is not None:
                self.allocator.release(temporary)

        self.emit_statements(statement.then_body)
        self.emit("j %s" % end_label)
        self.label(else_label)
        self.emit_statements(statement.else_body)
        self.label(end_label)

    # ------------------------------------------------------------- assembly

    def generate(self, program: Program) -> str:
        self.lines.append("# LoomQ L3 classical control block")
        self.lines.append("# measurement bits arrive in x10, x11, ... and are read-only")
        self.emit_statements(program.body)

        if self.allocator.used:
            self.lines.append("# clear scratch so the final register state is exactly r1..r9")
            for register in sorted(self.allocator.used):
                self.emit("li x%d, 0" % register)
        if not any(line.strip() and not line.strip().startswith("#") for line in self.lines):
            self.emit("addi x0, x0, 0")
        return "\n".join(self.lines) + "\n"


def generate_assembly(program: Program) -> str:
    """Compile the classical AST into TinyRISCV assembly text."""
    generator = _Generator(program)
    return generator.generate(program)


__all__ = ["TOP_REGISTER", "generate_assembly"]
