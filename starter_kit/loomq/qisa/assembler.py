"""Mnemonics <-> 32-bit words.

The encoding in :mod:`loomq.qisa.isa` is only real if something actually uses
it, so the assembler is not decoration: ``assemble()`` turns a listing into
words, ``disassemble()`` turns them back, and the extended emulator executes
either form.  ``loomq qisa --words`` prints a program as ``.word 0x...`` lines
that run identically to the mnemonics they came from — which is the end-to-end
evidence that the bit layout is the truth and the mnemonics are the convenience.
"""

import re
from typing import Dict, List, Optional, Tuple

from .isa import SPEC, QuantumInstruction, decode, encode, is_quantum_word

_REGISTER = re.compile(r"^x(\d{1,2})$", re.IGNORECASE)
_WORD_DIRECTIVE = re.compile(r"^\.word\s+(0x[0-9a-fA-F]+|\d+)$")


def parse_register(token: str) -> int:
    match = _REGISTER.match(token.strip().rstrip(","))
    if not match:
        raise ValueError("%r is not a register name" % token)
    number = int(match.group(1))
    if not 0 <= number <= 31:
        raise ValueError("register index out of range: %r" % token)
    return number


def assemble_line(line: str) -> Optional[int]:
    """Encode one line, or return ``None`` if it is not a LoomQ-Q instruction."""
    text = line.split("#", 1)[0].strip()
    if not text:
        return None

    directive = _WORD_DIRECTIVE.match(text)
    if directive:
        word = int(directive.group(1), 0)
        return word if is_quantum_word(word) else None

    tokens = text.replace(",", " ").split()
    mnemonic = tokens[0].lower()
    if mnemonic not in SPEC:
        return None
    return encode(mnemonic, *[parse_register(token) for token in tokens[1:]])


def assemble(source: str) -> List[Tuple[int, Optional[int]]]:
    """``(line number, word or None)`` for every line of ``source``."""
    return [
        (number, assemble_line(line))
        for number, line in enumerate(source.splitlines(), start=1)
    ]


def to_words(source: str) -> str:
    """Rewrite every LoomQ-Q mnemonic as an equivalent ``.word`` directive."""
    output = []
    for line in source.splitlines():
        word = assemble_line(line)
        if word is None:
            output.append(line)
        else:
            comment = line.split("#", 1)[0].strip()
            output.append("    .word 0x%08X    # %s" % (word, comment))
    return "\n".join(output) + "\n"


def disassemble(word: int) -> QuantumInstruction:
    return decode(word)


def listing(source: str) -> str:
    """A side-by-side encoding listing, for the specification document."""
    rows = []
    for line in source.splitlines():
        text = line.split("#", 1)[0].strip()
        word = assemble_line(line)
        if word is None:
            rows.append("%-34s %s" % ("", text))
        else:
            instruction = decode(word)
            rows.append(
                "0x%08X  f3=0b%s f7=0x%02X  %s"
                % (
                    word,
                    format(instruction.funct3, "03b"),
                    instruction.funct7,
                    instruction.text(),
                )
            )
    return "\n".join(rows)


__all__ = ["assemble", "assemble_line", "disassemble", "listing", "parse_register", "to_words"]
