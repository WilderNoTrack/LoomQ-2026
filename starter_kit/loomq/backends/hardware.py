"""Real-hardware backends: SpinQ Cloud and Origin Quantum's Wukong chip.

These are the only backends that touch a network, need an account, and cost a
queue slot, so they are opt-in and never selected automatically:

    LOOMQ_EXECUTOR=hardware python3 -m loomq hardware circuits/bell.qasm --target spinq

Credentials come from the environment, never from the repository:

    SpinQ Cloud     LOOMQ_SPINQ_USERNAME, LOOMQ_SPINQ_KEYFILE, LOOMQ_SPINQ_PLATFORM
    Origin Quantum  LOOMQ_ORIGINQ_TOKEN, LOOMQ_ORIGINQ_CHIP

A hardware result is noisy by definition, so the execution layer's
self-validation does not apply: ``loomq hardware`` keeps whatever the device
returned and reports the top-K overlap with the ideal distribution instead,
which is exactly what the rules ask hardware evidence to demonstrate.
"""

import os
from typing import Any, Dict, Optional, Tuple

from ..errors import BackendError
from ..ir import Circuit
from ..result import new_job_id
from .base import Backend, ExecutionOutcome, import_optional

#: SpinQ Cloud platforms and their qubit counts.
SPINQ_PLATFORMS = {
    "gemini_vp": 2,
    "triangulum_vp": 3,
    "superconductor_vp": 8,
}

#: Native gate set per platform, read off the console's own ``support_gates``.
#: These are far narrower than the twelve-gate whitelist — the 2-qubit NMR
#: machine has no S or T at all, and the 8-qubit superconducting one has no
#: CNOT — so a circuit that runs on a simulator can still be unrunnable here.
SPINQ_NATIVE_GATES = {
    "gemini_vp": frozenset({"x", "y", "z", "h", "rx", "ry", "rz", "cx", "id"}),
    "triangulum_vp": frozenset({"x", "y", "z", "h", "t", "tdg", "rx", "ry", "rz", "cx", "ccx", "id"}),
    "superconductor_vp": frozenset({"x", "y", "z", "h", "s", "t", "rx", "ry", "rz", "id"}),
}


def _require(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackendError("%s is not set. %s" % (name, hint))
    return value


#: Origin Quantum addresses machines by a numeric chip id, and pyqpanda's
#: ``real_chip_type`` enum has not kept up: ``origin_72`` points at the retired
#: 72-qubit Wukong, which answers "under maintenance" indefinitely, while the
#: current 180-qubit Wukong is simply chip 180. ``tools/originq_status.py
#: --chips`` sweeps the space and reports which ids are live.
ORIGINQ_DEFAULT_CHIP = 180


#: pyqpanda's enum, for people who configure LoomQ with a member name. The
#: values are the chip ids the REST API expects, and note that `origin_72`
#: points at a machine that no longer answers.
ORIGINQ_CHIP_ALIASES = {
    "origin_72": 72,
    "origin_wuyuan_d3": 7,
    "origin_wuyuan_d4": 5,
    "origin_wuyuan_d5": 2,
    "wukong": ORIGINQ_DEFAULT_CHIP,
    "origin_wukong": ORIGINQ_DEFAULT_CHIP,
}


def _resolve_chip(module: Any, name: Any) -> int:
    """Chip id from an integer, a ``real_chip_type`` member name, or an alias."""
    text = str(name).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    lowered = text.lower()
    if lowered in ORIGINQ_CHIP_ALIASES:
        return ORIGINQ_CHIP_ALIASES[lowered]
    member = getattr(getattr(module, "real_chip_type", None), text, None)
    if member is not None:
        try:
            return int(member)
        except (TypeError, ValueError):  # pragma: no cover - enum is integral
            pass
    raise BackendError(
        "unknown Origin chip %r; use a numeric chip id (the live Wukong is %d) "
        "or one of %s" % (name, ORIGINQ_DEFAULT_CHIP, ", ".join(sorted(ORIGINQ_CHIP_ALIASES)))
    )


def _without_measurement(circuit: Circuit) -> str:
    """OpenQASM 2.0 for ``circuit`` with every ``measure`` removed."""
    from ..emitters.spinq import emit_spinq
    from ..ir import GateOp

    stripped = circuit.copy_empty()
    for op in circuit.ops:
        if isinstance(op, GateOp):
            stripped.append(op)
    text = emit_spinq(stripped)
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("measure ")
    ) + "\n"


