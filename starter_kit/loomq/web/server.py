"""A standard-library HTTP server for the LoomQ web interface.

Every endpoint is a thin wrapper over the same functions the CLI and the
evaluator use, so the browser cannot see behaviour the tests do not cover.

It binds to localhost by default and refuses to serve anything outside its own
package directory.
"""

import json
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict

from ..capabilities import backends
from ..diagram import svg_diagram, text_diagram
from ..emitters import TARGETS
from ..errors import LoomQError
from ..execution import describe_environment, run_circuit, transpile_qasm
from ..qasm import parse_qasm
from ..result import counts_to_distribution
from ..sim import ideal_distribution, measurement_width
from ..version import __version__

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAX_BODY = 1 << 20


def _load_page() -> bytes:
    with open(os.path.join(_HERE, "index.html"), "rb") as handle:
        return handle.read()


# ------------------------------------------------------------------- handlers


def _circuit_payload(qasm: str) -> Dict[str, Any]:
    circuit = parse_qasm(qasm)
    distribution = ideal_distribution(circuit, measurement_width(circuit))
    return {
        "qasm": qasm,
        "summary": circuit.summary(),
        "distribution": distribution,
        "svg": svg_diagram(circuit),
        "text_diagram": text_diagram(circuit),
    }


def api_examples(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prompts": [
            "做一个 3 比特的 GHZ 纠缠态，并全部测量",
            "我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]",
            "我需要运行一个 15 比特电路，且零排队等待，选哪个平台？",
            "给我一个 3 比特的 W 态，解释一下它和 GHZ 有什么不同",
            "准备 |101> 这个状态并测量",
        ],
        "backends": backends(),
        "targets": list(TARGETS),
        "version": __version__,
    }


def api_ask(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..agent import respond

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise LoomQError("请先描述你想做什么")
    result = respond(prompt)
    response = {"text": result.text, "trace": result.trace}
    qasm = result.trace.get("qasm")
    if qasm:
        response["circuit"] = _circuit_payload(qasm)
    return response


def api_transpile(payload: Dict[str, Any]) -> Dict[str, Any]:
    qasm = str(payload.get("qasm") or "")
    return {
        "circuit": _circuit_payload(qasm),
        "targets": {target: transpile_qasm(qasm, target) for target in TARGETS},
    }


def api_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    qasm = str(payload.get("qasm") or "")
    target = str(payload.get("target") or "braket")
    shots = int(payload.get("shots") or 1024)
    shots = max(1, min(shots, 100000))
    result = run_circuit(qasm, target, shots)
    return {"result": result, "distribution": counts_to_distribution(result["counts"])}


def api_hybrid(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..hybrid import compile_hybrid, verify

    source = str(payload.get("source") or "")
    operations, assembly = compile_hybrid(source)
    return {
        "operations": operations,
        "assembly": assembly,
        "verification": verify(source),
    }


def api_health(_: Dict[str, Any]) -> Dict[str, Any]:
    from ..agent.llm import is_configured

    report = describe_environment()
    report["agent_configured"] = is_configured()
    report["version"] = __version__
    return report


_ROUTES = {
    "/api/examples": api_examples,
    "/api/ask": api_ask,
    "/api/transpile": api_transpile,
    "/api/run": api_run,
    "/api/hybrid": api_hybrid,
    "/api/health": api_health,
}  # type: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]


class LoomQHandler(BaseHTTPRequestHandler):
    server_version = "LoomQ/" + __version__

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        if os.environ.get("LOOMQ_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    # -------------------------------------------------------------- plumbing

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # --------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, _load_page(), "text/html; charset=utf-8")
            return
        if path in _ROUTES:
            self._dispatch(path, {})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        handler = _ROUTES.get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self._send_json(413, {"ok": False, "error": "request too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "request body was not JSON"})
            return
        self._dispatch(path, payload if isinstance(payload, dict) else {})

    def _dispatch(self, path: str, payload: Dict[str, Any]) -> None:
        try:
            result = _ROUTES[path](payload)
        except LoomQError as exc:
            self._send_json(200, {"ok": False, "error": str(exc), "kind": type(exc).__name__})
            return
        except Exception as exc:  # noqa: BLE001 - never take the UI down
            if os.environ.get("LOOMQ_WEB_VERBOSE"):
                traceback.print_exc()
            self._send_json(200, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        result["ok"] = True
        self._send_json(200, result)


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer((host, port), LoomQHandler)
    url = "http://%s:%d/" % (host, port)
    print("LoomQ web interface on %s" % url)
    print("按 Ctrl-C 停止。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


__all__ = ["LoomQHandler", "serve"]
