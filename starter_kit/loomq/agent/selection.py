"""Backend selection: constraints in, canonical backend ids out.

The rules are explicit that the capability table is the sole basis for the
correct answer, so the filtering is a table scan, not a judgement call.  The
model's only contribution is turning "I need zero queue time for a 15-qubit
circuit" into ``{"min_qubits": 15, "no_queue": true}``; :func:`recommend` does
the rest and is fully testable without a network.

When nothing satisfies the constraints, saying so plainly and naming the closest
alternative is the correct answer — the rules score honesty above a confident
wrong id.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from ..capabilities import closest_alternatives, describe, select

#: Phrases that pin a constraint even if the model missed it.
_NO_QUEUE = ("零排队", "无排队", "不排队", "不想等", "无需等待", "不用等", "立刻", "马上",
             "no queue", "zero queue", "no wait", "without waiting", "immediately", "instant")
_FREE = ("免费", "不花钱", "不想花钱", "零成本", "没有预算", "free", "no cost", "without paying",
         "no budget", "don't want to pay", "do not want to pay")
_STRICTLY_FREE = ("完全免费", "一分钱都不", "absolutely free", "completely free", "no charge at all")
_HARDWARE = ("真机", "实机", "真实量子", "物理量子", "硬件", "qpu", "real machine", "real hardware",
             "real quantum", "physical device", "actual quantum computer")
_SIMULATOR = ("模拟器", "仿真", "simulator", "simulation", "emulator")
_NO_ACCOUNT = ("不想注册", "无需注册", "没有账号", "不用注册", "without an account",
               "no account", "no signup", "without registering")

_QUBIT_PATTERNS = (
    re.compile(r"(\d+)\s*(?:个)?\s*(?:比特|量子比特|qubit)", re.IGNORECASE),
    re.compile(r"(\d+)[- ]?qubit", re.IGNORECASE),
)


def constraints_from_text(prompt: str) -> Dict[str, Any]:
    """A local, model-free reading of the prompt.

    Used to fill gaps the model left null and as the fallback when the model
    service is unavailable — never as a replacement for it.
    """
    lowered = prompt.lower()
    constraints = {}  # type: Dict[str, Any]

    for pattern in _QUBIT_PATTERNS:
        match = pattern.search(prompt)
        if match:
            constraints["min_qubits"] = int(match.group(1))
            break

    if any(token in lowered for token in _NO_QUEUE):
        constraints["no_queue"] = True
    if any(token in lowered for token in _STRICTLY_FREE):
        constraints["max_cost"] = "free"
    elif any(token in lowered for token in _FREE):
        constraints["max_cost"] = "free_quota"
    if any(token in lowered for token in _HARDWARE):
        constraints["kind"] = "hardware"
    elif any(token in lowered for token in _SIMULATOR):
        constraints["kind"] = "simulator"
    if any(token in lowered for token in _NO_ACCOUNT):
        constraints["requires_account"] = False
    return constraints


def merge_constraints(model: Optional[Dict[str, Any]], text: Dict[str, Any]) -> Dict[str, Any]:
    """Model-extracted constraints win; the text scan fills in what it left out."""
    merged = dict(text)
    for key, value in (model or {}).items():
        if value is None:
            continue
        if key == "kind" and value not in ("simulator", "qpu", "cloud", "hardware"):
            continue
        if key == "max_cost" and value not in ("free", "free_quota", "paid"):
            continue
        merged[key] = value
    return merged


def recommend(constraints: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``(matching backends, diagnosis)`` for a constraint set."""
    matches = select(
        min_qubits=constraints.get("min_qubits"),
        kind=constraints.get("kind"),
        no_queue=constraints.get("no_queue"),
        max_cost=constraints.get("max_cost"),
        requires_account=constraints.get("requires_account"),
        platform=constraints.get("platform"),
    )
    diagnosis = {"constraints": dict(constraints), "matched": len(matches)}

    if matches:
        # Prefer the roomiest option first so the headline answer is the safest.
        matches.sort(key=lambda entry: (-entry["max_qubits"], entry["id"]))
        return matches, diagnosis

    diagnosis["relaxations"] = _relaxations(constraints)
    diagnosis["alternatives"] = closest_alternatives(constraints.get("min_qubits") or 0)
    return [], diagnosis


