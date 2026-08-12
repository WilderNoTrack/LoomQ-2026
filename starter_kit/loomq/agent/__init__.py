"""L2 — the agent that lets someone with no quantum background drive a QPU.

``agent_chat(prompt) -> str`` handles the three scored task shapes: describe a
state and get a circuit, hand over broken code and get it fixed, or ask which
platform to run on.

The design principle is **the model understands, LoomQ decides**:

1. one model call turns the request into a typed specification;
2. Python acts on that specification — synthesising the circuit for a known
   state family, or filtering the capability table for a backend question;
3. every circuit is parsed, simulated and checked *before* it is returned, and a
   failure goes back to the model as a concrete diagnostic rather than to the
   user as a broken answer.

That split is what makes rephrased prompts harmless: rewording changes the input
to step 1, while steps 2 and 3 are deterministic code with their own tests.  It
also means the answer is verified rather than hoped for — LoomQ already owns an
exact simulator, so there is no reason to ship a circuit it has not run.

Nothing here has a network dependency beyond the configured model service, and
no URL, key or model name is hard-coded: see :mod:`loomq.agent.llm`.
"""

import os
import time
from typing import Any, Dict, List, Optional

from ..errors import AgentError, LLMConfigurationError, LLMTransportError, LoomQError, QasmError
from ..result import hellinger_fidelity
from .llm import LLMClient, extract_json, is_configured, load_config
from .prompts import repair_messages, understanding_messages
from .selection import constraints_from_text, format_answer, merge_constraints
from .synthesis import analyse, canonical_family, expected_distribution, normalize, synthesize

#: Leave headroom inside the 120 s per-case budget for verification and retries.
DEADLINE_MARGIN = 12.0

#: How many times a failing circuit is sent back to the model.
MAX_REPAIR_ROUNDS = 2

#: Fidelity a circuit must reach against a known target to be accepted.
ACCEPT_FIDELITY = 0.97


class AgentResult(object):
    """The reply plus a machine-readable trace, for the UI and the tests."""

    __slots__ = ("text", "trace")

    def __init__(self, text: str, trace: Dict[str, Any]) -> None:
        self.text = text
        self.trace = trace

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.text


# ---------------------------------------------------------------- formatting


def _code_block(qasm: str) -> str:
    return "```qasm\n%s\n```" % qasm.strip()


def _distribution_lines(distribution: Dict[str, float], language: str, limit: int = 8) -> List[str]:
    ordered = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))[:limit]
    header = "预期测量结果（无噪声）：" if language != "en" else "Expected outcomes (noise-free):"
    lines = [header]
    for key, probability in ordered:
        bar = "#" * max(1, int(round(probability * 24)))
        lines.append("  |%s>  %5.1f%%  %s" % (key, probability * 100.0, bar))
    return lines


def _circuit_answer(
    qasm: str,
    explanation: str,
    language: str,
    provenance: str,
    verified_against: Optional[str] = None,
) -> str:
    distribution, summary = analyse(qasm)
    chinese = language != "en"

    lines = []  # type: List[str]
    if explanation:
        lines.append(explanation.strip())
        lines.append("")
    lines.append(_code_block(qasm))
    lines.append("")
    lines.extend(_distribution_lines(distribution, language))
    lines.append("")
    if chinese:
        lines.append(
            "电路规模：%d 比特 / %d 个门 / 深度 %d。%s"
            % (summary["qubits"], summary["gates"], summary["depth"], provenance)
        )
        if verified_against:
            lines.append("已用 LoomQ 内置精确模拟器核对：%s。" % verified_against)
        lines.append("想真跑一次：`loomq run circuit.qasm --target braket`（本地模拟器，免费无需账号）。")
    else:
        lines.append(
            "Circuit: %d qubits / %d gates / depth %d. %s"
            % (summary["qubits"], summary["gates"], summary["depth"], provenance)
        )
        if verified_against:
            lines.append("Checked against LoomQ's exact simulator: %s." % verified_against)
        lines.append("Run it: `loomq run circuit.qasm --target braket` (free local simulator).")
    return "\n".join(lines)


