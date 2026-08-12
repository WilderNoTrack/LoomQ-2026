"""Circuit drawings — ASCII for the terminal, SVG for the browser.

A first-time user cannot read OpenQASM, but they can read a picture of wires and
boxes.  Both renderers walk the same layered schedule, so the terminal and the
web UI never disagree about what a circuit looks like.
"""

import html
from typing import Dict, List, Optional, Sequence, Tuple

from .ir import BarrierOp, Circuit, ConditionalOp, GateOp, MeasureOp, Operation, ResetOp

#: Short labels for the whitelist, plus the ones users hand-write.
_LABELS = {
    "h": "H",
    "x": "X",
    "y": "Y",
    "z": "Z",
    "s": "S",
    "sdg": "S+",
    "t": "T",
    "tdg": "T+",
    "rx": "Rx",
    "ry": "Ry",
    "rz": "Rz",
    "u1": "P",
    "sx": "√X",
    "sxdg": "√X+",
}


def gate_label(op: GateOp) -> str:
    label = _LABELS.get(op.name, op.name.upper())
    if op.params:
        label += "(%s)" % ",".join(_short_angle(value) for value in op.params)
    return label


def _short_angle(value: float) -> str:
    import math

    for denominator, text in ((1, "π"), (2, "π/2"), (3, "π/3"), (4, "π/4"), (8, "π/8")):
        for sign in (1, -1):
            if abs(value - sign * math.pi / denominator) < 1e-9:
                return ("-" if sign < 0 else "") + text
    return "%.3g" % value


def schedule(circuit: Circuit) -> List[List[Operation]]:
    """Group operations into columns that can be drawn side by side."""
    columns = []  # type: List[List[Operation]]
    occupied = []  # type: List[set]

    for op in circuit.ops:
        if isinstance(op, BarrierOp):
            continue
        qubits = set(op.qubits)
        if isinstance(op, MeasureOp):
            span = qubits
        elif len(qubits) > 1:
            span = set(range(min(qubits), max(qubits) + 1))
        else:
            span = qubits

        placed = False
        for index in range(len(columns) - 1, -1, -1):
            if occupied[index] & span:
                target = index + 1
                break
        else:
            target = 0
        if target < len(columns):
            columns[target].append(op)
            occupied[target] |= span
            placed = True
        if not placed:
            columns.append([op])
            occupied.append(set(span))
    return columns


# ------------------------------------------------------------------- ASCII


def text_diagram(circuit: Circuit, max_width: int = 110) -> str:
    """A monospace circuit drawing."""
    if circuit.num_qubits == 0:
        return "(empty circuit)"

    rows = circuit.num_qubits
    names = [circuit.qubit_label(index) for index in range(rows)]
    width = max(len(name) for name in names)
    wire = [["%*s ─" % (width, name)] for name in names]
    classical = ["%*s ═" % (width, "c")]

    for column in schedule(circuit):
        cells = ["─"] * rows
        written = []  # type: List[int]
        for op in column:
            if isinstance(op, MeasureOp):
                cells[op.qubit] = "M"
                written.append(op.clbit)
            elif isinstance(op, ResetOp):
                cells[op.qubit] = "0"
            elif isinstance(op, ConditionalOp):
                for qubit in op.qubits:
                    cells[qubit] = "?"
            elif isinstance(op, GateOp):
                if len(op.qubits) == 1:
                    cells[op.qubits[0]] = gate_label(op)
                else:
                    _draw_multi(op, cells)
        classical_cell = ",".join(str(index) for index in sorted(written)) if written else "═"
        cell_width = max(len(text) for text in cells + [classical_cell])
        for index in range(rows):
            wire[index].append(_pad(cells[index], cell_width))
        classical.append(_pad(classical_cell, cell_width, fill="═"))

    lines = ["".join(row) + "─" for row in wire]
    lines.append("".join(classical) + "═")
    text = "\n".join(lines)
    if max_width and max(len(line) for line in text.split("\n")) > max_width:
        return _wrap(text, max_width)
    return text


def _draw_multi(op: GateOp, cells: List[str]) -> None:
    qubits = op.qubits
    if op.name in ("cx", "cy", "cz", "cu1", "crz", "cry", "crx", "ch", "cp"):
        for control in qubits[:-1]:
            cells[control] = "●"
        cells[qubits[-1]] = "⊕" if op.name == "cx" else gate_label(op)
    elif op.name == "ccx":
        cells[qubits[0]] = "●"
        cells[qubits[1]] = "●"
        cells[qubits[2]] = "⊕"
    elif op.name == "swap":
        cells[qubits[0]] = "×"
        cells[qubits[1]] = "×"
    else:
        for position, qubit in enumerate(qubits):
            cells[qubit] = "%s[%d]" % (gate_label(op), position)
    low, high = min(qubits), max(qubits)
    for qubit in range(low + 1, high):
        if cells[qubit] == "─":
            cells[qubit] = "│"