def _relaxations(constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Which single constraint, if dropped, would make an answer possible."""
    findings = []
    for key in ("min_qubits", "no_queue", "max_cost", "kind", "requires_account"):
        if constraints.get(key) is None:
            continue
        relaxed = dict(constraints)
        relaxed.pop(key)
        matches = select(
            min_qubits=relaxed.get("min_qubits"),
            kind=relaxed.get("kind"),
            no_queue=relaxed.get("no_queue"),
            max_cost=relaxed.get("max_cost"),
            requires_account=relaxed.get("requires_account"),
        )
        if matches:
            findings.append({"drop": key, "would_allow": [entry["id"] for entry in matches]})
    return findings


_CONSTRAINT_LABELS_ZH = {
    "min_qubits": "至少 %s 比特",
    "kind": "类型 = %s",
    "no_queue": "无排队",
    "max_cost": "费用不超过 %s",
    "requires_account": "账号要求 = %s",
}


def format_answer(constraints: Dict[str, Any], language: str = "zh") -> str:
    """The user-facing reply, always containing the canonical backend id."""
    matches, diagnosis = recommend(constraints)
    chinese = language != "en"

    stated = []
    for key, value in sorted(constraints.items()):
        if value is None:
            continue
        template = _CONSTRAINT_LABELS_ZH.get(key)
        if template is None:
            continue
        stated.append(template % value if "%s" in template else template)
    condition_text = "、".join(stated) if stated else ("没有额外限制" if chinese else "no constraints")

    if matches:
        if chinese:
            lines = ["按你的条件（%s），这些后端都满足：" % condition_text, ""]
            lines += ["- " + describe(entry) for entry in matches]
            lines.append("")
            lines.append("推荐：`%s`——%s。" % (matches[0]["id"], matches[0]["notes"]))
            lines.append("")
            lines.append("用 LoomQ 直接跑：`loomq run circuit.qasm --target %s`" % matches[0]["platform"])
        else:
            lines = ["Given %s, these backends qualify:" % condition_text, ""]
            lines += ["- " + describe(entry, "en") for entry in matches]
            lines.append("")
            lines.append("Recommended: `%s` - %s." % (matches[0]["id"], matches[0]["notes"]))
        return "\n".join(lines)

    alternatives = diagnosis.get("alternatives") or []
    if chinese:
        lines = ["没有后端能同时满足这些条件（%s）——如实说明比给一个错答案更有用。" % condition_text, ""]
        if alternatives:
            lines.append("目前能力上限最高的是：")
            lines += ["- " + describe(entry) for entry in alternatives]
        for finding in diagnosis.get("relaxations", []):
            lines.append(
                "若放宽「%s」，可选：%s"
                % (finding["drop"], "、".join("`%s`" % name for name in finding["would_allow"]))
            )
        lines.append("")
        lines.append("另一条路是把电路拆小，再分批提交。")
    else:
        lines = ["No backend satisfies all of these constraints (%s)." % condition_text, ""]
        if alternatives:
            lines.append("The largest available backends are:")
            lines += ["- " + describe(entry, "en") for entry in alternatives]
        for finding in diagnosis.get("relaxations", []):
            lines.append(
                "Dropping '%s' would allow: %s"
                % (finding["drop"], ", ".join("`%s`" % name for name in finding["would_allow"]))
            )
    return "\n".join(lines)


__all__ = [
    "constraints_from_text",
    "format_answer",
    "merge_constraints",
    "recommend",
]
