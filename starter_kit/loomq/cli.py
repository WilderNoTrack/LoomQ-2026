"""``python -m loomq`` — the command line front door.

Deliberately usable by someone who has never written a line of quantum code:

    python -m loomq tour                     what this is, in 60 seconds
    python -m loomq ask "make a Bell state"  describe it, get a circuit
    python -m loomq web                      the same thing in a browser

and by someone who has:

    python -m loomq transpile bell.qasm --target originq
    python -m loomq run bell.qasm --target braket --shots 8192
    python -m loomq hybrid program.hqasm --verify
    python -m loomq selftest
    python -m loomq doctor
"""

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .circuits import official_suite
from .diagram import text_diagram
from .emitters import TARGETS
from .errors import LoomQError
from .execution import describe_environment, run_circuit, transpile_qasm
from .qasm import parse_qasm
from .result import counts_to_distribution, hellinger_fidelity
from .sim import ideal_distribution, measurement_width
from .verify import verify_target_ir

_BAR = "█"


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _histogram(distribution, limit: int = 12) -> str:
    ordered = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))[:limit]
    lines = []
    for key, value in ordered:
        lines.append("  |%s>  %6.2f%%  %s" % (key, value * 100.0, _BAR * max(1, int(value * 40))))
    return "\n".join(lines)


# ------------------------------------------------------------------ commands


def command_transpile(args: argparse.Namespace) -> int:
    source = _read(args.file)
    targets = TARGETS if args.target == "all" else (args.target,)
    for index, target in enumerate(targets):
        if len(targets) > 1:
            print("%s--- %s ---" % ("\n" if index else "", target))
        print(transpile_qasm(source, target).rstrip())
    return 0


def command_run(args: argparse.Namespace) -> int:
    source = _read(args.file)
    result = run_circuit(source, args.target, args.shots)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("backend : %s" % result["backend"])
    print("executor: %s" % result["meta"].get("executor", "unknown"))
    print("job id  : %s" % result["job_id"])
    print("shots   : %d" % result["shots"])
    print("counts  :")
    print(_histogram(counts_to_distribution(result["counts"])))
    return 0


def command_show(args: argparse.Namespace) -> int:
    circuit = parse_qasm(_read(args.file))
    print(text_diagram(circuit))
    print()
    print("qubits %(qubits)d | gates %(gates)d | depth %(depth)d" % circuit.summary())
    print()
    print(_histogram(ideal_distribution(circuit, measurement_width(circuit))))
    return 0


def command_ask(args: argparse.Namespace) -> int:
    from .agent import respond

    result = respond(" ".join(args.prompt))
    print(result.text)
    if args.trace:
        print("\n--- trace ---")
        print(json.dumps(result.trace, ensure_ascii=False, indent=2))
    return 0


def command_chat(args: argparse.Namespace) -> int:
    from .agent import respond

    print("LoomQ — 用一句话说你想做什么，Ctrl-C 退出。")
    print('例如："做一个 3 比特的 GHZ 纠缠态并全部测量"\n')
    while True:
        try:
            prompt = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt in ("exit", "quit", "q"):
            return 0
        try:
            print("\n" + respond(prompt).text + "\n")
        except LoomQError as exc:
            print("\n[LoomQ] %s\n" % exc)


def command_hybrid(args: argparse.Namespace) -> int:
    from .hybrid import compile_hybrid, verify_all

    source = _read(args.file)
    operations, assembly = compile_hybrid(source)
    print("--- quantum operations ---")
    for line in operations:
        print("  " + line)
    print("\n--- RISC-V assembly ---")
    print(assembly.rstrip())
    if args.verify:
        ok, records = verify_all(source)
        print("\n--- exhaustive injection check ---")
        for record in records:
            print(
                "  %s -> %s %s"
                % (
                    record["injection"],
                    record["actual"],
                    "ok" if record["match"] else "MISMATCH expected %s" % record["expected"],
                )
            )
        print("\n%s" % ("all injections match the reference interpreter" if ok else "FAILED"))
        return 0 if ok else 1
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    from .agent.llm import REQUIRED_ENV, is_configured

    report = describe_environment()
    print("LoomQ %s" % __version__)
    print("python %s" % sys.version.split()[0])
    print("\nexecution policy : %s" % report["executor_policy"])
    print("sampling mode    : %s" % report["sampling"])
    print("\nplatforms:")
    for name, info in sorted(report["platforms"].items()):
        mark = "yes" if info["sdk_available"] else "no "
        print("  [%s] %-9s %-26s %s" % (mark, name, info["simulator_id"], info["detail"]))
    print("\nL2 model service:")
    if is_configured():
        for name in REQUIRED_ENV:
            value = os.environ.get(name, "")
            shown = "set (%d characters)" % len(value) if "KEY" in name else value
            print("  %-26s %s" % (name, shown))
    else:
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
        print("  not configured; missing %s" % ", ".join(missing))
        print("  L1 and L3 work without it; L2 needs a model service.")
    return 0


