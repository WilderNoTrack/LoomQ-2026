"""Hybrid-QASM front end.

A Hybrid-QASM program is an OpenQASM 2.0 program with one or more
``classical { ... }`` blocks spliced in.  Parsing happens in two stages:

1. **Split.** A brace-matching scan lifts out every ``classical`` block.  What
   remains is ordinary OpenQASM 2.0, so it goes through the same front end that
   L1 uses — the quantum half of an L3 program is validated exactly as strictly
   as a normal circuit, register bounds and all.
2. **Parse.** Each block body is parsed into :mod:`loomq.hybrid.ast` with a
   recursive-descent parser, reusing the OpenQASM lexer for tokens.

Both halves keep source order, so the returned quantum operation list matches
the order the gates appear in the original program.
"""

from typing import List, Optional, Sequence, Tuple

from ..errors import HybridQasmError
from ..ir import Circuit
from ..qasm import parse_qasm
from ..qasm.lexer import Token, tokenize
from .ast import (
    Assign,
    BinaryOp,
    Comparison,
    Const,
    If,
    LAST_VARIABLE_REGISTER,
    MeasurementBit,
    Node,
    Program,
    Variable,
)

_KEYWORD = "classical"


# ------------------------------------------------------------------ splitting


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def split_segments(source: str) -> List[Tuple[str, str, int]]:
    """Ordered ``(kind, text, line)`` segments, ``kind`` in ``quantum``/``classical``.

    Source order is preserved, which matters for the unified LoomQ-Q stream:
    a ``classical`` block that reads ``c[0]`` has to be emitted *after* the
    measurement that produces it and *before* the gates that follow.

    Comments and string literals are skipped so a ``}`` inside either cannot
    close a block early.
    """
    segments = []  # type: List[Tuple[str, str, int]]
    quantum = []  # type: List[str]
    quantum_line = 1
    index = 0
    length = len(source)

    def flush() -> None:
        text = "".join(quantum)
        if text.strip():
            segments.append(("quantum", text, quantum_line))
        del quantum[:]

    while index < length:
        if source.startswith("//", index) or source.startswith("#", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
            quantum.append(source[index:end])
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise HybridQasmError("unterminated block comment", _line_of(source, index))
            quantum.append(source[index:end + 2])
            index = end + 2
            continue
        if source[index] == '"':
            end = source.find('"', index + 1)
            if end == -1:
                raise HybridQasmError("unterminated string literal", _line_of(source, index))
            quantum.append(source[index:end + 1])
            index = end + 1
            continue

        if source.startswith(_KEYWORD, index) and _is_word_boundary(source, index, len(_KEYWORD)):
            cursor = index + len(_KEYWORD)
            while cursor < length and source[cursor] in " \t\r\n":
                cursor += 1
            if cursor < length and source[cursor] == "{":
                start_line = _line_of(source, index)
                body, cursor = _match_braces(source, cursor, start_line)
                flush()
                segments.append(("classical", body, start_line))
                quantum_line = _line_of(source, cursor)
                index = cursor
                continue

        quantum.append(source[index])
        index += 1

    flush()
    return segments


def split_classical_blocks(source: str) -> Tuple[str, List[Tuple[str, int]]]:
    """``(quantum_source, [(block_body, line), ...])``.

    Classical blocks are replaced by the newlines they spanned, so line numbers
    in the quantum half's diagnostics still point at the user's file.
    """
    quantum = []  # type: List[str]
    blocks = []  # type: List[Tuple[str, int]]
    index = 0
    length = len(source)

    while index < length:
        if source.startswith("//", index) or source.startswith("#", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
            quantum.append(source[index:end])
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise HybridQasmError("unterminated block comment", _line_of(source, index))
            quantum.append(source[index:end + 2])
            index = end + 2
            continue
        if source[index] == '"':
            end = source.find('"', index + 1)
            if end == -1:
                raise HybridQasmError("unterminated string literal", _line_of(source, index))
            quantum.append(source[index:end + 1])
            index = end + 1
            continue

        if source.startswith(_KEYWORD, index) and _is_word_boundary(source, index, len(_KEYWORD)):
            cursor = index + len(_KEYWORD)
            while cursor < length and source[cursor] in " \t\r\n":
                cursor += 1
            if cursor < length and source[cursor] == "{":
                start_line = _line_of(source, index)
                body, cursor = _match_braces(source, cursor, start_line)
                blocks.append((body, start_line))
                # Preserve line numbering for the quantum half's diagnostics.
                quantum.append("\n" * source.count("\n", index, cursor))
                index = cursor
                continue

        quantum.append(source[index])
        index += 1

    return "".join(quantum), blocks


def _is_word_boundary(source: str, index: int, length: int) -> bool:
    before = source[index - 1] if index > 0 else " "
    after = source[index + length] if index + length < len(source) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _match_braces(source: str, start: int, line: int) -> Tuple[str, int]:
    depth = 0
    index = start
    length = len(source)
    while index < length:
        char = source[index]
        if source.startswith("//", index) or source.startswith("#", index):
            end = source.find("\n", index)
            index = length if end == -1 else end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise HybridQasmError("unterminated block comment", line)
            index = end + 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index], index + 1
        index += 1
    raise HybridQasmError("unterminated `classical` block", line)


# --------------------------------------------------------------------- parser


class _ClassicalParser(object):
    def __init__(self, source: str, line_offset: int) -> None:
        self.tokens = tokenize(source)  # type: List[Token]
        self.position = 0
        self.line_offset = line_offset - 1

    @property
    def token(self) -> Token:
        return self.tokens[self.position]

    def advance(self) -> Token:
        token = self.tokens[self.position]
        if token.kind != "EOF":
            self.position += 1
        return token

    def fail(self, message: str, token: Optional[Token] = None) -> None:
        token = token or self.token
        raise HybridQasmError(message, self.line_offset + token.line)

    def expect(self, value: str) -> Token:
        if not self.token.is_op(value):
            found = "end of block" if self.token.kind == "EOF" else repr(self.token.value)
            self.fail("expected %r but found %s" % (value, found))
        return self.advance()

    # ------------------------------------------------------------ statements

    def parse_block(self) -> List[Node]:
        body = []  # type: List[Node]
        while self.token.kind != "EOF":
            if self.token.is_op("}"):
                break
            body.append(self.parse_statement())
        return body

    def parse_statement(self) -> Node:
        if self.token.is_op(";"):
            self.advance()
            return Program([])
        if self.token.is_id("if"):
            return self.parse_if()
        return self.parse_assignment()

    def parse_if(self) -> If:
        self.advance()
        self.expect("(")
        condition = self.parse_comparison()
        self.expect(")")
        then_body = self.parse_body()
        else_body = []  # type: List[Node]
        if self.token.is_id("else"):
            self.advance()
            if self.token.is_id("if"):
                else_body = [self.parse_if()]
            else:
                else_body = self.parse_body()
        return If(condition, then_body, else_body)

    def parse_body(self) -> List[Node]:
        if self.token.is_op("{"):
            self.advance()
            body = self.parse_block()
            self.expect("}")
            return body
        return [self.parse_statement()]

    def parse_assignment(self) -> Assign:
        token = self.token
        if token.kind != "ID":
            self.fail("expected an assignment or `if`, found %r" % token.value)
        target = self.parse_variable(self.advance())
        self.expect("=")
        value = self.parse_expression()
        if self.token.is_op(";"):
            self.advance()
        return Assign(target, value)

    def parse_variable(self, token: Token) -> Variable:
        name = token.value
        if len(name) < 2 or name[0] not in "rR" or not name[1:].isdigit():
            self.fail(
                "assignment target must be one of r1..r%d, found %r"
                % (LAST_VARIABLE_REGISTER, name),
                token,
            )
        index = int(name[1:])
        if not 1 <= index <= LAST_VARIABLE_REGISTER:
            self.fail("register %s is outside r1..r%d" % (name, LAST_VARIABLE_REGISTER), token)
        return Variable(index)

    # ----------------------------------------------------------- expressions

    def parse_comparison(self) -> Comparison:
        left = self.parse_expression()
        if self.token.is_op("==") or self.token.is_op("!="):
            operator = self.advance().value
            return Comparison(operator, left, self.parse_expression())
        self.fail("a condition must compare two expressions with `==` or `!=`")

    def parse_expression(self) -> Node:
        node = self.parse_unary()
        while self.token.is_op("+") or self.token.is_op("-"):
            operator = self.advance().value
            node = BinaryOp(operator, node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        if self.token.is_op("-"):
            self.advance()
            return BinaryOp("-", Const(0), self.parse_unary())
        if self.token.is_op("+"):
            self.advance()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Node:
        token = self.token
        if token.is_op("("):
            self.advance()
            node = self.parse_expression()
            self.expect(")")
            return node
        if token.kind == "NUMBER":
            self.advance()
            if not token.value.isdigit():
                self.fail("the classical block only supports integer literals", token)
            return Const(int(token.value))
        if token.kind == "ID":
            self.advance()
            name = token.value
            if name in ("c", "C"):
                self.expect("[")
                index_token = self.token
                if index_token.kind != "NUMBER" or not index_token.value.isdigit():
                    self.fail("measurement index must be an integer", index_token)
                self.advance()
                self.expect("]")
                return MeasurementBit(int(index_token.value))
            return self.parse_variable(token)
        self.fail("expected an integer, rN or c[k], found %r" % token.value)


def parse_classical(source: str, line: int = 1) -> List[Node]:
    return _ClassicalParser(source, line).parse_block()


# ---------------------------------------------------------------- entry point


def parse_hybrid(source: str) -> Tuple[Circuit, Program]:
    """``(quantum_circuit, classical_program)`` for a Hybrid-QASM string."""
    if not isinstance(source, str) or not source.strip():
        raise HybridQasmError("the Hybrid-QASM program is empty")

    quantum_source, blocks = split_classical_blocks(source)
    circuit = parse_qasm(quantum_source)

    body = []  # type: List[Node]
    for block, line in blocks:
        body.extend(parse_classical(block, line))
    return circuit, Program(body)


def parse_hybrid_segments(source: str) -> Tuple[Circuit, List[Tuple[str, object]]]:
    """``(circuit, segments)`` where each segment is ``("gates", (start, end))``
    or ``("classical", Program)``.

    The gate spans index into ``circuit.ops``, so the caller can walk the program
    in source order without re-parsing anything.  Cumulative prefixes are parsed
    to find each boundary: parsing is deterministic, so the op count after
    chunk *k* is exactly where the next classical block belongs.
    """
    if not isinstance(source, str) or not source.strip():
        raise HybridQasmError("the Hybrid-QASM program is empty")

    raw = split_segments(source)
    quantum_chunks = [text for kind, text, _ in raw if kind == "quantum"]
    circuit = parse_qasm("".join(quantum_chunks))

    segments = []  # type: List[Tuple[str, object]]
    prefix = []  # type: List[str]
    consumed = 0
    for kind, text, line in raw:
        if kind == "quantum":
            prefix.append(text)
            total = len(parse_qasm("".join(prefix)).ops)
            if total > consumed:
                segments.append(("gates", (consumed, total)))
                consumed = total
        else:
            segments.append(("classical", Program(parse_classical(text, line))))
    return circuit, segments


__all__ = [
    "parse_classical",
    "parse_hybrid",
    "parse_hybrid_segments",
    "split_classical_blocks",
    "split_segments",
]