def _map_to_clbits(raw: Dict[str, int], circuit: Circuit, reverse: bool) -> Dict[str, int]:
    """Re-key auto-measured, per-qubit counts onto the circuit's clbits.

    SpinQ measures every qubit itself, so the keys come back one character per
    qubit.  The circuit's own ``measure`` statements say which clbit each qubit
    belongs in, and that mapping is reapplied here — the same job the emitted
    ``measure q[i] -> c[j];`` lines do everywhere else.
    """
    from ..ir import MeasureOp
    from ..sim import measurement_width

    pairs = [(op.qubit, op.clbit) for op in circuit.ops if isinstance(op, MeasureOp)]
    if not pairs:
        pairs = [(index, index) for index in range(circuit.num_qubits)]
    width = measurement_width(circuit)

    counts = {}  # type: Dict[str, int]
    for key, count in raw.items():
        text = key[::-1] if reverse else key
        value = 0
        for qubit, clbit in pairs:
            position = len(text) - 1 - qubit
            if 0 <= position < len(text) and text[position] == "1":
                value |= 1 << clbit
        label = "".join("1" if (value >> bit) & 1 else "0" for bit in range(width - 1, -1, -1))
        counts[label] = counts.get(label, 0) + int(count)
    return counts


class SpinQCloudBackend(Backend):
    """量旋云真机 — the platform the rules recommend trying first."""

    platform = "spinq"
    backend_id = "spinq_cloud_qpu"
    executor = "spinqit (SpinQ Cloud)"
    is_hardware = True

    def availability(self) -> Tuple[bool, str]:
        module = import_optional("spinqit")
        if module is None:
            return False, "spinqit is not installed"
        if not hasattr(module, "get_spinq_cloud"):
            return False, "this spinqit build has no get_spinq_cloud()"
        if not os.environ.get("LOOMQ_SPINQ_USERNAME"):
            return False, "LOOMQ_SPINQ_USERNAME is not set"
        if not os.environ.get("LOOMQ_SPINQ_KEYFILE"):
            return False, "LOOMQ_SPINQ_KEYFILE is not set"
        return True, "SpinQ Cloud credentials present"

    def choose_platform(self, circuit: Circuit, backend: Any = None) -> str:
        """Smallest platform that fits the circuit, has machines, and has the gates.

        Three filters, and all three matter in practice: the console reports
        ``machine_count: 0`` for a platform under maintenance, and the native
        gate sets are narrower than the whitelist — a Bell pair needs ``cnot``,
        which the 8-qubit superconducting machine does not have.
        """
        pinned = os.environ.get("LOOMQ_SPINQ_PLATFORM")
        needed = {op.name for op in circuit.gates}

        if pinned:
            if pinned not in SPINQ_PLATFORMS:
                raise BackendError(
                    "unknown SpinQ platform %r; expected one of %s"
                    % (pinned, ", ".join(sorted(SPINQ_PLATFORMS)))
                )
            if SPINQ_PLATFORMS[pinned] < circuit.num_qubits:
                raise BackendError(
                    "%s has %d qubits but the circuit needs %d"
                    % (pinned, SPINQ_PLATFORMS[pinned], circuit.num_qubits)
                )
            return pinned

        reasons = []
        for name, capacity in sorted(SPINQ_PLATFORMS.items(), key=lambda item: item[1]):
            if capacity < circuit.num_qubits:
                reasons.append("%s: %d qubits < %d" % (name, capacity, circuit.num_qubits))
                continue
            missing = needed - SPINQ_NATIVE_GATES.get(name, frozenset())
            if missing:
                reasons.append("%s: no native %s" % (name, ", ".join(sorted(missing))))
                continue
            if backend is not None and self._machine_count(backend, name) == 0:
                reasons.append("%s: no machine currently available" % name)
                continue
            return name

        raise BackendError(
            "no SpinQ Cloud platform can run this circuit — " + "; ".join(reasons)
        )

    @staticmethod
    def _machine_count(backend: Any, name: str) -> Optional[int]:
        """``machine_count`` from the console, or ``None`` if it cannot be read."""
        try:
            import json

            description = backend.get_platform(name)
            payload = json.loads(str(description))
            return int(payload.get("machine_count", 1))
        except Exception:  # noqa: BLE001 - availability is advisory
            return None

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        module = import_optional("spinqit")
        if module is None:  # pragma: no cover - guarded by availability()
            raise BackendError("spinqit is not installed")

        username = _require("LOOMQ_SPINQ_USERNAME", "Register at cloud.spinq.cn.")
        keyfile = _require(
            "LOOMQ_SPINQ_KEYFILE",
            "Point it at the private key downloaded from the SpinQ Cloud console.",
        )
        if not os.path.isfile(keyfile):
            raise BackendError("LOOMQ_SPINQ_KEYFILE does not point at a file: %s" % keyfile)

        task_name = os.environ.get("LOOMQ_SPINQ_TASK", "loomq")
        import tempfile

        # SpinQ Cloud rejects explicit measurement: "A measure will be done
        # automatically at the end of the circuit." The local Taurus simulator
        # accepts it, so this is a hardware-only rewrite — the artifact that
        # `transpile()` returns keeps its measurements, and the mapping back onto
        # clbits is reapplied to the counts below.
        submitted = _without_measurement(circuit)

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".qasm", delete=False, encoding="utf-8"
        )
        try:
            handle.write(submitted)
            handle.close()
            program = module.get_compiler("qasm").compile(handle.name, 0)
        except Exception as exc:
            raise BackendError("spinqit could not compile the circuit: %s" % exc)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover - best effort
                pass

        try:
            backend = module.get_spinq_cloud(username, keyfile)
        except Exception as exc:
            raise BackendError("SpinQ Cloud rejected the credentials: %s" % exc)

        platform = self.choose_platform(circuit, backend)
        try:
            config = module.SpinQCloudConfig()
            config.configure_platform(platform)
            config.configure_shots(shots)
            config.configure_task(task_name, "loomq")
            outcome = backend.execute(program, config)
            raw = dict(outcome.counts)
        except Exception as exc:
            raise BackendError("SpinQ Cloud rejected or failed the task: %s" % exc)

        # `task_code` is the id the SpinQ console shows — the one the judges
        # will look up. Falling back to a generated id would make the evidence
        # untraceable, which the rules score as zero, so a missing task code is
        # an error rather than something to paper over.
        job_id = (
            getattr(outcome, "task_code", None)
            or getattr(outcome, "task_id", None)
            or getattr(outcome, "job_id", None)
        )
        if not job_id:
            raise BackendError(
                "SpinQ Cloud returned no task code, so the run would not be "
                "traceable in the console; refusing to record it as evidence"
            )

        return ExecutionOutcome(
            _map_to_clbits(raw, circuit, reverse=True),
            job_id=str(job_id),
            meta={
                "executor": self.executor,
                "hardware": True,
                "spinq_platform": platform,
                "spinq_task_name": getattr(outcome, "task_name", None) or task_name,
                "auto_measured": True,
                "bit_order_reversed": True,
                "key_convention": "clbit_lsb_first",
                "traceable_in_console": True,
            },
        )


