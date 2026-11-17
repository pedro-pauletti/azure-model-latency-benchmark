"""Local control plane for the private Azure Container Apps manual job."""

import gzip
import copy
import json
import time
from pathlib import Path

from azure.identity import AzureCliCredential

from azure_io import AzureIO

VERSION = "2025-01-01"
TERMINAL = {"Succeeded", "Failed", "Stopped", "Degraded"}
ARTIFACT_FILES = {"run.json", "requests.jsonl", "runner.log"}


def load_config(root):
    path = root / "containerapps.json"
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    required = ("job_id", "storage_container", "region", "image")
    if any(not isinstance(config.get(key), str) or not config[key] for key in required):
        raise ValueError("Configuracao Container Apps invalida; execute deploy_containerapps.py.")
    return config


def apply_snapshot(snapshot, job):
    files = snapshot.get("files", {})
    if not isinstance(files, dict) or set(files) - ARTIFACT_FILES:
        raise ValueError("Invalid remote artifact paths.")
    for name, content in files.items():
        if not isinstance(content, str):
            raise ValueError("Invalid remote artifact content.")
        if name == "runner.log":
            path = job["controls"] / name
        else:
            job["folder"].mkdir(parents=True, exist_ok=True)
            path = job["folder"] / name
        temporary = path.with_suffix(path.suffix + ".download")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


def start_execution(io, config, job):
    container, job_id = config["storage_container"], config["job_id"]
    executions = io.arm("GET", job_id + "/executions", VERSION).get("value", [])
    if any(item.get("properties", {}).get("status") in {"Running", "Processing", "Pending"}
           for item in executions):
        raise RuntimeError("Ja existe uma execucao Container Apps ativa.")
    io.put_json(container, "runs/" + job["id"] + "/input.json", {
        "id": job["id"], "kind": job["kind"], "settings": {**job["settings"], "confirm_billing": True},
    })
    definition = io.arm("GET", job_id, VERSION)
    template = {key: copy.deepcopy(value) for key, value in definition["properties"]["template"].items()
                if key in {"containers", "initContainers"} and value is not None}
    for group in template.values():
        for index, item in enumerate(group):
            clean = {key: value for key, value in item.items()
                     if key in {"name", "image", "command", "args", "env", "resources", "volumeMounts"}
                     and value is not None}
            if "resources" in clean:
                clean["resources"] = {key: value for key, value in clean["resources"].items()
                                      if key in {"cpu", "memory"}}
            group[index] = clean
    template["containers"][0].setdefault("env", []).append({"name": "BENCHMARK_RUN_ID", "value": job["id"]})
    started = io.arm("POST", job_id + "/start", VERSION, json=template)
    if not started.get("name"):
        raise RuntimeError("Container Apps nao retornou a identidade da execucao.")
    return started["name"]


def execute(config, job, lock):
    container, job_id = config["storage_container"], config["job_id"]
    prefix = "runs/" + job["id"] + "/"
    with AzureCliCredential(process_timeout=60) as credential:
        io = AzureIO(credential)
        try:
            # Resume the same remote execution after a control-plane failure, never rebill it.
            execution_name = job.get("cloud_execution") or start_execution(io, config, job)
            execution_id = job_id + "/executions/" + execution_name
            with lock:
                job["cloud_execution"] = execution_name
            cancel_sent = False
            last_snapshot = None
            startup_deadline = time.monotonic() + 900
            while True:
                if job["cancel_path"].exists() and not cancel_sent:
                    io.put_json(container, prefix + "cancel.json", {"cancel": True})
                    cancel_sent = True
                response = io.blob("GET", container, prefix + "snapshot.json.gz", missing_ok=True)
                if response is not None and response.headers.get("etag") != last_snapshot:
                    snapshot = json.loads(gzip.decompress(response.content))
                    last_snapshot = response.headers.get("etag")
                    with lock:
                        apply_snapshot(snapshot, job)
                        if job["state"] != "stopping":
                            job["state"] = "running"
                    if snapshot.get("finished"):
                        if snapshot.get("error"):
                            raise RuntimeError(snapshot["error"])
                        return snapshot["exit_code"]
                status = io.arm("GET", execution_id, VERSION).get("properties", {}).get("status")
                if status in TERMINAL:
                    # The job uploads its final snapshot before exiting.
                    response = io.blob("GET", container, prefix + "snapshot.json.gz", missing_ok=True)
                    if response is not None:
                        snapshot = json.loads(gzip.decompress(response.content))
                        with lock:
                            apply_snapshot(snapshot, job)
                        if snapshot.get("finished") and not snapshot.get("error"):
                            return snapshot["exit_code"]
                    raise RuntimeError(f"Container Apps terminou sem resultado final: {status}. Execucao {execution_name}.")
                if last_snapshot is None and time.monotonic() > startup_deadline:
                    raise RuntimeError(f"Container Apps nao iniciou o worker em 15 minutos: {execution_name}. Verifique a execucao Azure.")
                time.sleep(3)
        finally:
            io.close()