# ------------------------------------------------------------- circuit paths


def _verify(qasm: str, expected: Optional[Dict[str, float]]) -> Dict[str, Any]:
    """Parse, normalise and simulate a candidate circuit."""
    normalized = normalize(qasm)
    distribution, summary = analyse(normalized)
    report = {"qasm": normalized, "summary": summary, "distribution": distribution}
    if expected:
        report["fidelity"] = hellinger_fidelity(distribution, expected)
    return report


def _publish(trace: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Expose the verified circuit so the web UI can draw and run it."""
    trace["qasm"] = report["qasm"]
    trace["distribution"] = report["distribution"]
    trace["summary"] = report["summary"]
    if "fidelity" in report:
        trace["fidelity"] = report["fidelity"]


def _repair_loop(
    client: LLMClient,
    intent: str,
    qasm: str,
    diagnostic: str,
    expected: Optional[Dict[str, float]],
    trace: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Send a failing circuit back to the model with the exact error."""
    attempts = trace.setdefault("repairs", [])
    current, current_diagnostic = qasm, diagnostic

    for _ in range(MAX_REPAIR_ROUNDS):
        try:
            reply = client.complete(
                repair_messages(intent, current, current_diagnostic), attempts=1
            )
        except LLMTransportError as exc:
            attempts.append({"diagnostic": current_diagnostic, "outcome": str(exc)})
            return None
        candidate = _extract_qasm(reply) or reply
        try:
            report = _verify(candidate, expected)
        except LoomQError as exc:
            attempts.append({"diagnostic": current_diagnostic, "outcome": str(exc)})
            current, current_diagnostic = candidate, str(exc)
            continue
        fidelity = report.get("fidelity")
        if fidelity is not None and fidelity < ACCEPT_FIDELITY:
            attempts.append({"diagnostic": current_diagnostic, "outcome": "fidelity %.4f" % fidelity})
            current = report["qasm"]
            current_diagnostic = (
                "the circuit parses but its measurement distribution does not match the "
                "stated intent (Hellinger fidelity %.4f against the expected outcome)" % fidelity
            )
            continue
        attempts.append({"diagnostic": current_diagnostic, "outcome": "repaired"})
        return report
    return None


def _extract_qasm(text: str) -> Optional[str]:
    """Recover a QASM program from a model reply in whatever shape it arrives.

    Models answer a "return only the program" instruction with a fenced block,
    with bare source, or — often enough to matter — with the JSON object they
    were asked for two turns earlier, the program hiding in a string field with
    its newlines still escaped.  All three are handled, because a reply LoomQ
    cannot read is a case lost for no good reason.
    """
    if not isinstance(text, str) or "OPENQASM" not in text:
        return None

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = extract_json(stripped)
        except LLMTransportError:
            payload = None
        if isinstance(payload, dict):
            for key in ("qasm", "program", "code", "circuit"):
                value = payload.get(key)
                if isinstance(value, str) and "OPENQASM" in value:
                    return value.strip()

    body = text[text.find("OPENQASM"):]
    fence = body.find("```")
    if fence != -1:
        body = body[:fence]
    # A bare program lifted out of JSON keeps its escapes; a real one has none.
    if "\\n" in body and "\n" not in body.strip():
        body = body.encode("utf-8").decode("unicode_escape")
        quote = body.find('"')
        if quote != -1:
            body = body[:quote]
    body = body.strip()
    if not body or "\\n" in body:
        return None
    return body


def _handle_circuit(
    client: Optional[LLMClient],
    prompt: str,
    spec: Dict[str, Any],
    trace: Dict[str, Any],
) -> str:
    language = "en" if spec.get("language") == "en" else "zh"
    state = spec.get("state") or {}
    explanation = str(spec.get("explanation") or "").strip()

    request = dict(state)
    request["num_qubits"] = spec.get("num_qubits") or state.get("num_qubits")

    family = canonical_family(state.get("family"))
    trace["family"] = family
    trace["num_qubits"] = request.get("num_qubits")

    # Path A: a state family LoomQ can build from its textbook definition.
    synthesized = synthesize(request) if family else None
    if synthesized:
        report = _verify(synthesized, expected_distribution(request))
        trace["route"] = "synthesised"
        _publish(trace, report)
        provenance = (
            "由 LoomQ 按 %s 态的标准构造直接生成，只使用白名单 12 门。" % family
            if language != "en"
            else "Built by LoomQ from the textbook %s construction, whitelist gates only." % family
        )
        return _circuit_answer(
            report["qasm"],
            explanation,
            language,
            provenance,
            "分布与目标态完全一致" if language != "en" else "distribution matches the target state",
        )

    # Path B: the model's own program, verified and repaired if necessary.
    expected = spec.get("expected_counts") if isinstance(spec.get("expected_counts"), dict) else None
    if expected:
        total = sum(float(value) for value in expected.values() if isinstance(value, (int, float)))
        expected = (
            {str(k): float(v) / total for k, v in expected.items() if isinstance(v, (int, float))}
            if total > 0
            else None
        )

    candidate = spec.get("qasm")
    report = None  # type: Optional[Dict[str, Any]]
    diagnostic = None  # type: Optional[str]

    if isinstance(candidate, str) and candidate.strip():
        try:
            report = _verify(candidate, expected)
            fidelity = report.get("fidelity")
            if fidelity is not None and fidelity < ACCEPT_FIDELITY:
                diagnostic = (
                    "the circuit parses but its distribution does not match the stated "
                    "intent (fidelity %.4f)" % fidelity
                )
                report = None
        except LoomQError as exc:
            diagnostic = str(exc)

    if report is None and client is not None and isinstance(candidate, str) and candidate.strip():
        report = _repair_loop(
            client, prompt, candidate, diagnostic or "the program did not compile", expected, trace
        )

    if report is None:
        raise AgentError(diagnostic or "no runnable circuit could be produced")

    trace["route"] = "model+verified"
    _publish(trace, report)
    provenance = (
        "由模型起草，LoomQ 解析、降级到白名单 12 门并模拟核对后给出。"
        if language != "en"
        else "Drafted by the model, then parsed, lowered to the whitelist and simulated by LoomQ."
    )
    return _circuit_answer(report["qasm"], explanation, language, provenance)


def _handle_selection(prompt: str, spec: Dict[str, Any], trace: Dict[str, Any]) -> str:
    language = "en" if spec.get("language") == "en" else "zh"
    constraints = merge_constraints(spec.get("constraints"), constraints_from_text(prompt))
    trace["route"] = "capability-table"
    trace["constraints"] = constraints
    return format_answer(constraints, language)


def _handle_explanation(
    client: Optional[LLMClient], prompt: str, spec: Dict[str, Any], trace: Dict[str, Any]
) -> str:
    language = "en" if spec.get("language") == "en" else "zh"
    trace["route"] = "explain"
    system = (
        "你是 LoomQ 的量子计算向导，面向完全没有物理背景的用户。"
        "用日常语言回答，最多 6 句，不要用公式。"
        "如果用户其实想跑一个电路，就告诉他可以直接描述想要的效果。"
        if language != "en"
        else "You are LoomQ's quantum guide for people with no physics background. "
        "Answer in plain language, at most six sentences, no formulae."
    )
    if client is not None:
        try:
            reply = client.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                attempts=1,
            ).strip()
            if _is_prose(reply):
                return reply
        except LLMTransportError:
            pass

    parts = []  # type: List[str]
    fallback = str(spec.get("explanation") or "").strip()
    if _is_prose(fallback):
        parts.append(fallback)
    failure = trace.get("error")
    if failure:
        parts.append(
            "不过这次我没能生成一个通过自检的电路：%s" % failure
            if language != "en"
            else "I could not produce a circuit that passed verification: %s" % failure
        )
    parts.append(
        "你可以直接描述想要的效果，例如「做一个 3 比特的 GHZ 纠缠态并全部测量」，"
        "或者把出错的 QASM 贴给我，我会告诉你哪一行有问题。"
        if language != "en"
        else "Describe the effect you want - for example \"make a 3-qubit GHZ state and "
        "measure everything\" - or paste the QASM that failed and I will point at the line."
    )
    return "\n\n".join(parts)


