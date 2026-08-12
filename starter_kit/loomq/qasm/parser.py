"""Recursive-descent parser for OpenQASM 2.0.

Scope: the full 2.0 statement grammar — registers, ``gate`` declarations with
inlining, parameter expressions, register broadcasting, ``measure``, ``reset``,
``barrier`` and ``if``.  User-defined gates are inlined at parse time, so every
later stage only ever sees the flat gate table in :mod:`loomq.gates`.

The parser is intentionally forgiving where forgiveness is harmless (a missing
``OPENQASM 2.0;`` header, ``cnot`` for ``cx``) and strict where a mistake would
silently change the physics (arity, register bounds, unknown gates).
"""

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import QasmError
from ..gates import canonical_name, is_known, signature
from ..ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, Register, ResetOp
from .lexer import Token, tokenize

#: ``parse_measure``/``parse_reset``/``parse_barrier`` hand back the operations
#: they appended so ``if`` can lift them into a :class:`ConditionalOp`.
Emitted = List[Operation]

_MAX_INLINE_DEPTH = 64

_FUNCTIONS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "exp": math.exp,
    "ln": math.log,
    "log": math.log,
    "sqrt": math.sqrt,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
}

_CONSTANTS = {"pi": math.pi, "PI": math.pi, "π": math.pi, "tau": math.tau, "euler": math.e}

#: Common beginner mistakes worth a targeted hint instead of "unknown gate".
_GATE_HINTS = {
    "cnot": "OpenQASM 2.0 spells the controlled-NOT `cx`",
    "toffoli": "OpenQASM 2.0 spells the Toffoli gate `ccx`",
    "hadamard": "the Hadamard gate is `h`",
    "not": "the bit-flip gate is `x`",
    "phase": "the phase gate is `u1(theta)` (or `s`, `t` for fixed angles)",
    "measure_all": "use `measure q -> c;` to measure a whole register",
}


class _GateDeclaration(object):
    """A ``gate`` body captured for later inlining."""

    __slots__ = ("name", "params", "qubits", "body")

    def __init__(
        self,
        name: str,
        params: Sequence[str],
        qubits: Sequence[str],
        body: Sequence[Tuple[str, List[Any], List[str], Token]],
    ) -> None:
        self.name = name
        self.params = list(params)
        self.qubits = list(qubits)
        self.body = list(body)