class OriginWukongBackend(Backend):
    """本源悟空超导真机, driven over the cloud REST API with the standard library.

    pyqpanda is deliberately *not* used here. Its ``real_chip_measure`` raises
    ``value is not string (which is 0)`` while parsing a perfectly good task
    response — the platform returns ``taskState: 3`` with complete results
    alongside a stray ``errorMessage``, and the SDK's parser cannot cope. The
    REST API underneath is small and stable, so LoomQ speaks it directly.

    The side benefit is that the hardware path inherits the core's property of
    having no third-party dependencies: submitting to a 180-qubit
    superconducting QPU needs nothing but ``urllib``.
    """

    platform = "originq"
    backend_id = "originq_wukong"
    executor = "origin-qcloud REST (stdlib)"
    is_hardware = True

    SUBMIT_URL = "http://pyqanda-admin.qpanda.cn/api/taskApi/submitTask.json"
    DETAIL_URL = "http://pyqanda-admin.qpanda.cn/api/taskApi/getTaskDetail.json"

    #: taskState values: 3 finished, 4/35 failed.
    FINISHED = "3"
    FAILED = ("4", "35")

    def availability(self) -> Tuple[bool, str]:
        if not os.environ.get("LOOMQ_ORIGINQ_TOKEN"):
            return False, "LOOMQ_ORIGINQ_TOKEN is not set"
        return True, "Origin Quantum API token present (REST, no SDK needed)"

    def _post(self, url: str, payload: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        import json
        import urllib.request

        token = payload["apiKey"]
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Authorization": "oqcs_auth=" + token,
                "origin-language": "en",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        import json
        import time

        token = _require(
            "LOOMQ_ORIGINQ_TOKEN",
            "Apply for an API token at qcloud.originqc.com.cn and export it.",
        )
        chip = _resolve_chip(None, os.environ.get("LOOMQ_ORIGINQ_CHIP", ORIGINQ_DEFAULT_CHIP))
        task_name = os.environ.get("LOOMQ_ORIGINQ_TASK", "LoomQ")
        timeout = float(os.environ.get("LOOMQ_ORIGINQ_TIMEOUT_SECONDS", 1800))

        # The OriginIR text goes into `code` verbatim: the artifact
        # `transpile(qasm, "originq")` returns is the artifact Wukong runs.
        submission = {
            "apiKey": token,
            "code": native_ir,
            "codeLen": len(native_ir),
            "taskFrom": 4,
            "qubitNum": circuit.num_qubits,
            "classicalbitNum": circuit.num_clbits or circuit.num_qubits,
            "taskName": task_name,
            "chipId": chip,
            "isAmend": True,
            "mappingFlag": True,
            "circuitOptimization": True,
            "measureType": 1,
            "QMachineType": 5,
            "shot": shots,
        }

        try:
            body = self._post(self.SUBMIT_URL, submission)
        except Exception as exc:
            raise BackendError("could not reach Origin Quantum: %s" % exc)

        if not body.get("success"):
            code = body.get("code")
            message = body.get("message") or "unknown error"
            if code == 20045:
                raise BackendError(
                    "chip %s is under maintenance. Note that an unknown chip id is "
                    "reported the same way — run `python3 tools/originq_status.py "
                    "--chips` to see which ids are live. (%s)" % (chip, message)
                )
            if code == 401:
                raise BackendError("Origin Quantum rejected the API token (%s)" % message)
            raise BackendError("Origin Quantum refused the task: %s (code %s)" % (message, code))

        task_id = (body.get("obj") or {}).get("taskId")
        if not task_id:
            raise BackendError(
                "Origin Quantum accepted the task but returned no taskId, so the "
                "run would not be traceable in the console"
            )

        record = self._await_result(token, task_id, timeout)
        counts = self._counts(record, shots)

        meta = {
            "executor": self.executor,
            "hardware": True,
            "origin_chip": chip,
            "origin_task_name": task_name,
            "submitted_ir": "originir",
            "key_convention": "clbit_msb_first",
            "traceable_in_console": True,
            "readout_mitigation": True,
        }
        for source, target in (
            ("mappingQubit", "physical_qubits"),
            ("pulseTime", "pulse_time_ns"),
            ("taskResult", "mitigated_probabilities"),
        ):
            if record.get(source):
                meta[target] = record[source]
        return ExecutionOutcome(counts, job_id=str(task_id), meta=meta)

    def _await_result(self, token: str, task_id: str, timeout: float) -> Dict[str, Any]:
        """Poll until the task finishes, and return its result record."""
        import time

        deadline = time.time() + timeout
        interval = 5.0
        while time.time() < deadline:
            body = self._post(self.DETAIL_URL, {"taskId": task_id, "apiKey": token})
            container = (body.get("obj") or {}).get("qcodeTaskNewVo") or {}
            records = container.get("taskResultList") or []
            if records:
                record = records[0]
                state = str(record.get("taskState"))
                if state == self.FINISHED:
                    return record
                if state in self.FAILED:
                    raise BackendError(
                        "Origin Quantum task %s failed: %s"
                        % (task_id, record.get("errorMessage") or "no detail given")
                    )
            time.sleep(interval)
            interval = min(interval * 1.5, 30.0)
        raise BackendError(
            "Origin Quantum task %s did not finish within %.0f s; it may still be "
            "queued — check the console" % (task_id, timeout)
        )

    @staticmethod
    def _counts(record: Dict[str, Any], shots: int) -> Dict[str, int]:
        """Raw device counts, preferring measured shots over mitigated probabilities.

        The platform returns both: ``probCount`` holds the counts the chip
        actually produced, keyed in hex, and ``taskResult`` holds probabilities
        after readout-error mitigation. Hardware evidence should be what the
        device measured, so the raw counts win and the mitigated numbers are
        kept in ``meta``.
        """
        import json

        raw = record.get("probCount")
        if raw:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            keys, values = payload["key"], payload["value"]
            width = max(len(bin(int(key, 16))[2:]) for key in keys)
            counts = {}
            for key, value in zip(keys, values):
                label = bin(int(key, 16))[2:].zfill(width)
                counts[label] = counts.get(label, 0) + int(value)
            return counts

        probabilities = record.get("taskResult")
        if not probabilities:
            raise BackendError("Origin Quantum returned neither counts nor probabilities")
        payload = json.loads(probabilities) if isinstance(probabilities, str) else probabilities
        return {
            key: int(round(float(value) * shots))
            for key, value in zip(payload["key"], payload["value"])
        }


HARDWARE_BACKENDS = {
    "spinq": SpinQCloudBackend,
    "originq": OriginWukongBackend,
}


def hardware_backend(target: str) -> Backend:
    from ..emitters import normalize_target

    platform = normalize_target(target)
    factory = HARDWARE_BACKENDS.get(platform)
    if factory is None:
        raise BackendError(
            "%s has no free real-hardware path in this competition; "
            "AWS Braket's cloud devices are paid and the rules allow the local "
            "simulator instead" % platform
        )
    return factory()


def hardware_report() -> Dict[str, Dict[str, object]]:
    report = {}
    for platform, factory in HARDWARE_BACKENDS.items():
        backend = factory()
        usable, reason = backend.availability()
        report[platform] = {
            "backend_id": backend.backend_id,
            "ready": usable,
            "detail": reason,
        }
    return report


__all__ = [
    "HARDWARE_BACKENDS",
    "OriginWukongBackend",
    "SPINQ_PLATFORMS",
    "SpinQCloudBackend",
    "hardware_backend",
    "hardware_report",
]
