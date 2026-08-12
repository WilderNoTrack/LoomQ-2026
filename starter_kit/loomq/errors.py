"""Exception hierarchy shared by every LoomQ layer.

Errors carry enough structure that the L2 agent can turn them into plain-language
help ("line 4: you used `CX`, OpenQASM gate names are lower case") instead of a
stack trace.  That is the whole point of the project, so the diagnostics live in
the core rather than in the UI.
"""

from typing import Optional


class LoomQError(Exception):
    """Base class for everything LoomQ raises deliberately."""


class QasmError(LoomQError):
    """A problem in the user's OpenQASM source."""

    def __init__(
        self,
        message: str,
        line: Optional[int] = None,
        column: Optional[int] = None,
        source_line: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        self.message = message
        self.line = line
        self.column = column
        self.source_line = source_line
        self.hint = hint
        super().__init__(self.format())

    def format(self) -> str:
        where = ""
        if self.line is not None:
            where = " (line %d" % self.line
            if self.column is not None:
                where += ", column %d" % self.column
            where += ")"
        text = "%s%s" % (self.message, where)
        if self.source_line:
            text += "\n    " + self.source_line.strip()
        if self.hint:
            text += "\n  hint: " + self.hint
        return text


class UnsupportedGateError(QasmError):
    """The circuit uses a gate LoomQ does not know how to interpret."""


class TranspileError(LoomQError):
    """The circuit cannot be expressed in the requested target IR."""


class BackendError(LoomQError):
    """A backend could not execute the circuit."""


class UnknownTargetError(LoomQError):
    """`target` was not one of the supported platform names."""


class HybridQasmError(LoomQError):
    """A problem in a Hybrid-QASM classical block (L3)."""

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        self.message = message
        self.line = line
        super().__init__(
            "%s (line %d)" % (message, line) if line is not None else message
        )


class AgentError(LoomQError):
    """The L2 agent could not fulfil the request."""


class LLMConfigurationError(AgentError):
    """A required ``LOOMQ_LLM_*`` environment variable is missing or unusable."""


class LLMTransportError(AgentError):
    """The model service could not be reached or returned an unusable payload."""


__all__ = [
    "LoomQError",
    "QasmError",
    "UnsupportedGateError",
    "TranspileError",
    "BackendError",
    "UnknownTargetError",
    "HybridQasmError",
    "AgentError",
    "LLMConfigurationError",
    "LLMTransportError",
]