def command_selftest(args: argparse.Namespace) -> int:
    """Round-trip every circuit through every target, then fuzz L3."""
    from .hybrid import verify_all
    from .hybrid.fuzz import generate

    failures = 0
    print("L1 — transpile, re-import and compare distributions")
    print("%-12s %-9s %-10s %s" % ("circuit", "target", "roundtrip", "run() fidelity"))
    for name, qasm in official_suite():
        circuit = parse_qasm(qasm)
        expected = ideal_distribution(circuit, measurement_width(circuit))
        for target in TARGETS:
            native = transpile_qasm(qasm, target)
            ok, fidelity, detail = verify_target_ir(qasm, target, native)
            result = run_circuit(qasm, target, args.shots)
            observed = hellinger_fidelity(counts_to_distribution(result["counts"]), expected)
            status = "ok" if ok and observed >= 0.97 else "FAIL"
            if status == "FAIL":
                failures += 1
            print("%-12s %-9s %-10s %.6f  %s" % (name, target, status, observed, "" if ok else detail))

    print("\nL3 — %d random Hybrid-QASM programs, every measurement injection" % args.cases)
    mismatches = 0
    for seed in range(args.cases):
        ok, _ = verify_all(generate(seed=seed))
        if not ok:
            mismatches += 1
    failures += mismatches
    print("  %d/%d programs match the reference interpreter" % (args.cases - mismatches, args.cases))

    print("\n%s" % ("SELFTEST PASSED" if failures == 0 else "SELFTEST FAILED (%d)" % failures))
    return 0 if failures == 0 else 1


def command_web(args: argparse.Namespace) -> int:
    from .web import serve

    return serve(host=args.host, port=args.port, open_browser=not args.no_browser)


def command_tour(args: argparse.Namespace) -> int:
    from .tour import run_tour

    return run_tour(interactive=not args.quiet)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loomq",
        description="LoomQ — one middle layer for SpinQ, Origin Quantum and AWS Braket.",
    )
    parser.add_argument("--version", action="version", version="LoomQ " + __version__)
    subparsers = parser.add_subparsers(dest="command")

    transpile = subparsers.add_parser("transpile", help="OpenQASM 2.0 -> a backend's native IR")
    transpile.add_argument("file", help="path to a .qasm file, or - for stdin")
    transpile.add_argument("--target", default="all", choices=list(TARGETS) + ["all"])
    transpile.set_defaults(handler=command_transpile)

    run = subparsers.add_parser("run", help="execute a circuit and print the counts")
    run.add_argument("file")
    run.add_argument("--target", default="braket", choices=TARGETS)
    run.add_argument("--shots", type=int, default=8192)
    run.add_argument("--json", action="store_true", help="print the raw result schema")
    run.set_defaults(handler=command_run)

    show = subparsers.add_parser("show", help="draw a circuit and its ideal outcome")
    show.add_argument("file")
    show.set_defaults(handler=command_show)

    ask = subparsers.add_parser("ask", help="describe what you want in plain language")
    ask.add_argument("prompt", nargs="+")
    ask.add_argument("--trace", action="store_true", help="show how the answer was reached")
    ask.set_defaults(handler=command_ask)

    chat = subparsers.add_parser("chat", help="interactive session with the agent")
    chat.set_defaults(handler=command_chat)

    hybrid = subparsers.add_parser("hybrid", help="compile Hybrid-QASM (L3)")
    hybrid.add_argument("file")
    hybrid.add_argument("--verify", action="store_true", help="check every measurement injection")
    hybrid.set_defaults(handler=command_hybrid)

    doctor = subparsers.add_parser("doctor", help="what is installed and configured")
    doctor.set_defaults(handler=command_doctor)

    selftest = subparsers.add_parser("selftest", help="run LoomQ's own end-to-end checks")
    selftest.add_argument("--shots", type=int, default=8192)
    selftest.add_argument("--cases", type=int, default=100)
    selftest.set_defaults(handler=command_selftest)

    web = subparsers.add_parser("web", help="launch the browser interface")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--no-browser", action="store_true")
    web.set_defaults(handler=command_web)

    tour = subparsers.add_parser("tour", help="a guided first run for absolute beginners")
    tour.add_argument("--quiet", action="store_true", help="do not wait for keypresses")
    tour.set_defaults(handler=command_tour)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except LoomQError as exc:
        print("LoomQ: %s" % exc, file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print("LoomQ: no such file: %s" % exc.filename, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


__all__ = ["build_parser", "main"]