def _is_prose(text: str) -> bool:
    """Guard against echoing a raw JSON payload back to the user."""
    candidate = (text or "").strip()
    if not candidate:
        return False
    return not (candidate.startswith("{") and candidate.endswith("}"))


# ---------------------------------------------------------------- offline path


def _offline(prompt: str, trace: Dict[str, Any], reason: str) -> AgentResult:
    """Best effort when the model service is unavailable.

    Formal scoring requires a successful model call, so this cannot earn points
    — it exists so the CLI and the web UI stay usable, and so a network outage
    produces a helpful message rather than a stack trace.
    """
    trace["offline_reason"] = reason
    constraints = constraints_from_text(prompt)
    if constraints:
        trace["route"] = "offline:capability-table"
        return AgentResult(format_answer(constraints, "zh"), trace)
    trace["route"] = "offline:unavailable"
    return AgentResult(
        "LoomQ 目前无法连接模型服务（%s）。\n"
        "请确认已设置 LOOMQ_LLM_BASE_URL / LOOMQ_LLM_API_KEY / LOOMQ_LLM_MODEL。\n"
        "不依赖模型的功能仍然可用，例如：`loomq run circuit.qasm --target braket`。" % reason,
        trace,
    )


# ---------------------------------------------------------------- entry point


def respond(prompt: str, client: Optional[LLMClient] = None) -> AgentResult:
    """Full agent turn, returning the reply and a trace of how it was reached."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("prompt must be a non-empty string")

    trace = {"prompt_chars": len(prompt)}  # type: Dict[str, Any]

    if client is None:
        if not is_configured():
            return _offline(prompt, trace, "LOOMQ_LLM_* environment is not set")
        try:
            config = load_config()
        except LLMConfigurationError as exc:
            return _offline(prompt, trace, str(exc))
        budget = max(10.0, min(config.timeout, 120.0) - DEADLINE_MARGIN)
        client = LLMClient(config, deadline=time.time() + budget)

    try:
        raw = client.complete(understanding_messages(prompt), json_object=True)
        spec = extract_json(raw)
    except (LLMTransportError, LLMConfigurationError) as exc:
        return _offline(prompt, trace, str(exc))

    trace["model_calls"] = client.calls
    trace["task"] = spec.get("task")

    task = str(spec.get("task") or "").strip().lower()
    try:
        if task == "select_backend":
            text = _handle_selection(prompt, spec, trace)
        elif task in ("generate", "repair"):
            text = _handle_circuit(client, prompt, spec, trace)
        elif task == "explain":
            text = _handle_explanation(client, prompt, spec, trace)
        else:
            # Unlabelled but clearly a backend question, or clearly a circuit one.
            if constraints_from_text(prompt):
                text = _handle_selection(prompt, spec, trace)
            elif spec.get("qasm") or spec.get("state"):
                text = _handle_circuit(client, prompt, spec, trace)
            else:
                text = _handle_explanation(client, prompt, spec, trace)
    except AgentError as exc:
        trace["error"] = str(exc)
        text = _handle_explanation(client, prompt, spec, trace)

    trace["model_calls"] = client.calls
    trace["successful_model_calls"] = client.successful_calls
    return AgentResult(text, trace)


def agent_chat(prompt: str) -> str:
    """L2 contract entry point: prompt in, reply text out."""
    return respond(prompt).text


__all__ = ["ACCEPT_FIDELITY", "AgentResult", "agent_chat", "respond"]
