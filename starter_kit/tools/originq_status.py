#!/usr/bin/env python3
"""Is Origin Quantum's compute service up, and is it our end or theirs?

Standard library only — no pyqpanda, no virtualenv. That matters: when a
submission fails you want to know whether the SDK, the credential, the circuit
or the platform is at fault, and a probe that shares none of the submission's
dependencies answers that cleanly.

    python3 tools/originq_status.py              # one probe of every machine type
    python3 tools/originq_status.py --control    # + what an invalid key returns
    python3 tools/originq_status.py --watch      # poll until the service returns

How to read the result:

    401 Unauthorized     the credential is wrong or expired — our end
    20045 maintenance    the credential was accepted and the platform is down
    anything else        the service is up; run tools/hardware_run.py

A probe never queues a task: the platform answers before anything is scheduled,
so this costs no quota. The token is read from ``secrets.env`` and is never
printed, and the request body is never echoed.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
for _path in (_KIT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import loomq_env  # noqa: E402

URL = "http://pyqanda-admin.qpanda.cn/api/taskApi/submitTask.json"

ORIGINIR = (
    "QINIT 2\nCREG 2\nH q[0]\nCNOT q[0], q[1]\n"
    "MEASURE q[0], c[0]\nMEASURE q[1], c[1]\n"
)

#: ``QMachineType`` values the platform accepts. 5 is the real chip; the rest
#: are cloud simulators, which is why probing them separates "Wukong is busy"
#: from "the whole compute service is down".
MACHINE_TYPES = (
    (0, "cloud full-amplitude simulator"),
    (1, "cloud noise simulator"),
    (2, "cloud partial-amplitude simulator"),
    (3, "cloud single-amplitude simulator"),
    (5, "real chip (Wukong, chipId 72)"),
)

MAINTENANCE = 20045
UNAUTHORIZED = 401


#: Chip ids worth sweeping. 72 is the retired Wukong that pyqpanda still points
#: at; 180 is the one that is actually online.
CANDIDATE_CHIPS = (1, 2, 3, 4, 5, 72, 73, 74, 75, 100, 180, 181)

RESOURCE_NULL = 10002
SUCCESS = 10000


def probe(token, machine_type=0, chip_id=180, timeout=45):
    """``(code, message)``; ``code`` is ``None`` when the request never landed."""
    payload = {
        "apiKey": token,
        "code": ORIGINIR,
        "codeLen": len(ORIGINIR),
        "taskFrom": 4,
        "qubitNum": 2,
        "classicalbitNum": 2,
        "taskName": "LoomQ availability probe",
        "chipId": chip_id,
        "isAmend": True,
        "mappingFlag": True,
        "circuitOptimization": True,
        "measureType": 1,
        "QMachineType": machine_type,
        "shot": 100,
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": "oqcs_auth=" + token,
            "origin-language": "en",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body.get("code"), body.get("message")
    except Exception as exc:  # noqa: BLE001 - transport failures are a result too
        return None, "%s: %s" % (type(exc).__name__, exc)


def verdict(code):
    if code is None:
        return "unreachable from this machine"
    if code == UNAUTHORIZED:
        return "credential rejected — check LOOMQ_ORIGINQ_TOKEN"
    if code == MAINTENANCE:
        return "credential accepted, this chip is under maintenance"
    if code == RESOURCE_NULL:
        return "no such chip id"
    return "service is up"


def chip_sweep(token):
    """Which chip ids are live.

    This exists because a wrong ``chipId`` is reported as "under maintenance",
    not as a bad request — and the server validates it for *every* machine type,
    including the pure cloud simulators. Probing one dead chip therefore looks
    exactly like a platform-wide outage. Sweeping the ids is what separates the
    two, and it is how LoomQ found that pyqpanda's ``real_chip_type.origin_72``
    points at a retired machine while the live Wukong is simply chip 180.
    """
    print("%-8s %-8s %s" % ("chipId", "code", "message"))
    print("-" * 88)
    live = []
    for chip_id in CANDIDATE_CHIPS:
        code, message = probe(token, machine_type=5, chip_id=chip_id)
        if code == SUCCESS:
            live.append(chip_id)
        print("%-8s %-8s %s" % (chip_id, code, message))
    print()
    if live:
        print("live chip ids: %s" % ", ".join(str(chip) for chip in live))
        print("set LOOMQ_ORIGINQ_CHIP to one of these in secrets.env")
    else:
        print("no chip id accepted a task")
    return 0 if live else 1


def snapshot(token, control=False):
    print("%-3s %-34s %-8s %s" % ("id", "machine type", "code", "message"))
    print("-" * 92)
    codes = []
    for machine_type, label in MACHINE_TYPES:
        code, message = probe(token, machine_type)
        codes.append(code)
        print("%-3d %-34s %-8s %s" % (machine_type, label, code, message))

    if control:
        print()
        code, message = probe("0" * 96)
        print("%-3s %-34s %-8s %s" % ("-", "control: deliberately invalid key", code, message))
        print("    (a different code here proves the real key was accepted)")

    distinct = {code for code in codes}
    print("\nverdict: %s" % verdict(codes[-1]))
    if distinct == {MAINTENANCE}:
        print("every path including the pure cloud simulators is down, so this is a"
              "\nplatform-wide outage rather than a busy or broken Wukong chip.")
    return 0 if codes[-1] not in (MAINTENANCE, UNAUTHORIZED, None) else 1


def watch(token, interval, hours):
    """Poll until the service returns. Prints only on a state change."""
    deadline = time.time() + hours * 3600
    unreachable = 0
    while time.time() < deadline:
        code, message = probe(token)
        if code is None:
            unreachable += 1
            if unreachable in (6, 24):
                print("originq: unreachable for %d consecutive probes (%s)"
                      % (unreachable, message), flush=True)
            time.sleep(interval)
            continue
        unreachable = 0
        if code != MAINTENANCE:
            print("originq: compute service is BACK — code=%s message=%s"
                  % (code, message), flush=True)
            return 0
        time.sleep(interval)
    print("originq: still under maintenance after %d hours" % hours, flush=True)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--control", action="store_true",
                        help="also show what an invalid key returns")
    parser.add_argument("--chips", action="store_true",
                        help="sweep chip ids and report which are live")
    parser.add_argument("--watch", action="store_true",
                        help="poll until the service returns, printing only on change")
    parser.add_argument("--interval", type=int, default=900, help="seconds between polls")
    parser.add_argument("--hours", type=int, default=72, help="give up after this long")
    parser.add_argument("--secrets")
    args = parser.parse_args(argv)

    loomq_env.load(args.secrets)
    token = os.environ.get("LOOMQ_ORIGINQ_TOKEN")
    if not token:
        print("LOOMQ_ORIGINQ_TOKEN is not set; fill it into starter_kit/secrets.env")
        return 2

    if args.chips:
        return chip_sweep(token)
    if args.watch:
        return watch(token, args.interval, args.hours)
    return snapshot(token, control=args.control)


if __name__ == "__main__":
    sys.exit(main())