def _pad(text: str, width: int, fill: str = "─") -> str:
    if text in ("─", "═"):
        return fill * (width + 2)
    padding = width - len(text)
    left = padding // 2
    right = padding - left
    return fill * (left + 1) + text + fill * (right + 1)


def _wrap(text: str, max_width: int) -> str:
    lines = text.split("\n")
    chunks = []  # type: List[str]
    start = 0
    longest = max(len(line) for line in lines)
    while start < longest:
        end = start + max_width
        chunks.append("\n".join(line[start:end] for line in lines))
        start = end
    return "\n\n".join(chunks)


# --------------------------------------------------------------------- SVG

_CELL = 56
_ROW = 44
_LEFT = 74


def svg_diagram(circuit: Circuit) -> str:
    """A self-contained SVG drawing that inherits the page's colours."""
    columns = schedule(circuit)
    rows = max(circuit.num_qubits, 1)
    width = _LEFT + max(len(columns), 1) * _CELL + 40
    height = rows * _ROW + 48

    parts = [
        '<svg viewBox="0 0 %d %d" width="100%%" role="img" '
        'aria-label="quantum circuit diagram" xmlns="http://www.w3.org/2000/svg">' % (width, height)
    ]
    parts.append(
        '<style>'
        '.wire{stroke:var(--diagram-wire);stroke-width:1.5}'
        '.link{stroke:var(--diagram-ink);stroke-width:2}'
        '.box{fill:var(--diagram-box);stroke:var(--diagram-ink);stroke-width:1.5;rx:6}'
        '.lbl{fill:var(--diagram-ink);font:600 13px ui-monospace,monospace;text-anchor:middle;'
        'dominant-baseline:central}'
        '.qname{fill:var(--diagram-muted);font:500 13px ui-monospace,monospace;'
        'dominant-baseline:central}'
        '.dot{fill:var(--diagram-ink)}'
        '</style>'
    )

    for row in range(rows):
        y = 28 + row * _ROW
        parts.append('<text class="qname" x="8" y="%d">%s</text>' % (y, html.escape(circuit.qubit_label(row))))
        parts.append('<line class="wire" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (_LEFT - 12, y, width - 20, y))

    for index, column in enumerate(columns):
        x = _LEFT + index * _CELL + _CELL // 2
        for op in column:
            parts.extend(_svg_operation(op, x))

    parts.append("</svg>")
    return "".join(parts)


def _y(row: int) -> int:
    return 28 + row * _ROW


def _svg_box(x: int, row: int, label: str, width: int = 38) -> List[str]:
    y = _y(row)
    return [
        '<rect class="box" x="%d" y="%d" width="%d" height="26"/>'
        % (x - width // 2, y - 13, width),
        '<text class="lbl" x="%d" y="%d">%s</text>' % (x, y, html.escape(label)),
    ]


def _svg_operation(op: Operation, x: int) -> List[str]:
    if isinstance(op, MeasureOp):
        return _svg_box(x, op.qubit, "M")
    if isinstance(op, ResetOp):
        return _svg_box(x, op.qubit, "|0>")
    if isinstance(op, ConditionalOp):
        return _svg_operation(op.body, x)
    if not isinstance(op, GateOp):
        return []

    qubits = op.qubits
    if len(qubits) == 1:
        label = gate_label(op)
        return _svg_box(x, qubits[0], label, width=max(38, 9 * len(label)))

    parts = [
        '<line class="link" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (x, _y(min(qubits)), x, _y(max(qubits)))
    ]
    if op.name == "swap":
        for qubit in qubits:
            y = _y(qubit)
            parts.append(
                '<line class="link" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (x - 6, y - 6, x + 6, y + 6)
            )
            parts.append(
                '<line class="link" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (x - 6, y + 6, x + 6, y - 6)
            )
        return parts

    for control in qubits[:-1]:
        parts.append('<circle class="dot" cx="%d" cy="%d" r="5"/>' % (x, _y(control)))
    target = qubits[-1]
    if op.name in ("cx", "ccx"):
        y = _y(target)
        parts.append(
            '<circle cx="%d" cy="%d" r="11" fill="none" stroke="var(--diagram-ink)" '
            'stroke-width="2"/>' % (x, y)
        )
        parts.append('<line class="link" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (x - 11, y, x + 11, y))
        parts.append('<line class="link" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (x, y - 11, x, y + 11))
    else:
        label = gate_label(op)
        parts.extend(_svg_box(x, target, label, width=max(38, 9 * len(label))))
    return parts


__all__ = ["gate_label", "schedule", "svg_diagram", "text_diagram"]
