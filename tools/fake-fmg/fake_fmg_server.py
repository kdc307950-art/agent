"""Local HTTPS Fake FortiManager used only for protocol-level demonstration.

It implements the small JSON-RPC surface exercised by ``FortiManagerCommandGateway``:
status and target validation reads, preview, install submission, and task polling.
It never connects to a Fortinet product or manages a real device.
"""

from __future__ import annotations

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

API_TOKEN = os.getenv("FAKE_FMG_TOKEN", "fake-token-123")
HOST = os.getenv("FAKE_FMG_HOST", "0.0.0.0")
PORT = int(os.getenv("FAKE_FMG_PORT", "8443"))
PREVIEW_MODE = os.getenv("FAKE_FMG_PREVIEW_MODE", "diff").strip().lower()

TASKS: dict[int, dict[str, Any]] = {}
NEXT_TASK = 1000


class FakeFmgHandler(BaseHTTPRequestHandler):
    """Single-threaded JSON-RPC fixture with deterministic task progress."""

    protocol_version = "HTTP/1.1"

    def _send(self, http_code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(http_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _result(
        request_id: str | int,
        url: str,
        *,
        code: int = 0,
        message: str = "OK",
        data: Any = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": {"code": code, "message": message},
            "url": url,
        }
        if data is not None:
            result["data"] = data
        return {"id": request_id, "result": [result]}

    @staticmethod
    def _new_task(*, kind: str, stall: bool) -> int:
        global NEXT_TASK
        NEXT_TASK += 1
        TASKS[NEXT_TASK] = {
            "percent": 30,
            "num_err": 0,
            "state": "running",
            "polls": 0,
            "kind": kind,
            "stall": stall,
        }
        return NEXT_TASK

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "fake-fmg", "tasks": len(TASKS)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/jsonrpc":
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("Authorization", "") != f"Bearer {API_TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length)) if length > 0 else None
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid json"})
            return
        if not isinstance(request, dict):
            self._send(400, {"error": "invalid request"})
            return

        params = request.get("params") or [{}]
        parameter = params[0] if isinstance(params, list) and params else {}
        if not isinstance(parameter, dict):
            parameter = {}
        method = request.get("method", "")
        url = parameter.get("url", "")
        data = parameter.get("data") or {}
        request_id = request.get("id", 1)

        if method == "get" and url == "/sys/status":
            self._send(
                200,
                self._result(
                    request_id,
                    url,
                    data={
                        "Version": "fake-fmg-v1",
                        "Hostname": "fake-fmg",
                        "Platform": "FAKE-FMG",
                        "Build": "local-demo",
                    },
                ),
            )
            return

        if method == "get" and url == "/dvmdb/device":
            self._send(
                200,
                self._result(request_id, url, data=[{"name": "FGT-FAKE-001", "oid": 1}]),
            )
            return

        if method == "get" and url.startswith("/pm/config/adom/") and url.endswith("/pkg"):
            self._send(
                200,
                self._result(request_id, url, data=[{"name": "PKG_FAKE", "oid": 1}]),
            )
            return

        is_preview = url == "/securityconsole/install/preview" or (
            url == "/securityconsole/install/package" and "preview" in data.get("flags", [])
        )
        if method == "exec" and is_preview:
            # STALL injects an install timeout. Preview must finish first so the
            # drill proves the approved-preview then execution-unknown branch.
            task_id = self._new_task(kind="preview", stall=False)
            self._send(200, self._result(request_id, url, data={"task": task_id}))
            return

        if method == "exec" and url == "/securityconsole/preview/result":
            message: Any
            if PREVIEW_MODE == "noop":
                message = ""
            else:
                message = {
                    "changes": [
                        {
                            "scope": "FGT-FAKE-001/root",
                            "operation": "install",
                            "summary": "demo VPN package deployment",
                        }
                    ]
                }
            self._send(200, self._result(request_id, url, data={"message": message}))
            return

        if method == "exec" and url.startswith("/securityconsole/install/"):
            adom = data.get("adom", "")
            if adom == "REJECT":
                self._send(
                    200,
                    self._result(
                        request_id, url, code=-11, message="No permission for the resource"
                    ),
                )
                return
            task_id = self._new_task(kind=url.rsplit("/", 1)[-1], stall=adom == "STALL")
            self._send(200, self._result(request_id, url, data={"task": task_id}))
            return

        if method == "get" and url.startswith("/task/task/"):
            try:
                task_id = int(url.rsplit("/", 1)[-1])
            except ValueError:
                self._send(200, self._result(request_id, url, code=-6, message="Invalid url"))
                return
            task = TASKS.get(task_id)
            if task is None:
                self._send(200, self._result(request_id, url, code=-6, message="Invalid url"))
                return
            task["polls"] += 1
            if task["stall"]:
                task["percent"] = 50
            elif task["polls"] > 3:
                task["percent"] = 100
                task["state"] = "done"
            else:
                task["percent"] = min(90, 30 + task["polls"] * 20)
            result: dict[str, Any] = {
                "percent": task["percent"],
                "num_err": task["num_err"],
                "state": task["state"],
                "num_warn": 0,
                "num_lines": 1,
            }
            if task["percent"] == 100:
                result["line"] = [
                    {
                        "name": "FGT-FAKE-001",
                        "oid": 1,
                        "vdom": "root",
                        "percent": 100,
                        "state": "done",
                        "err": 0,
                        "detail": f"{task['kind']} completed by Fake FMG",
                    }
                ]
            self._send(200, self._result(request_id, url, data=result))
            return

        self._send(200, self._result(request_id, url, code=-6, message="not mocked"))

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args


def main() -> None:
    root = Path(__file__).resolve().parent
    httpd = HTTPServer((HOST, PORT), FakeFmgHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "certs" / "cert.pem", root / "certs" / "key.pem")
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    print(f"Fake FMG listening on https://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