class _Parser(object):
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.position = 0
        self.circuit = Circuit()
        self.declarations = {}  # type: Dict[str, _GateDeclaration]

    # ------------------------------------------------------------ token utils

    @property
    def token(self) -> Token:
        return self.tokens[self.position]

    def advance(self) -> Token:
        token = self.tokens[self.position]
        if token.kind != "EOF":
            self.position += 1
        return token

    def error(self, message: str, token: Optional[Token] = None, hint: Optional[str] = None) -> None:
        token = token or self.token
        raise QasmError(
            message,
            line=token.line,
            column=token.column,
            source_line=token.source_line,
            hint=hint,
        )

    def expect_op(self, value: str, hint: Optional[str] = None) -> Token:
        if not self.token.is_op(value):
            found = "end of file" if self.token.kind == "EOF" else repr(self.token.value)
            self.error("expected %r but found %s" % (value, found), hint=hint)
        return self.advance()

    def expect_id(self) -> Token:
        if self.token.kind != "ID":
            self.error("expected an identifier but found %r" % self.token.value)
        return self.advance()

    def expect_integer(self) -> Tuple[int, Token]:
        token = self.token
        if token.kind != "NUMBER" or not token.value.isdigit():
            self.error("expected a non-negative integer but found %r" % token.value)
        self.advance()
        return int(token.value), token

    # ---------------------------------------------------------------- program

    def parse(self) -> Circuit:
        self.parse_header()
        while self.token.kind != "EOF":
            self.parse_statement()
        if not self.circuit.qregs:
            self.error(
                "the program never declares a quantum register",
                token=self.tokens[-1],
                hint="add a declaration such as `qreg q[2];` before using q[0]",
            )
        return self.circuit

    def parse_header(self) -> None:
        if self.token.is_id("OPENQASM"):
            self.advance()
            version = self.token
            if version.kind != "NUMBER":
                self.error("expected a version number after OPENQASM")
            if not version.value.startswith("2"):
                self.error(
                    "LoomQ's front end reads OpenQASM 2.0, got %s" % version.value,
                    token=version,
                    hint="OpenQASM 3 is produced by `transpile(qasm, 'braket')`, not consumed",
                )
            self.advance()
            self.expect_op(";")
        while self.token.is_id("include"):
            self.advance()
            if self.token.kind != "STRING":
                self.error("expected a quoted file name after include")
            self.advance()
            self.expect_op(";")

    # -------------------------------------------------------------- statement

    def parse_statement(self) -> None:
        token = self.token
        if token.kind != "ID":
            self.error("expected a statement but found %r" % token.value)

        keyword = token.value
        if keyword == "qreg":
            self.parse_register_declaration(quantum=True)
        elif keyword == "creg":
            self.parse_register_declaration(quantum=False)
        elif keyword == "gate":
            self.parse_gate_declaration()
        elif keyword == "opaque":
            self.parse_opaque_declaration()
        elif keyword == "measure":
            self.parse_measure()
        elif keyword == "reset":
            self.parse_reset()
        elif keyword == "barrier":
            self.parse_barrier()
        elif keyword == "if":
            self.parse_if()
        elif keyword in ("include", "OPENQASM"):
            self.error(
                "`%s` may only appear in the program header" % keyword,
                hint="move it above the first register declaration",
            )
        else:
            self.parse_gate_call()

    def parse_register_declaration(self, quantum: bool) -> None:
        self.advance()
        name_token = self.expect_id()
        self.expect_op("[")
        size, size_token = self.expect_integer()
        self.expect_op("]")
        self.expect_op(";")
        if size <= 0:
            self.error("register %s must hold at least one bit" % name_token.value, token=size_token)
        existing = (
            self.circuit.qreg(name_token.value) if quantum else self.circuit.creg(name_token.value)
        )
        if existing is not None:
            self.error(
                "register %r is declared twice" % name_token.value,
                token=name_token,
            )
        if quantum:
            self.circuit.add_qreg(name_token.value, size)
        else:
            self.circuit.add_creg(name_token.value, size)

    # ----------------------------------------------------- gate declarations

    def parse_gate_declaration(self) -> None:
        self.advance()
        name_token = self.expect_id()
        params = []  # type: List[str]
        if self.token.is_op("("):
            self.advance()
            while not self.token.is_op(")"):
                params.append(self.expect_id().value)
                if self.token.is_op(","):
                    self.advance()
            self.expect_op(")")
        qubits = [self.expect_id().value]
        while self.token.is_op(","):
            self.advance()
            qubits.append(self.expect_id().value)
        self.expect_op("{")
        body = []  # type: List[Tuple[str, List[Any], List[str], Token]]
        while not self.token.is_op("}"):
            if self.token.kind == "EOF":
                self.error("unterminated gate body for %r" % name_token.value, token=name_token)
            if self.token.is_id("barrier"):
                self.advance()
                while not self.token.is_op(";"):
                    self.advance()
                self.expect_op(";")
                continue
            call_token = self.expect_id()
            call_params = []  # type: List[Any]
            if self.token.is_op("("):
                self.advance()
                while not self.token.is_op(")"):
                    call_params.append(self.parse_expression())
                    if self.token.is_op(","):
                        self.advance()
                self.expect_op(")")
            call_qubits = [self.expect_id().value]
            while self.token.is_op(","):
                self.advance()
                call_qubits.append(self.expect_id().value)
            self.expect_op(";")
            body.append((call_token.value, call_params, call_qubits, call_token))
        self.expect_op("}")
        self.declarations[name_token.value] = _GateDeclaration(
            name_token.value, params, qubits, body
        )

    def parse_opaque_declaration(self) -> None:
        opaque_token = self.advance()
        name_token = self.expect_id()
        while not self.token.is_op(";"):
            if self.token.kind == "EOF":
                self.error("unterminated opaque declaration", token=opaque_token)
            self.advance()
        self.expect_op(";")
        self.error(
            "gate %r is declared `opaque`, so it has no definition to simulate" % name_token.value,
            token=name_token,
            hint="replace it with a concrete `gate` body or a standard qelib1 gate",
        )

    # -------------------------------------------------------------- operands

    def parse_argument(self, quantum: bool) -> Tuple[Register, Optional[int], Token]:
        name_token = self.expect_id()
        register = (
            self.circuit.qreg(name_token.value) if quantum else self.circuit.creg(name_token.value)
        )
        if register is None:
            other = (
                self.circuit.creg(name_token.value)
                if quantum
                else self.circuit.qreg(name_token.value)
            )
            kind = "quantum" if quantum else "classical"
            if other is not None:
                hint = "%r is a %s register" % (
                    name_token.value,
                    "classical" if quantum else "quantum",
                )
            else:
                declared = [reg.name for reg in (self.circuit.qregs if quantum else self.circuit.cregs)]
                hint = (
                    "declared %s registers: %s" % (kind, ", ".join(declared))
                    if declared
                    else "declare it first, e.g. `%s %s[2];`" % ("qreg" if quantum else "creg", name_token.value)
                )
            self.error(
                "undeclared %s register %r" % (kind, name_token.value),
                token=name_token,
                hint=hint,
            )
        index = None  # type: Optional[int]
        if self.token.is_op("["):
            self.advance()
            index, index_token = self.expect_integer()
            self.expect_op("]")
            if index >= register.size:
                self.error(
                    "%s[%d] is out of range; %s holds %d bit(s) (0..%d)"
                    % (register.name, index, register.name, register.size, register.size - 1),
                    token=index_token,
                )
        return register, index, name_token

    def expand(self, register: Register, index: Optional[int], quantum: bool) -> List[int]:
        if index is None:
            return register.indices()
        return [register.index(index)]

    def broadcast(
        self, operands: Sequence[List[int]], token: Token, what: str
    ) -> List[Tuple[int, ...]]:
        """Apply OpenQASM's register-broadcast rule to a statement's operands."""
        widths = {len(operand) for operand in operands}
        widths.discard(1)
        if len(widths) > 1:
            self.error(
                "%s mixes registers of different sizes (%s)"
                % (what, ", ".join(str(len(operand)) for operand in operands)),
                token=token,
                hint="broadcasting needs every register operand to have the same length",
            )
        width = widths.pop() if widths else 1
        rows = []
        for step in range(width):
            rows.append(tuple(operand[0] if len(operand) == 1 else operand[step] for operand in operands))
        return rows

    # ------------------------------------------------------------ statements

    def parse_measure(self) -> Emitted:
        keyword = self.advance()
        qregister, qindex, _ = self.parse_argument(quantum=True)
        self.expect_op("->", hint="measurement syntax is `measure q[0] -> c[0];`")
        cregister, cindex, _ = self.parse_argument(quantum=False)
        self.expect_op(";")
        rows = self.broadcast(
            [self.expand(qregister, qindex, True), self.expand(cregister, cindex, False)],
            keyword,
            "measure",
        )
        operations = [MeasureOp(row[0], row[1]) for row in rows]
        for operation in operations:
            self.circuit.append(operation)
        return operations

    def parse_reset(self) -> Emitted:
        keyword = self.advance()
        register, index, _ = self.parse_argument(quantum=True)
        self.expect_op(";")
        operations = [ResetOp(qubit) for qubit in self.expand(register, index, True)]
        for operation in operations:
            self.circuit.append(operation)
        return operations

    def parse_barrier(self) -> Emitted:
        self.advance()
        qubits = []  # type: List[int]
        while not self.token.is_op(";"):
            register, index, _ = self.parse_argument(quantum=True)
            qubits.extend(self.expand(register, index, True))
            if self.token.is_op(","):
                self.advance()
        self.expect_op(";")
        operation = BarrierOp(qubits)
        self.circuit.append(operation)
        return [operation]

    def parse_if(self) -> None:
        keyword = self.advance()
        self.expect_op("(")
        name_token = self.expect_id()
        register = self.circuit.creg(name_token.value)
        if register is None:
            self.error(
                "undeclared classical register %r in `if` condition" % name_token.value,
                token=name_token,
            )
        self.expect_op("==", hint="OpenQASM 2.0 conditions compare a whole creg: `if (c == 1)`")
        value, _ = self.expect_integer()
        self.expect_op(")")

        start = len(self.circuit.ops)
        self.parse_statement()
        body = self.circuit.ops[start:]
        del self.circuit.ops[start:]
        if not body:
            self.error("`if` has no operation to guard", token=keyword)
        for operation in body:
            self.circuit.append(ConditionalOp(register.indices(), value, operation))

    def parse_gate_call(self) -> None:
        name_token = self.advance()
        params = []  # type: List[Any]
        if self.token.is_op("("):
            self.advance()
            while not self.token.is_op(")"):
                params.append(self.parse_expression())
                if self.token.is_op(","):
                    self.advance()
                elif not self.token.is_op(")"):
                    self.error("expected ',' or ')' in the parameter list")
            self.expect_op(")")
        operands = []  # type: List[List[int]]
        if self.token.is_op(";"):
            self.error(
                "gate %r is applied to no qubits" % name_token.value,
                token=name_token,
                hint="write `%s q[0];`" % name_token.value,
            )
        while not self.token.is_op(";"):
            register, index, _ = self.parse_argument(quantum=True)
            operands.append(self.expand(register, index, True))
            if self.token.is_op(","):
                self.advance()
            elif not self.token.is_op(";"):
                self.error("expected ',' or ';' after a gate operand")
        self.expect_op(";")

        values = [self.evaluate(node, {}, name_token) for node in params]
        rows = self.broadcast(operands, name_token, "gate %s" % name_token.value)
        for row in rows:
            self.emit_call(name_token.value, values, list(row), name_token, depth=0)

    # -------------------------------------------------------------- inlining

    def emit_call(
        self,
        name: str,
        params: Sequence[float],
        qubits: Sequence[int],
        token: Token,
        depth: int,
    ) -> None:
        if depth > _MAX_INLINE_DEPTH:
            self.error(
                "gate %r expands recursively without terminating" % name,
                token=token,
            )

        declaration = self.declarations.get(name)
        if declaration is not None:
            if len(params) != len(declaration.params):
                self.error(
                    "gate %s takes %d parameter(s), got %d"
                    % (name, len(declaration.params), len(params)),
                    token=token,
                )
            if len(qubits) != len(declaration.qubits):
                self.error(
                    "gate %s acts on %d qubit(s), got %d"
                    % (name, len(declaration.qubits), len(qubits)),
                    token=token,
                )
            environment = dict(zip(declaration.params, params))
            binding = dict(zip(declaration.qubits, qubits))
            for call_name, call_params, call_qubits, call_token in declaration.body:
                resolved_params = [
                    self.evaluate(node, environment, call_token) for node in call_params
                ]
                try:
                    resolved_qubits = [binding[argument] for argument in call_qubits]
                except KeyError as missing:
                    self.error(
                        "gate %s uses undeclared qubit %s in its body" % (name, missing),
                        token=call_token,
                    )
                self.emit_call(call_name, resolved_params, resolved_qubits, call_token, depth + 1)
            return

        if not is_known(name):
            hint = _GATE_HINTS.get(name.lower())
            self.error(
                "unknown gate %r" % name,
                token=token,
                hint=hint or "LoomQ accepts the qelib1 standard library; define custom gates with `gate`",
            )

        canonical = canonical_name(name)
        expected_params, expected_qubits = signature(canonical)
        if len(params) != expected_params:
            self.error(
                "gate %s takes %d parameter(s), got %d" % (canonical, expected_params, len(params)),
                token=token,
            )
        if len(qubits) != expected_qubits:
            self.error(
                "gate %s acts on %d qubit(s), got %d" % (canonical, expected_qubits, len(qubits)),
                token=token,
                hint="check the operand order, e.g. `cx control, target;`",
            )
        if len(set(qubits)) != len(qubits):
            self.error(
                "gate %s is applied to the same qubit more than once" % canonical,
                token=token,
            )
        self.circuit.append(GateOp(canonical, params, qubits))

    # ------------------------------------------------------------ expressions

    def parse_expression(self) -> Any:
        return self.parse_additive()

    def parse_additive(self) -> Any:
        node = self.parse_multiplicative()
        while self.token.is_op("+") or self.token.is_op("-"):
            operator = self.advance().value
            node = ("bin", operator, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self) -> Any:
        node = self.parse_power()
        while self.token.is_op("*") or self.token.is_op("/"):
            operator = self.advance().value
            node = ("bin", operator, node, self.parse_power())
        return node

    def parse_power(self) -> Any:
        node = self.parse_unary()
        if self.token.is_op("^"):
            self.advance()
            return ("bin", "^", node, self.parse_power())
        return node

    def parse_unary(self) -> Any:
        if self.token.is_op("-"):
            self.advance()
            return ("neg", self.parse_unary())
        if self.token.is_op("+"):
            self.advance()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Any:
        token = self.token
        if token.is_op("("):
            self.advance()
            node = self.parse_expression()
            self.expect_op(")")
            return node
        if token.kind == "NUMBER":
            self.advance()
            return ("num", float(token.value))
        if token.kind == "ID":
            self.advance()
            if self.token.is_op("("):
                self.advance()
                argument = self.parse_expression()
                self.expect_op(")")
                return ("call", token.value, argument, token)
            return ("var", token.value, token)
        self.error("expected a number or parameter but found %r" % token.value)

    def evaluate(self, node: Any, environment: Dict[str, float], token: Token) -> float:
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "neg":
            return -self.evaluate(node[1], environment, token)
        if kind == "var":
            name = node[1]
            if name in environment:
                return environment[name]
            if name in _CONSTANTS:
                return _CONSTANTS[name]
            self.error(
                "unknown parameter %r in an angle expression" % name,
                token=node[2],
                hint="use a number, `pi`, or a parameter declared by the enclosing gate",
            )
        if kind == "call":
            function = _FUNCTIONS.get(node[1])
            if function is None:
                self.error(
                    "unknown function %r in an angle expression" % node[1],
                    token=node[3],
                    hint="available: " + ", ".join(sorted(_FUNCTIONS)),
                )
            try:
                return function(self.evaluate(node[2], environment, token))
            except ValueError as exc:
                self.error("%s() is undefined here: %s" % (node[1], exc), token=node[3])
        if kind == "bin":
            operator = node[1]
            left = self.evaluate(node[2], environment, token)
            right = self.evaluate(node[3], environment, token)
            if operator == "+":
                return left + right
            if operator == "-":
                return left - right
            if operator == "*":
                return left * right
            if operator == "/":
                if right == 0:
                    self.error("division by zero in an angle expression", token=token)
                return left / right
            if operator == "^":
                return left ** right
        raise QasmError("malformed angle expression")  # pragma: no cover - defensive


def parse_qasm(source: str) -> Circuit:
    """Parse OpenQASM 2.0 source into a :class:`loomq.ir.Circuit`.

    Raises :class:`loomq.errors.QasmError` with line/column context on any
    problem, which is what the L2 agent turns into human-readable repair advice.
    """
    if not isinstance(source, str):
        raise QasmError("QASM source must be a string, got %s" % type(source).__name__)
    if not source.strip():
        raise QasmError(
            "the program is empty",
            hint="a minimal program is:\nOPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[1];\ncreg c[1];\nh q[0];\nmeasure q -> c;",
        )
    return _Parser(source).parse()


__all__ = ["parse_qasm"]
