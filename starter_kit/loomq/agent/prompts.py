"""Prompts for the L2 agent.

One call does the understanding: it turns free-form text into a typed
specification.  Everything that decides an answer — which backends satisfy the
constraints, what a GHZ state's circuit is — happens in Python afterwards, so a
reworded prompt changes the *input* to the parser and never the correctness of
the result.

A second prompt exists only for repair: when a circuit LoomQ could not
synthesise fails to parse or simulate, the concrete diagnostic goes back to the
model with the broken source.
"""

from ..capabilities import summary_for_prompt
from ..gates import WHITELIST

UNDERSTANDING_SYSTEM = """You are the intent parser inside LoomQ, a universal quantum \
middle layer. You never talk to the end user directly: you convert one request into a \
strict JSON specification that LoomQ's deterministic compiler then acts on.

Reply with a single JSON object and nothing else. No prose, no markdown fence.

{
  "task": "generate" | "repair" | "select_backend" | "explain",
  "language": "zh" | "en",
  "num_qubits": <integer or null>,
  "state": {
    "family": "bell" | "ghz" | "w" | "uniform" | "basis" | "qft" | "grover" | "custom",
    "variant": "phi_plus" | "phi_minus" | "psi_plus" | "psi_minus" | null,
    "basis_state": <binary string, rightmost character is qubit 0, or null>,
    "marked_state": <binary string for grover, or null>
  },
  "measure_all": <true|false>,
  "constraints": {
    "min_qubits": <integer or null>,
    "kind": "simulator" | "qpu" | "cloud" | "hardware" | null,
    "no_queue": <true|false|null>,
    "max_cost": "free" | "free_quota" | "paid" | null,
    "requires_account": <true|false|null>
  },
  "expected_counts": <object mapping bit strings to probabilities, or null>,
  "qasm": <OpenQASM 2.0 source string, or null>,
  "explanation": <one or two sentences, in the user's language>
}

Rules for each field:

task
  "generate"        the user describes a state or algorithm they want a circuit for.
  "repair"          the user supplies broken code and states what it should do.
  "select_backend"  the user asks which platform/backend to run on.
  "explain"         anything else (a concept question, a greeting).

state.family
  Use the named family whenever the request matches one, even loosely:
  a "maximally entangled state" on n>2 qubits is "ghz"; an "EPR pair" or
  "Bell state" is "bell"; "equal superposition"/"all states at once" is
  "uniform"; a named bit pattern like |101> is "basis" with basis_state "101".
  Use "custom" only when none of the families fits.

num_qubits
  The qubit count the user asked for. Infer it from the state description when
  it is implied (a Bell state is 2; |101> is 3).

constraints
  Fill this in for "select_backend" only, from the user's words:
  "no queue"/"zero wait"        -> no_queue: true
  "free"/"no budget"            -> max_cost: "free_quota"
  "must not cost money at all"  -> max_cost: "free"
  "real machine"/"real hardware"/"QPU" -> kind: "hardware"
  "simulator"                   -> kind: "simulator"
  a qubit count                 -> min_qubits
  Leave a field null when the user did not constrain it. Never invent limits.

qasm
  For "generate" and "repair", always provide your best OpenQASM 2.0 program:
  declare qreg and creg, use only these gates - %s -
  and measure every qubit into the matching classical bit.
  For "repair", preserve the user's stated intent, not their mistakes.

expected_counts
  The ideal noiseless measurement distribution of the circuit you intend, keyed
  by bit strings whose RIGHTMOST character is c[0]. Probabilities, not shots.

The available backends, which are the only ones that exist, are:
%s
""" % (
    ", ".join(WHITELIST),
    summary_for_prompt(),
)


REPAIR_SYSTEM = """You repair OpenQASM 2.0 programs for LoomQ.

You are given a program, the user's stated intent, and the exact diagnostic \
LoomQ's parser or simulator produced. Return a single corrected OpenQASM 2.0 \
program and nothing else - no explanation, no markdown fence.

Requirements:
- keep the user's stated intent; fix only what is broken;
- declare every register you use;
- gate names are lower case; use only: %s;
- measure every qubit into the matching classical bit;
- the program must be complete and runnable on its own.
""" % ", ".join(WHITELIST)


def understanding_messages(prompt: str):
    return [
        {"role": "system", "content": UNDERSTANDING_SYSTEM},
        {"role": "user", "content": prompt},
    ]


def repair_messages(intent: str, source: str, diagnostic: str):
    user = (
        "User intent:\n%s\n\nProgram:\n%s\n\nLoomQ diagnostic:\n%s\n\n"
        "Return the corrected OpenQASM 2.0 program only."
        % (intent.strip(), source.strip(), diagnostic.strip())
    )
    return [
        {"role": "system", "content": REPAIR_SYSTEM},
        {"role": "user", "content": user},
    ]


__all__ = [
    "REPAIR_SYSTEM",
    "UNDERSTANDING_SYSTEM",
    "repair_messages",
    "understanding_messages",
]
