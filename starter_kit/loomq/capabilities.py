"""The official backend capability table, loaded as data.

``backend_capabilities.json`` is the *only* basis formal L2 scoring uses to
derive the correct answer set for a "which platform should I use" question.  So
LoomQ loads that exact file and filters it in code, rather than pasting the
table into a prompt and hoping the model recites it correctly.  The model's job
is to turn a sentence into constraints; deciding which backends satisfy them is
arithmetic, and arithmetic belongs in Python.
"""

import json
import os
from typing import Any, Dict, List, Optional, Sequence

_TABLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend_capabilities.json",
)

_CACHE = None  # type: Optional[Dict[str, Any]]

#: Queue characteristics, ordered from "instant" to "slowest".
QUEUE_ORDER = ("none", "minutes_to_hours", "hours")

#: Cost tiers, ordered from cheapest.
COST_ORDER = ("free", "free_quota", "paid")


def load_table(path: Optional[str] = None) -> Dict[str, Any]:
    """Read and cache the capability table."""
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    with open(path or _TABLE_PATH, encoding="utf-8") as handle:
        table = json.load(handle)
    if path is None:
        _CACHE = table
    return table


def backends(path: Optional[str] = None) -> List[Dict[str, Any]]:
    return list(load_table(path)["backends"])


def backend_ids(path: Optional[str] = None) -> List[str]:
    return [entry["id"] for entry in backends(path)]


def find(backend_id: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for entry in backends(path):
        if entry["id"] == backend_id:
            return entry
    return None


def select(
    min_qubits: Optional[int] = None,
    kind: Optional[str] = None,
    no_queue: Optional[bool] = None,
    max_cost: Optional[str] = None,
    requires_account: Optional[bool] = None,
    platform: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every backend satisfying all supplied constraints.

    ``kind`` accepts ``simulator``, ``qpu``, ``cloud`` and the convenience value
    ``hardware`` (``qpu`` or ``cloud``, i.e. anything that can reach a real
    device).  ``max_cost`` is the most expensive tier the user will accept.
    """
    matches = []
    for entry in backends():
        if min_qubits is not None and entry["max_qubits"] < min_qubits:
            continue
        if kind is not None:
            if kind == "hardware":
                if entry["kind"] not in ("qpu", "cloud"):
                    continue
            elif entry["kind"] != kind:
                continue
        if no_queue and entry["queue"] != "none":
            continue
        if max_cost is not None and max_cost in COST_ORDER:
            if COST_ORDER.index(entry["cost"]) > COST_ORDER.index(max_cost):
                continue
        if requires_account is not None and entry["requires_account"] != requires_account:
            continue
        if platform is not None and entry["platform"] != platform:
            continue
        matches.append(entry)
    return matches


def closest_alternatives(min_qubits: int, limit: int = 2) -> List[Dict[str, Any]]:
    """Largest backends available, for when no option satisfies the request."""
    ordered = sorted(backends(), key=lambda entry: -entry["max_qubits"])
    return ordered[:limit]


def describe(entry: Dict[str, Any], language: str = "zh") -> str:
    if language == "zh":
        kind = {"simulator": "模拟器", "qpu": "真机", "cloud": "云端"}.get(entry["kind"], entry["kind"])
        queue = {
            "none": "无排队",
            "minutes_to_hours": "分钟～小时级排队",
            "hours": "小时级排队",
        }.get(entry["queue"], entry["queue"])
        cost = {"free": "免费", "free_quota": "有免费额度", "paid": "付费"}.get(
            entry["cost"], entry["cost"]
        )
        account = "需要注册账号" if entry["requires_account"] else "无需账号"
        return "`%s`（%s，%s，最多 %d 比特，%s，%s，%s）" % (
            entry["id"],
            entry["name"],
            kind,
            entry["max_qubits"],
            queue,
            cost,
            account,
        )
    return "`%s` (%s, %s, up to %d qubits, queue=%s, cost=%s)" % (
        entry["id"],
        entry["name"],
        entry["kind"],
        entry["max_qubits"],
        entry["queue"],
        entry["cost"],
    )


def summary_for_prompt() -> str:
    """A compact rendering of the table for the model's system prompt."""
    lines = []
    for entry in backends():
        lines.append(
            "- %s | platform=%s | kind=%s | max_qubits=%d | queue=%s | cost=%s | account=%s"
            % (
                entry["id"],
                entry["platform"],
                entry["kind"],
                entry["max_qubits"],
                entry["queue"],
                entry["cost"],
                "yes" if entry["requires_account"] else "no",
            )
        )
    return "\n".join(lines)


__all__ = [
    "COST_ORDER",
    "QUEUE_ORDER",
    "backend_ids",
    "backends",
    "closest_alternatives",
    "describe",
    "find",
    "load_table",
    "select",
    "summary_for_prompt",
]
