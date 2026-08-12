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


def command_qisa(args: argparse.Namespace) -> int:
    """Bonus: compile Hybrid-QASM into one LoomQ-Q instruction stream."""
    import collections

    from .hybrid.parser import split_classical_blocks
    from .qisa.assembler import listing, to_words
    from .qisa.compile import compile_unified

    source = _read(args.file)
    assembly = compile_unified(source)

    if args.listing:
        print(listing(assembly))
    elif args.words:
        print(to_words(assembly).rstrip())
    else:
        print(assembly.rstrip())

    if not args.run:
        return 0

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from riscv_emulator_loomq import QuantumRISCVEmulator  # type: ignore

    quantum_source, _ = split_classical_blocks(source)
    circuit = parse_qasm(quantum_source)
    width = measurement_width(circuit)
    expected = ideal_distribution(circuit, width)

    counts = collections.Counter()
    registers = collections.Counter()
    for seed in range(args.shots):
        emulator = QuantumRISCVEmulator(seed=seed)
        emulator.load_program(assembly)
        state = emulator.execute()
        value = sum(1 << bit for bit in range(width) if state.get("x%d" % (10 + bit), 0))
        counts["".join("1" if (value >> b) & 1 else "0" for b in range(width - 1, -1, -1))] += 1
        registers[tuple(sorted((k, v) for k, v in state.items() if int(k[1:]) < 10))] += 1

    observed = {key: value / float(args.shots) for key, value in counts.items()}
    fidelity = hellinger_fidelity(observed, expected)

    print("\n--- %d executions on the extended emulator ---" % args.shots)
    print("measured:")
    print(_histogram(observed))
    print("\nreference simulator (exact):")
    print(_histogram(expected))
    print("\nHellinger fidelity: %.6f  %s" % (fidelity, "ok" if fidelity >= 0.97 else "FAIL"))
    print("\nclassical outcomes reached (r1..r9):")
    for state, hits in registers.most_common():
        print("  %5.1f%%  %s" % (100.0 * hits / args.shots, dict(state) or "{}"))
    return 0 if fidelity >= 0.97 else 1


def command_hardware(args: argparse.Namespace) -> int:
    """Run on a real QPU and write the evidence bundle the rules ask for."""
    import json as _json

    from .backends.hardware import hardware_backend, hardware_report
    from .execution import compile_circuit
    from .result import build_result, normalize_counts, top_states, validate_result

    if args.status:
        for platform, info in sorted(hardware_report().items()):
            print(
                "[%s] %-9s %-20s %s"
                % ("ready" if info["ready"] else "  no ", platform, info["backend_id"], info["detail"])
            )
        return 0

    source = _read(args.file)
    circuit, lowered, native_ir = compile_circuit(source, args.target)
    width = measurement_width(circuit)
    expected = ideal_distribution(circuit, width)

    backend = hardware_backend(args.target)
    usable, reason = backend.availability()
    if not usable:
        print("LoomQ: %s is not ready — %s" % (backend.backend_id, reason), file=sys.stderr)
        return 1

    print("submitting %d qubits / %d gates to %s (%d shots)…"
          % (circuit.num_qubits, len(lowered.gates), backend.backend_id, args.shots))
    outcome = backend.execute(lowered, native_ir, args.shots)

    counts = _as_counts(outcome.counts, args.shots)
    counts = normalize_counts(counts, width)
    result = build_result(
        backend=backend.backend_id,
        job_id=outcome.job_id or "unknown",
        shots=sum(counts.values()),
        counts=counts,
        meta=dict(outcome.meta, target=args.target, source_file=os.path.basename(args.file)),
    )
    valid, why = validate_result(result)
    if not valid:
        print("LoomQ: the device returned a result LoomQ cannot certify: %s" % why, file=sys.stderr)

    observed = counts_to_distribution(result["counts"])
    fidelity = hellinger_fidelity(observed, expected)
    ideal_top = top_states(
        {key: int(round(value * 10000)) for key, value in expected.items()}, args.top
    )
    device_top = top_states(result["counts"], args.top)
    overlap = [state for state in device_top if state in ideal_top]

    print("\njob id  : %s" % result["job_id"])
    print("counts  :")
    print(_histogram(observed))
    print("\nideal   :")
    print(_histogram(expected))
    print("\ntop-%d ideal : %s" % (args.top, ", ".join(ideal_top)))
    print("top-%d device: %s" % (args.top, ", ".join(device_top)))
    print("main-peak hits: %d/%d   Hellinger fidelity: %.4f (noise expected)"
          % (len(overlap), len(ideal_top), fidelity))

    directory = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "evidence", "files")
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    stem = "%s-%s" % (args.target, os.path.splitext(os.path.basename(args.file))[0])
    qasm_path = os.path.join(directory, stem + "-circuit.qasm")
    json_path = os.path.join(directory, stem + "-result.json")
    with open(qasm_path, "w", encoding="utf-8") as handle:
        handle.write(native_ir)
    with open(json_path, "w", encoding="utf-8") as handle:
        _json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print("\nevidence written:")
    print("  %s" % qasm_path)
    print("  %s" % json_path)
    print("\npaste into starter_kit/evidence/README.md:\n")
    print("平台名称：%s" % backend.backend_id)
    print("平台 job ID：%s" % result["job_id"])
    print("运行时间：%s" % result["timestamp"])
    print("shots：%d" % result["shots"])
    print("实际执行的 QASM：starter_kit/evidence/files/%s" % os.path.basename(qasm_path))
    print("平台返回的原始结果：starter_kit/evidence/files/%s" % os.path.basename(json_path))
    return 0


def _as_counts(raw, shots: int):
    """Some builds return probabilities; the schema needs integer counts."""
    values = list(raw.values())
    if values and all(isinstance(value, float) for value in values):
        scaled = {key: int(round(value * shots)) for key, value in raw.items()}
        drift = shots - sum(scaled.values())
        if drift and scaled:
            top = max(scaled, key=lambda key: scaled[key])
            scaled[top] += drift
        return {key: value for key, value in scaled.items() if value > 0}
    return {key: int(value) for key, value in raw.items()}


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

    qisa = subparsers.add_parser(
        "qisa", help="compile Hybrid-QASM into one LoomQ-Q instruction stream (bonus)"
    )
    qisa.add_argument("file")
    qisa.add_argument("--words", action="store_true", help="emit raw .word encodings")
    qisa.add_argument("--listing", action="store_true", help="side-by-side encoding listing")
    qisa.add_argument("--run", action="store_true", help="execute and compare with the reference")
    qisa.add_argument("--shots", type=int, default=1000)
    qisa.set_defaults(handler=command_qisa)

    hardware = subparsers.add_parser(
        "hardware", help="run on a real QPU and write the evidence bundle"
    )
    hardware.add_argument("file", nargs="?", default="circuits/bell.qasm")
    hardware.add_argument("--target", default="spinq", choices=("spinq", "originq"))
    hardware.add_argument("--shots", type=int, default=1024)
    hardware.add_argument("--top", type=int, default=2, help="how many main peaks to compare")
    hardware.add_argument("--out", help="evidence directory (default evidence/files)")
    hardware.add_argument("--status", action="store_true", help="show credential readiness")
    hardware.set_defaults(handler=command_hardware)

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
