#!/usr/bin/env python3
"""Submit to a real QPU and write the evidence bundle, using ``secrets.env``.

This is the manual, credentialed counterpart to ``loomq hardware``: it loads
``starter_kit/secrets.env`` first so the account details never have to be typed
into a shell (where they land in history) or exported globally.

    python3 tools/hardware_run.py --status
    python3 tools/hardware_run.py --target spinq   --shots 1024
    python3 tools/hardware_run.py --target originq --shots 1000

It writes ``evidence/files/<target>-<circuit>-circuit.qasm`` and
``-result.json`` and prints the block to paste into ``evidence/README.md``.
Nothing about the credential is printed.

Real hardware queues: SpinQ Cloud is usually minutes, Origin Quantum's Wukong
chip is usually hours. Submit early.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
for _path in (_KIT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import loomq_env  # noqa: E402

from loomq.cli import main as cli_main  # noqa: E402

CREDENTIALS = {
    "spinq": ("LOOMQ_SPINQ_USERNAME", "LOOMQ_SPINQ_KEYFILE", "LOOMQ_SPINQ_PLATFORM"),
    "originq": ("LOOMQ_ORIGINQ_TOKEN", "LOOMQ_ORIGINQ_CHIP"),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", default="spinq", choices=("spinq", "originq"))
    parser.add_argument("--file", default="circuits/bell.qasm")
    parser.add_argument("--shots", type=int, default=1024)
    parser.add_argument("--top", type=int, default=2)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--secrets")
    args = parser.parse_args(argv)

    loomq_env.load(args.secrets)

    if args.status:
        for platform, names in sorted(CREDENTIALS.items()):
            loomq_env.print_report("%s credentials:" % platform, names)
        print()
        return cli_main(["hardware", "--status"])

    missing = loomq_env.require(
        [name for name in CREDENTIALS[args.target] if not name.endswith(("PLATFORM", "CHIP"))]
    )
    if missing:
        print("Missing for %s: %s" % (args.target, ", ".join(missing)))
        print(
            "Fill them into starter_kit/secrets.env "
            "(copy secrets.env.example if you have not yet)."
        )
        return 2

    os.chdir(_KIT)
    return cli_main(
        [
            "hardware", args.file,
            "--target", args.target,
            "--shots", str(args.shots),
            "--top", str(args.top),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
