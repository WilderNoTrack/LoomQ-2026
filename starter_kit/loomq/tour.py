"""``python -m loomq tour`` — a first run for someone who knows no physics.

Four steps, each one a real execution rather than a description: flip a coin,
entangle two of them, watch the correlation appear, then transpile the same
circuit for three vendors.  Nothing here needs an API key, an account or a
network connection, which is the point — the first experience of quantum
computing should not begin with a registration form.
"""

import sys
from typing import List, Optional

from .circuits import bell, ghz
from .diagram import text_diagram
from .emitters import TARGETS
from .execution import run_circuit, transpile_qasm
from .qasm import parse_qasm
from .result import counts_to_distribution

_RULE = "─" * 68

_COIN = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
h q[0];
measure q -> c;
"""


def _histogram(distribution, indent: str = "    ") -> str:
    lines = []
    for key, value in sorted(distribution.items(), key=lambda item: (-item[1], item[0])):
        lines.append("%s|%s>  %5.1f%%  %s" % (indent, key, value * 100.0, "█" * int(value * 34)))
    return "\n".join(lines)


def _pause(interactive: bool) -> None:
    if not interactive:
        return
    try:
        input("\n    [Enter 继续] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)


def _step(number: int, title: str, body: List[str]) -> None:
    print("\n%s\n第 %d 步 · %s\n%s" % (_RULE, number, title, _RULE))
    for line in body:
        print(line)


def run_tour(interactive: bool = True, shots: int = 1024) -> int:
    print("\nLoomQ · 量子接入平权计划")
    print("这趟导览不需要任何物理背景，也不需要注册任何账号。")
    print("每一步都会真的跑一次电路，你看到的数字都是算出来的，不是写死的。")
    _pause(interactive)

    _step(1, "一枚量子硬币", [
        "  普通硬币抛出去，落地前它已经是正面或反面，只是你不知道。",
        "  量子比特不一样：在你测量之前，它同时是 0 和 1。",
        "  `h`（阿达马门）就是把一个确定的 0 变成「一半一半」的那个动作。",
        "",
        text_diagram(parse_qasm(_COIN)),
    ])
    result = run_circuit(_COIN, "braket", shots)
    print("\n    跑 %d 次，结果：" % shots)
    print(_histogram(counts_to_distribution(result["counts"])))
    _pause(interactive)

    _step(2, "两枚硬币，绑在一起", [
        "  现在加第二个比特，用 `cx` 把它们连起来。",
        "  这叫「纠缠」：两枚硬币各自看都是随机的，但永远同号。",
        "",
        text_diagram(parse_qasm(bell())),
    ])
    result = run_circuit(bell(), "braket", shots)
    distribution = counts_to_distribution(result["counts"])
    print("\n    跑 %d 次，结果：" % shots)
    print(_histogram(distribution))
    print("\n    注意：`01` 和 `10` 一次都没出现。这就是纠缠——")
    print("    不是「两个都随机」，而是「两个一起随机」。")
    _pause(interactive)

    _step(3, "三个也行，而且不用改代码", [
        "  同一套写法推广到三个比特，就是 GHZ 态。",
        "",
        text_diagram(parse_qasm(ghz(3))),
    ])
    result = run_circuit(ghz(3), "braket", shots)
    print("\n    跑 %d 次，结果：" % shots)
    print(_histogram(counts_to_distribution(result["counts"])))
    _pause(interactive)

    source = bell()
    _step(4, "同一个电路，三种量子机器的方言", [
        "  每个量子云平台都有自己的指令格式。LoomQ 的工作就是替你翻译。",
        "  下面是完全相同的贝尔态电路，翻译成三家平台各自的原生指令：",
    ])
    for target in TARGETS:
        print("\n    ── %s ──" % target)
        for line in transpile_qasm(source, target).rstrip().split("\n"):
            print("    " + line)
    _pause(interactive)

    print("\n%s\n接下来可以做什么\n%s" % (_RULE, _RULE))
    print("  python -m loomq web                     浏览器界面，用一句话描述就能出电路")
    print('  python -m loomq ask "做一个 4 比特 GHZ 态"   直接问，不用会写 QASM')
    print("  python -m loomq show circuits/bell.qasm  画出任意电路并算出理论分布")
    print("  python -m loomq doctor                  看看本机连上了哪些平台")
    print("\n  真机在等你：`spinq_cloud_qpu` 和 `originq_wukong` 都有免费额度。")
    print("  LoomQ 已经把翻译这一层做完了，剩下的只是注册一个账号。\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - convenience
    return run_tour(interactive="--quiet" not in (argv or sys.argv[1:]))


__all__ = ["run_tour"]
