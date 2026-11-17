"""Loopback-only UI for starting and observing Azure OpenAI benchmark subprocesses."""

import argparse
import copy
import hmac
import json
import math
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from azure.core.exceptions import AzureError
from benchmark import summarize, write_report
from dashboard import load_runs, render_dashboard, report_details
import tool_benchmark
import cloud_runner

ROOT = Path(__file__).resolve().parent
LIMITS = {
    "input_tokens": (256, 131072, 512),
    "output_tokens": (16, 8192, 128),
    "repetitions": (1, 100, 5),
    "warmups": (0, 3, 1),
    "interval": (0, 30, 3),
    "timeout": (10, 300, 180),
}


def validate_request(data, config):
    allowed = {*LIMITS, "models", "tiers", "confirm_billing"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Parametros desconhecidos ou corpo invalido.")
    if data.get("confirm_billing") is not True:
        raise ValueError("Confirme a cobranca antes de iniciar.")
    clean = {}
    for key, (low, high, default) in LIMITS.items():
        value = data.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} deve estar entre {low} e {high}.")
        if key != "interval" and int(value) != value:
            raise ValueError(f"{key} deve ser inteiro.")
        clean[key] = value if key == "interval" else int(value)
    names = {model["name"] for model in config["models"]}
    for key, choices, default in (
        ("models", names, list(names)), ("tiers", {"default", "priority"}, ["default", "priority"]),
    ):
        value = data.get(key, default)
        if (
            not isinstance(value, list) or not value
            or any(not isinstance(item, str) or item not in choices for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError(f"Selecao invalida: {key}.")
        clean[key] = value
    return clean


def read_journal(folder):
    path = folder / "requests.jsonl"
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8")
    # A writer may be in the middle of its last line. Complete malformed lines still fail.
    lines = data.splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n") and line.strip()]


class BenchApp:
    def __init__(self, root=ROOT, results=None):
        self.root = root
        self.results = results or root / "results"
        self.results.mkdir(parents=True, exist_ok=True)
        self.tool_results = results / "tools" if results is not None else root / "tool-results"
        self.tool_results.mkdir(parents=True, exist_ok=True)
        self.config = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
        self.cloud = cloud_runner.load_config(root)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.job = None
        self.worker = None

    def catalog(self):
        cloud_ready = self.cloud is not None and self.cloud.get("ready", True)
        return {
            "csrf_token": self.token, "models": self.config["models"],
            "limits": {key: {"min": values[0], "max": values[1], "default": values[2]} for key, values in LIMITS.items()},
            "account": self.config["account"], "project": self.config["project"],
            "profile": "latency-v1",
            "tool_benchmark": tool_benchmark.catalog(self.config, self.root),
            "execution_targets": [
                {"id": "local", "label": "Local", "available": True},
                {"id": "containerapps", "label": "Azure Container Apps - Brazil South" + (
                    " (indisponivel: conectividade Blob)" if self.cloud and not cloud_ready else ""),
                 "region": self.cloud["region"] if self.cloud else "brazilsouth",
                 "available": cloud_ready,
                 "unavailable_reason": self.cloud.get("unavailable_reason") if self.cloud else "Nao provisionado"},
            ],
            "default_execution": "containerapps" if cloud_ready else "local",
        }

    def start(self, data, kind="text"):
        if not isinstance(data, dict):
            raise ValueError("Corpo invalido.")
        data = dict(data)
        target = data.pop("execution_target", "local")
        if target not in {"local", "containerapps"}:
            raise ValueError("Executor invalido.")
        if target == "containerapps" and not self.cloud:
            raise ValueError("Container Apps ainda nao foi provisionado neste servidor.")
        if target == "containerapps" and not self.cloud.get("ready", True):
            raise ValueError(self.cloud.get("unavailable_reason", "Executor Azure indisponivel."))
        settings = (tool_benchmark.validate_request(data, self.config, self.root)
                    if kind == "tools" else validate_request(data, self.config))
        with self.lock:
            if self.job and self.job["state"] in {"starting", "running", "stopping"}:
                raise RuntimeError("Ja existe um benchmark em execucao.")
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-live-" + uuid.uuid4().hex[:8]
            folder = (self.tool_results if kind == "tools" else self.results) / identifier
            controls = self.results / ".controls" / identifier
            controls.mkdir(parents=True)
            config = copy.deepcopy(self.config)
            config["models"] = [model for model in config["models"] if model["name"] in settings["models"]]
            config_path = controls / "config.json"
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            settings_path = controls / "settings.json"
            settings_path.write_text(json.dumps({**settings, "confirm_billing": True}), encoding="utf-8")
            self.job = {
                "id": identifier, "kind": kind, "state": "starting", "settings": settings,
                "execution_target": target,
                "folder": folder, "controls": controls, "config_path": config_path,
                "settings_path": settings_path,
                "cancel_path": controls / "cancel.request",
                "started": datetime.now(timezone.utc).isoformat(),
                "process": None, "exit_code": None, "error": None,
            }
            job = self.job
            self.worker = threading.Thread(target=self._run, args=(job,))
            self.worker.start()
            return {"id": identifier, "state": "starting"}

    def _run(self, job):
        settings = job["settings"]
        command = [
            sys.executable, "-u", str(self.root / "benchmark.py"),
            "--config", str(job["config_path"]), "--result-dir", str(job["folder"]),
            "--run-id", job["id"],
            "--cancel-file", str(job["cancel_path"]), "--execute",
            "--models", *settings["models"], "--tiers", *settings["tiers"],
        ]
        for field in LIMITS:
            if job["kind"] == "text":
                command.extend(["--" + field.replace("_", "-"), str(settings[field])])
        if job["kind"] == "tools":
            command = [
                sys.executable, "-u", str(self.root / "tool_benchmark.py"),
                "--config", str(job["config_path"]), "--settings", str(job["settings_path"]),
                "--result-dir", str(job["folder"]), "--run-id", job["id"],
                "--cancel-file", str(job["cancel_path"]), "--execute",
            ]
        try:
            if job["execution_target"] == "containerapps":
                code = cloud_runner.execute(self.cloud, job, self.lock)
            else:
                with (job["controls"] / "runner.log").open("w", encoding="utf-8") as log:
                    with self.lock:
                        process = subprocess.Popen(
                            command, cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
                            shell=False, stdin=subprocess.DEVNULL,
                            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                               else {"start_new_session": True}),
                        )
                        job["process"] = process
                        if job["state"] != "stopping":
                            job["state"] = "running"
                    code = process.wait()
            # Also recover persisted partial rows after an unexpected worker failure.
            if job["folder"].exists():
                writer = tool_benchmark.write_report if job["kind"] == "tools" else write_report
                writer(job["folder"], read_journal(job["folder"]))
            with self.lock:
                job["exit_code"] = code
                if code == 3:
                    job["state"] = "cancelled"
                elif code == 0:
                    job["state"] = "completed"
                elif code == 2:
                    job["state"] = "completed_with_exclusions"
                else:
                    job["state"] = "failed"
                    job["error"] = "Falha no benchmark; veja o log e os resultados parciais."
        except (OSError, ValueError, RuntimeError, httpx.HTTPError, AzureError) as error:
            with self.lock:
                job["state"] = "failed"
                job["error"] = str(error)
        finally:
            with self.lock:
                job["ended"] = datetime.now(timezone.utc).isoformat()
                record = {key: value for key, value in job.items() if key not in {
                    "process", "folder", "controls", "config_path", "settings_path", "cancel_path",
                }}
                (job["controls"] / "job.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    def cancel(self):
        with self.lock:
            if not self.job or self.job["state"] not in {"starting", "running", "stopping"}:
                raise RuntimeError("Nao existe benchmark ativo para cancelar.")
            self.job["cancel_path"].touch()
            self.job["state"] = "stopping"
            return {"state": "stopping"}

    def state(self, kind="text"):
        with self.lock:
            job = self.job
            if not job:
                return {"job": None, "busy": False}
            busy = job["state"] in {"starting", "running", "stopping"}
            if job["kind"] != kind:
                return {"job": None, "busy": busy}
            if kind == "tools":
                return self.tool_state(job, busy)
            result = {
                key: job[key] for key in ("id", "state", "settings", "started", "exit_code", "error", "execution_target")
            }
            rows = read_journal(job["folder"])
            log_path = job["controls"] / "runner.log"
            result["log"] = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:] if log_path.exists() else []
            phases = {}
            for row in rows:
                counts = phases.setdefault(row["phase"], {"requests": 0, "errors": 0})
                counts["requests"] += 1
                counts["errors"] += row["status"] != "ok"
            result["phases"] = phases
            result["last_request"] = None
            if rows:
                last = rows[-1]
                result["last_request"] = {
                    key: last.get(key) for key in (
                        "phase", "model", "requested_tier", "actual_tier", "status",
                        "total_ms", "ttft_ms", "prompt_tokens", "completion_tokens", "error",
                        "request_parameters",
                    )
                }
            settings = job["settings"]
            expected = len(settings["models"]) * len(settings["tiers"]) * settings["repetitions"]
            result["expected"] = expected
            result["measured"] = phases.get("measure", {}).get("requests", 0)
            manifest_path = job["folder"] / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
            result["run"] = {
                "id": manifest.get("run_id", job["id"]), "started": job["started"],
                "completed": job["state"] in {"completed", "completed_with_exclusions"},
                "cancelled": job["state"] == "cancelled",
                "expected": expected, "account": self.config["account"],
                "project": self.config["project"], "location": self.config["location"],
                "concurrency": 1, "interval": settings["interval"],
                "models": [model for model in self.config["models"] if model["name"] in settings["models"]],
                "phases": phases, "summary": summarize(rows),
                "input_target": settings["input_tokens"], "output_target": settings["output_tokens"],
                **report_details(manifest, rows),
            }
            return {"job": result, "busy": busy}

    def tool_state(self, job, busy):
        info = tool_benchmark.catalog(self.config, self.root)
        settings = job["settings"]
        models = [model for model in info["models"] if model["name"] in settings["models"]]
        expected = tool_benchmark.expected_requests(models, settings)
        run = tool_benchmark.load_run(job["folder"])
        if run is None:
            run = {
                "id": job["id"], "kind": "tools", "started": job["started"], "ended": None,
                "completed": False, "cancelled": False, "expected": expected, "measured": 0,
                "account": self.config["account"], "project": self.config["project"],
                "location": self.config["location"], "profile": info["profile"],
                "settings": {**settings, "seed": 42}, "models": models, "tools": info["tools"],
                "cases": info["cases"][:settings["cases"]], "summary": [], "rows": [], "phases": {},
            }
        result = {key: job[key] for key in ("id", "kind", "state", "settings", "started", "exit_code", "error")}
        result["execution_target"] = job["execution_target"]
        path = job["controls"] / "runner.log"
        result.update({
            "run": run, "expected": expected, "measured": run["measured"], "phases": run["phases"],
            "log": path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:] if path.exists() else [],
        })
        return {"job": result, "busy": busy}

    def history(self, kind="text"):
        with self.lock:
            excluded_folder = self.job["folder"] if self.job else None
            if kind == "tools":
                active = self.job and self.job["state"] in {"starting", "running", "stopping"}
                return tool_benchmark.load_runs(self.tool_results, exclude=excluded_folder if active else None)
            active = self.job and self.job["state"] in {"starting", "running", "stopping"}
            return load_runs(self.results, exclude=excluded_folder if active else None, allow_empty=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "LatencyBench/1"

    def log_message(self, message, *args):
        print(f"{self.address_string()} {message % args}", flush=True)

    def respond(self, code, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload, allow_nan=False).encode("utf-8") if content_type.startswith("application/json") else payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, write=False):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            self.respond(403, {"error": "Host invalido; use o endereco loopback informado."})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != f"http://{host}") or (
            write and not hmac.compare_digest(self.headers.get("X-Benchmark-Token", ""), self.server.app.token)
        ):
            self.respond(403, {"error": "Origem ou token de controle invalido."})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        try:
            app = self.server.app
            if self.path == "/":
                html = render_dashboard(
                    app.history(), (app.root / "dashboard.template.html").read_text(encoding="utf-8"),
                    datetime.now(timezone.utc).isoformat(), app.config["models"],
                    tool_runs=app.history("tools"), tool_catalog=app.catalog()["tool_benchmark"],
                )
                self.respond(200, html, "text/html; charset=utf-8")
            elif self.path == "/api/bootstrap":
                self.respond(200, app.catalog())
            elif self.path == "/api/state":
                self.respond(200, app.state())
            elif self.path == "/api/runs":
                self.respond(200, {"runs": app.history()})
            elif self.path == "/api/tools/state":
                self.respond(200, app.state("tools"))
            elif self.path == "/api/tools/runs":
                self.respond(200, {"runs": app.history("tools")})
            else:
                self.respond(404, {"error": "Rota inexistente."})
        except (OSError, ValueError, RuntimeError) as error:
            self.log_error("%s", error)
            self.respond(500, {"error": f"Erro lendo resultados: {error}"})

    def do_POST(self):
        if not self.allowed(write=True):
            return
        try:
            if self.headers.get("Content-Type") != "application/json":
                self.respond(415, {"error": "Use application/json."})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                self.respond(413, {"error": "Corpo ausente ou excede 16 KiB."})
                return
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/start":
                self.respond(202, self.server.app.start(data))
            elif self.path == "/api/tools/start":
                self.respond(202, self.server.app.start(data, "tools"))
            elif self.path == "/api/cancel":
                self.respond(202, self.server.app.cancel())
            else:
                self.respond(404, {"error": "Rota inexistente."})
        except (ValueError, TypeError) as error:
            self.respond(400, {"error": str(error)})
        except RuntimeError as error:
            self.respond(409, {"error": str(error)})
        except OSError as error:
            self.log_error("%s", error)
            self.respond(500, {"error": f"Falha ao iniciar benchmark: {error}"})


def make_server(app, port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = BenchApp()
    server = make_server(app, args.port)
    print(f"Benchmark UI: http://127.0.0.1:{server.server_port}", flush=True)
    print("Entra credentials stay on this machine. Starting a run requires billing confirmation.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with app.lock:
            if app.job and app.job["state"] in {"starting", "running", "stopping"}:
                app.cancel()
        if app.worker and app.worker.is_alive():
            print("Waiting for the in-flight request and partial result persistence...", flush=True)
            app.worker.join()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
