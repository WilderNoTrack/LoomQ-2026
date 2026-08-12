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


def _require(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackendError("%s is not set. %s" % (name, hint))
    return value


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

    def choose_platform(self, circuit: Circuit) -> str:
        """Smallest platform that fits, unless one is pinned."""
        pinned = os.environ.get("LOOMQ_SPINQ_PLATFORM")
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
        for name, capacity in sorted(SPINQ_PLATFORMS.items(), key=lambda item: item[1]):
            if capacity >= circuit.num_qubits:
                return name
        raise BackendError(
            "SpinQ Cloud tops out at %d qubits; this circuit needs %d"
            % (max(SPINQ_PLATFORMS.values()), circuit.num_qubits)
        )

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

        platform = self.choose_platform(circuit)
        task_name = os.environ.get("LOOMQ_SPINQ_TASK", "loomq")
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".qasm", delete=False, encoding="utf-8"
        )
        try:
            handle.write(native_ir)
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
            config = module.SpinQCloudConfig()
            config.configure_platform(platform)
            config.configure_shots(shots)
            config.configure_task(task_name, "loomq")
            outcome = backend.execute(program, config)
            raw = dict(outcome.counts)
        except Exception as exc:
            raise BackendError("SpinQ Cloud rejected or failed the task: %s" % exc)

        job_id = (
            getattr(outcome, "task_id", None)
            or getattr(outcome, "job_id", None)
            or getattr(outcome, "id", None)
            or new_job_id("spinq-cloud")
        )
        return ExecutionOutcome(
            raw,
            job_id=str(job_id),
            meta={
                "executor": self.executor,
                "hardware": True,
                "spinq_platform": platform,
                "key_convention": "clbit_msb_first",
            },
        )


class OriginWukongBackend(Backend):
    """本源悟空超导真机 (72 qubits)."""

    platform = "originq"
    backend_id = "originq_wukong"
    executor = "pyqpanda (QCloud, real chip)"
    is_hardware = True

    def _sdk(self) -> Any:
        return import_optional("pyqpanda") or import_optional("pyqpanda3")

    def availability(self) -> Tuple[bool, str]:
        module = self._sdk()
        if module is None:
            return False, "pyqpanda is not installed"
        if not hasattr(module, "QCloud"):
            return False, "this pyqpanda build has no QCloud"
        if not os.environ.get("LOOMQ_ORIGINQ_TOKEN"):
            return False, "LOOMQ_ORIGINQ_TOKEN is not set"
        return True, "Origin Quantum API token present"

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        module = self._sdk()
        if module is None:  # pragma: no cover - guarded by availability()
            raise BackendError("pyqpanda is not installed")

        token = _require(
            "LOOMQ_ORIGINQ_TOKEN",
            "Apply for an API token at qcloud.originqc.com.cn and export it.",
        )
        chip_name = os.environ.get("LOOMQ_ORIGINQ_CHIP", "origin_72")

        from ..emitters.spinq import emit_spinq

        machine = module.QCloud()
        try:
            machine.init_qvm(token)
            if hasattr(machine, "set_configure"):
                machine.set_configure(72, 72)

            qasm = emit_spinq(circuit)
            if hasattr(module, "convert_qasm_string_to_qprog"):
                program, _, _ = module.convert_qasm_string_to_qprog(qasm, machine)
            elif hasattr(module, "convert_qasm_to_qprog"):
                program = module.convert_qasm_to_qprog(qasm, machine)
            else:
                raise BackendError("this pyqpanda build cannot import OpenQASM")

            chip = getattr(module.real_chip_type, chip_name, None)
            if chip is None:
                raise BackendError(
                    "unknown Origin chip %r; available: %s"
                    % (
                        chip_name,
                        ", ".join(
                            name for name in dir(module.real_chip_type)
                            if not name.startswith("_")
                        ),
                    )
                )
            raw = dict(machine.real_chip_measure(program, shots, chip))
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("Origin Quantum cloud call failed: %s" % exc)
        finally:
            try:
                machine.finalize()
            except Exception:  # pragma: no cover - best effort
                pass

        return ExecutionOutcome(
            raw,
            job_id=new_job_id("originq-wukong"),
            meta={
                "executor": self.executor,
                "hardware": True,
                "origin_chip": chip_name,
                "key_convention": "clbit_msb_first",
                "note": "real_chip_measure returns probabilities on some builds; "
                        "loomq hardware converts them to integer counts",
            },
        )


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
