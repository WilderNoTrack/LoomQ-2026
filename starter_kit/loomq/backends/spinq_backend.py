"""SpinQ (量旋) adapter, driven through the ``spinqit`` SDK.

SpinQ imports OpenQASM 2.0 directly, so the adapter hands it exactly the text
``transpile(qasm, "spinq")`` produced — the artifact that is scored and the
artifact that is executed are the same string, which is the point of having a
contract-shaped IR at all.
"""

import os
import tempfile
from typing import Tuple

from ..errors import BackendError
from ..ir import Circuit
from ..result import new_job_id
from .base import Backend, ExecutionOutcome, import_optional


class SpinQBackend(Backend):
    platform = "spinq"
    backend_id = "spinq_taurus_simulator"
    executor = "spinqit"

    def _sdk(self):
        return import_optional("spinqit")

    def availability(self) -> Tuple[bool, str]:
        module = self._sdk()
        if module is None:
            return False, "spinqit is not installed (pip install spinqit, CPython 3.10)"
        for symbol in ("get_compiler", "get_basic_simulator", "BasicSimulatorConfig"):
            if not hasattr(module, symbol):
                return False, "spinqit is installed but lacks %s()" % symbol
        return True, "spinqit %s" % getattr(module, "__version__", "unknown")

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        module = self._sdk()
        if module is None:  # pragma: no cover - guarded by availability()
            raise BackendError("spinqit is not installed")

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".qasm", delete=False, encoding="utf-8"
        )
        try:
            handle.write(native_ir)
            handle.close()
            compiler = module.get_compiler("qasm")
            program = compiler.compile(handle.name, 0)
        except Exception as exc:
            raise BackendError("spinqit could not compile the OpenQASM 2.0 artifact: %s" % exc)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover - best effort cleanup
                pass

        try:
            engine = module.get_basic_simulator()
            config = module.BasicSimulatorConfig()
            config.configure_shots(shots)
            outcome = engine.execute(program, config)
            raw = dict(outcome.counts)
        except Exception as exc:
            raise BackendError("spinqit failed to execute the circuit: %s" % exc)

        job_id = (
            getattr(outcome, "job_id", None)
            or getattr(outcome, "task_id", None)
            or new_job_id("spinq-local")
        )
        return ExecutionOutcome(
            raw,
            job_id=str(job_id),
            meta={
                "executor": self.executor,
                "key_convention": "clbit_msb_first",
                "qubits": getattr(program, "qnum", circuit.num_qubits),
            },
        )


__all__ = ["SpinQBackend"]
