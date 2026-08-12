"""Hand-written lexer for OpenQASM 2.0.

A generated lexer would be shorter, but this one keeps the exact line, column and
source line for every token, which is what lets LoomQ answer a beginner with
"line 4, column 1: `CX` should be `cx`" instead of a parse traceback.
"""

from typing import List, Optional

from ..errors import QasmError

#: Multi-character operators, longest first so ``->`` never lexes as ``-``.
_OPERATORS = ("->", "==", "!=", ">=", "<=", ";", ",", "(", ")", "[", "]", "{", "}",
              "+", "-", "*", "/", "^", "=", "<", ">")

_ID_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_ID_BODY = _ID_START | set("0123456789")
_DIGITS = set("0123456789")


class Token(object):
    __slots__ = ("kind", "value", "line", "column", "source_line")

    def __init__(self, kind: str, value: str, line: int, column: int, source_line: str) -> None:
        self.kind = kind  # 'ID' | 'NUMBER' | 'STRING' | 'OP' | 'EOF'
        self.value = value
        self.line = line
        self.column = column
        self.source_line = source_line

    def is_op(self, value: str) -> bool:
        return self.kind == "OP" and self.value == value

    def is_id(self, value: Optional[str] = None) -> bool:
        return self.kind == "ID" and (value is None or self.value == value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Token(%s, %r, line=%d)" % (self.kind, self.value, self.line)


def tokenize(source: str) -> List[Token]:
    """Split OpenQASM source into tokens, skipping comments and whitespace."""
    lines = source.split("\n")
    tokens = []  # type: List[Token]
    index = 0
    line = 1
    column = 1
    length = len(source)

    def current_line() -> str:
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def fail(message: str, hint: Optional[str] = None) -> None:
        raise QasmError(message, line=line, column=column, source_line=current_line(), hint=hint)

    while index < length:
        char = source[index]

        if char == "\n":
            index += 1
            line += 1
            column = 1
            continue
        if char in " \t\r":
            index += 1
            column += 1
            continue

        # line comment
        if source.startswith("//", index) or source.startswith("#", index):
            while index < length and source[index] != "\n":
                index += 1
            continue

        # block comment
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                fail("unterminated block comment")
            for offset in range(index, end + 2):
                if source[offset] == "\n":
                    line += 1
                    column = 1
                else:
                    column += 1
            index = end + 2
            continue

        start_column = column

        if char == '"':
            end = source.find('"', index + 1)
            if end == -1:
                fail("unterminated string literal")
            value = source[index + 1:end]
            tokens.append(Token("STRING", value, line, start_column, current_line()))
            column += end - index + 1
            index = end + 1
            continue

        if char in _ID_START:
            end = index
            while end < length and source[end] in _ID_BODY:
                end += 1
            value = source[index:end]
            tokens.append(Token("ID", value, line, start_column, current_line()))
            column += end - index
            index = end
            continue

        if char in _DIGITS or (char == "." and index + 1 < length and source[index + 1] in _DIGITS):
            end = index
            seen_dot = False
            seen_exp = False
            while end < length:
                candidate = source[end]
                if candidate in _DIGITS:
                    end += 1
                elif candidate == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    end += 1
                elif candidate in "eE" and not seen_exp and end > index:
                    seen_exp = True
                    end += 1
                    if end < length and source[end] in "+-":
                        end += 1
                else:
                    break
            value = source[index:end]
            tokens.append(Token("NUMBER", value, line, start_column, current_line()))
            column += end - index
            index = end
            continue

        for operator in _OPERATORS:
            if source.startswith(operator, index):
                tokens.append(Token("OP", operator, line, start_column, current_line()))
                index += len(operator)
                column += len(operator)
                break
        else:
            fail(
                "unexpected character %r" % char,
                hint="OpenQASM statements end with ';' and use lower-case gate names",
            )

    tokens.append(Token("EOF", "", line, column, current_line()))
    return tokens


__all__ = ["Token", "tokenize"]
