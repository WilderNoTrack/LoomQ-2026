"""Origin Quantum (本源) adapter.

Two import paths are supported because the ecosystem has both: ``pyqpanda``
(2.x) and ``pyqpanda3``.  Either way the circuit reaches the CPU QVM as a
``QProg``; the OriginIR text from ``transpile(qasm, "originq")`` is carried
along in the metadata so a result can always be traced back to the exact
instruction stream that produced it.
"""

from typing import Any, Tuple

from ..errors import BackendError
from ..emitters.spinq import emit_spinq
from ..ir import Circuit
from ..result import new_job_id
from .base import Backend, ExecutionOutcome, import_optional


class OriginQBackend(Backend):
    platform = "originq"
    backend_id = "originq_local_simulator"
    executor = "pyqpanda"

    def _sdk(self) -> Any:
        return import_optional("pyqpanda") or import_optional("pyqpanda3")

    def availability(self) -> Tuple[bool, str]:
        module = self._sdk()
        if module is None:
            return False, "pyqpanda is not installed (pip install pyqpanda)"
        if not hasattr(module, "CPUQVM"):
            return False, "pyqpanda is installed but exposes no CPUQVM"
        return True, "pyqpanda %s" % getattr(module, "__version__", "unknown")

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        module = self._sdk()
        if module is None:  # pragma: no cover - guarded by availability()
            raise BackendError("pyqpanda is not installed")

        machine = module.CPUQVM()
        machine.init_qvm()
        try:
            # pyqpanda's importer speaks OpenQASM 2.0; OriginIR is what the
            # contract asks `transpile()` to return, and travels in meta.
            qasm = emit_spinq(circuit)
            if hasattr(module, "convert_qasm_string_to_qprog"):
                program, _, cregs = module.convert_qasm_string_to_qprog(qasm, machine)
            elif hasattr(module, "convert_qasm_to_qprog"):
                program = module.convert_qasm_to_qprog(qasm, machine)
                cregs = machine.get_allocate_cbits()
            else:
                raise BackendError("this pyqpanda build cannot import OpenQASM")
            raw = machine.run_with_configuration(program, cregs, shots)
            raw = dict(raw)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("pyqpanda failed to execute the circuit: %s" % exc)
        finally:
            try:
                machine.finalize()
            except Exception:  # pragma: no cover - best effort cleanup
                pass

        return ExecutionOutcome(
            raw,
            job_id=new_job_id("originq-local"),
            meta={
                "executor": self.executor,
                "key_convention": "clbit_msb_first",
                "origin_ir_lines": len(native_ir.strip().splitlines()),
            },
        )


__all__ = ["OriginQBackend"]
