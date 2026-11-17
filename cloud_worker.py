"""Private Container Apps Job entrypoint; exchange control/results through Entra-protected Blob."""

import gzip
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from azure.core.exceptions import AzureError
from azure_io import AzureIO
from runtime_context import credential

ROOT = Path(__file__).resolve().parent


def command_for(kind, settings, folder, cancel):
    if kind == "tools":
        from tool_benchmark import validate_request
    elif kind == "text":
        from server import validate_request
    else:
        raise ValueError("Invalid benchmark kind.")
    config = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
    settings = validate_request(settings, config)
    if kind == "tools":
        path = folder / "settings.json"
        path.write_text(json.dumps({**settings, "confirm_billing": True}), encoding="utf-8")
        command = [sys.executable, "-u", str(ROOT / "tool_benchmark.py"), "--settings", str(path)]
    else:
        command = [sys.executable, "-u", str(ROOT / "benchmark.py"),
                   "--models", *settings["models"], "--tiers", *settings["tiers"]]
        for field in ("input_tokens", "output_tokens", "repetitions", "warmups", "interval", "timeout"):
            command.extend(["--" + field.replace("_", "-"), str(settings[field])])
    return command + ["--result-dir", str(folder / "result"), "--run-id", folder.name,
                      "--cancel-file", str(cancel), "--execute"]


def main():
    identifier = os.environ["BENCHMARK_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9-]{10,100}", identifier):
        raise ValueError("Invalid run ID.")
    container = os.environ["BENCHMARK_STORAGE_CONTAINER"]
    prefix = "runs/" + identifier + "/"
    folder = Path("/tmp/benchmark") / identifier
    folder.mkdir(parents=True, exist_ok=False)
    log_path = folder / "runner.log"
    cancel = folder / "cancel"
    lease_id, process, owns_run, lease_acquired = None, None, False, False
    with credential() as identity:
        io = AzureIO(identity)

        def publish(finished=False, exit_code=None, error=None):
            files = {}
            for name in ("run.json", "requests.jsonl"):
                path = folder / "result" / name
                if path.exists():
                    files[name] = path.read_text(encoding="utf-8")
            if log_path.exists():
                files["runner.log"] = log_path.read_text(encoding="utf-8", errors="replace")
            payload = json.dumps({"files": files, "finished": finished, "exit_code": exit_code, "error": error})
            io.blob("PUT", container, prefix + "snapshot.json.gz", gzip.compress(payload.encode(), compresslevel=1))

        try:
            # Never bill twice if an execution is accidentally replayed.
            claimed = io.put_json(container, prefix + "claimed.json", {"claimed": True},
                                  headers={"If-None-Match": "*"}, exists_ok=True)
            if claimed is None:
                raise RuntimeError("This benchmark run was already claimed; refusing duplicate inference.")
            owns_run = True
            lease_id = str(uuid.uuid4())
            io.blob("PUT", container, "runner.lock", b"", headers={"If-None-Match": "*"}, exists_ok=True)
            io.blob("PUT", container, "runner.lock", headers={
                "x-ms-lease-action": "acquire", "x-ms-lease-duration": "60",
                "x-ms-proposed-lease-id": lease_id,
            }, query="?comp=lease")
            lease_acquired = True
            data = io.get_json(container, prefix + "input.json")
            if data.get("id") != identifier:
                raise ValueError("Input run ID mismatch.")
            env = dict(os.environ)
            env["BENCHMARK_EXECUTION_CONTEXT"] = json.dumps({
                "target": "containerapps", "label": "Azure Container Apps Job",
                "region": os.environ["BENCHMARK_REGION"], "resource_id": os.environ["BENCHMARK_JOB_ID"],
                "cpu": 1, "memory": "2Gi", "parallelism": 1, "replica_retry_limit": 0,
                "image": os.environ["BENCHMARK_IMAGE"], "run_id": identifier,
                "revision_or_execution": os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME"),
                "authentication": "managed_identity",
            })
            command = command_for(data["kind"], data["settings"], folder, cancel)
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, shell=False, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT)
                renewed = time.monotonic()
                while process.poll() is None:
                    if io.blob("GET", container, prefix + "cancel.json", missing_ok=True) is not None:
                        cancel.touch(exist_ok=True)
                    if time.monotonic() - renewed > 20:
                        io.blob("PUT", container, "runner.lock", headers={
                            "x-ms-lease-action": "renew", "x-ms-lease-id": lease_id,
                        }, query="?comp=lease")
                        renewed = time.monotonic()
                    publish()
                    time.sleep(3)
                publish(finished=True, exit_code=process.returncode)
            return 0 if process.returncode in {0, 2, 3} else process.returncode
        except (OSError, ValueError, RuntimeError, httpx.HTTPError, AzureError) as error:
            if process and process.poll() is None:
                cancel.touch(exist_ok=True)
                # Fail closed if coordination/lease renewal fails: no orphaned inference loop.
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if owns_run:
                publish(finished=True, exit_code=1, error=str(error))
            raise
        finally:
            if lease_acquired:
                try:
                    io.blob("PUT", container, "runner.lock", headers={
                        "x-ms-lease-action": "release", "x-ms-lease-id": lease_id,
                    }, query="?comp=lease")
                except (RuntimeError, httpx.HTTPError, AzureError) as error:
                    print(f"Lease release failed (expires in <=60s): {error}", file=sys.stderr)
            io.close()


if __name__ == "__main__":
    sys.exit(main())
