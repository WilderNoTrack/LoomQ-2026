"""SpinQ (量旋) adapter, driven through the ``spinqit`` SDK.

SpinQ imports OpenQASM 2.0 directly, so the adapter hands it exactly the text
``transpile(qasm, "spinq")`` produced — the artifact that is scored and the
artifact that is executed are the same string, which is the point of having a
contract-shaped IR at all.

**Bit order.** spinqit reports counts with ``c[0]`` on the *left*, the opposite
of the contract's ``c[n-1]...c[0]``.  This is invisible on Bell and GHZ states —
their outcomes are palindromes — and shows up the moment a circuit is
asymmetric: ``x q[0]; swap q[0],q[1];`` returns ``01`` where the contract wants
``10``.  It was found by running ``tools/validate_vendor_ir.py`` against
spinqit itself, and the keys are reversed here rather than left for the
execution layer's calibration to notice.  Normalising exactly this kind of
per-platform difference is what the middle layer is for.

**Which interpreter runs it.** spinqit cannot share an environment with
amazon-braket (their ``antlr4-python3-runtime`` pins conflict exactly), so the
container installs it into ``/opt/spinq``.  An ``import spinqit`` from LoomQ's
own interpreter therefore fails *in the very image built to run it*, and the
result would be a silent fallback to the reference simulator.  This adapter
looks for a sibling interpreter — ``LOOMQ_SPINQ_PYTHON``, then ``/opt/spinq``,
then a local ``.venv-spinq`` — and runs :mod:`loomq.backends.spinq_runner`
there when the import is not available in-process.
"""

import json
import os
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from ..errors import BackendError
from ..ir import Circuit
from ..result import new_job_id
from .base import Backend, ExecutionOutcome, import_optional

#: Interpreters to try when ``import spinqit`` fails in-process, in order.
_SIDECAR_CANDIDATES = (
    "/opt/spinq/bin/python",
    "/opt/spinq/Scripts/python.exe",
    ".venv-spinq/bin/python",
    ".venv-spinq/Scripts/python.exe",
)


class SpinQBackend(Backend):
    platform = "spinq"
    backend_id = "spinq_taurus_simulator"
    executor = "spinqit"

    def _sdk(self):
        return import_optional("spinqit")

    def _sidecar(self) -> Optional[str]:
        """An interpreter that can import spinqit, if this one cannot."""
        pinned = os.environ.get("LOOMQ_SPINQ_PYTHON")
        candidates = [pinned] if pinned else []
        kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for candidate in _SIDECAR_CANDIDATES:
            candidates.append(
                candidate if os.path.isabs(candidate) else os.path.join(kit, candidate)
            )
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def availability(self) -> Tuple[bool, str]:
        module = self._sdk()
        if module is not None:
            for symbol in ("get_compiler", "get_basic_simulator", "BasicSimulatorConfig"):
                if not hasattr(module, symbol):
                    return False, "spinqit is installed but lacks %s()" % symbol
            return True, "spinqit %s (in-process)" % getattr(module, "__version__", "unknown")

        sidecar = self._sidecar()
        if sidecar:
            return True, "spinqit via %s" % sidecar
        return (
            False,
            "spinqit is not importable here and no sidecar interpreter was found "
            "(set LOOMQ_SPINQ_PYTHON, or see requirements-spinq.txt)",
        )

    def execute(self, circuit: Circuit, native_ir: str, shots: int) -> ExecutionOutcome:
        module = self._sdk()
        if module is None:
            return self._execute_sidecar(circuit, native_ir, shots)

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
            {key[::-1]: value for key, value in raw.items()},
            job_id=str(job_id),
            meta={
                "executor": self.executor,
                "key_convention": "clbit_lsb_first",
                "bit_order_reversed": True,
                "qubits": getattr(program, "qnum", circuit.num_qubits),
                "interpreter": "in-process",
            },
        )

    def _execute_sidecar(
        self, circuit: Circuit, native_ir: str, shots: int
    ) -> ExecutionOutcome:
        """Run spinqit in the interpreter that actually has it installed."""
        interpreter = self._sidecar()
        if interpreter is None:  # pragma: no cover - guarded by availability()
            raise BackendError("spinqit is not importable and no sidecar was found")

        kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [kit] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [])
        )

        try:
            completed = subprocess.run(
                [interpreter, "-m", "loomq.backends.spinq_runner", str(shots)],
                input=native_ir.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                cwd=kit,
                timeout=float(os.environ.get("LOOMQ_SPINQ_TIMEOUT_SECONDS", 300)),
            )
        except subprocess.TimeoutExpired:
            raise BackendError("spinqit sidecar timed out")
        except OSError as exc:
            raise BackendError("could not start the spinqit sidecar: %s" % exc)

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip() or "no detail"
            raise BackendError("spinqit sidecar failed: %s" % detail)

        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
        except ValueError:
            raise BackendError("spinqit sidecar returned unreadable output")

        raw = payload.get("counts") or {}
        if not raw:
            raise BackendError("spinqit sidecar returned no counts")

        return ExecutionOutcome(
            {key[::-1]: int(value) for key, value in raw.items()},
            job_id=new_job_id("spinq-local"),
            meta={
                "executor": self.executor,
                "key_convention": "clbit_lsb_first",
                "bit_order_reversed": True,
                "qubits": payload.get("qubits") or circuit.num_qubits,
                "interpreter": interpreter,
                "spinqit_version": payload.get("spinqit_version"),
            },
        )


__all__ = ["SpinQBackend"]
