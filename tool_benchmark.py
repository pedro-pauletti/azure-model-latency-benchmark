"""Controlled latency benchmark for native LLM tool calls versus Decision choice routing."""

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import tiktoken
from azure.identity import AzureCliCredential
from runtime_context import credential as runtime_credential, execution_context, historical_context

from benchmark import percentile, write_csv
from tool_agent import CASES, INSTRUCTIONS, TOOLS, ToolSelectionAgent

ROOT = Path(__file__).resolve().parent
METRICS = ("selection_ms", "headers_ms", "http_total_ms", "tool_ms", "agent_total_ms")
LIMITS = {
    "cases": (3, 12, 6), "repetitions": (1, 50, 3), "warmups": (0, 3, 1),
    "padding_tokens": (0, 16384, 0), "interval": (0, 30, 1), "timeout": (10, 300, 60),
}


def catalog(config, root=ROOT):
    extra = json.loads((root / "tool-benchmark.json").read_text(encoding="utf-8"))
    decision = extra["decision_model"]
    return {
        "models": [decision, *[{**model, "kind": "llm"} for model in config["models"]]],
        "limits": {key: {"min": low, "max": high, "default": default}
                   for key, (low, high, default) in LIMITS.items()},
        "tools": TOOLS, "cases": CASES, "profile": extra["profile"], "decision_model": decision,
    }


def validate_request(data, config, root=ROOT):
    allowed = {*LIMITS, "models", "tiers", "confirm_billing"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Parametros desconhecidos no benchmark de tools.")
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
    if clean["cases"] % 3:
        raise ValueError("cases deve ser 3, 6, 9 ou 12 para manter as classes balanceadas.")
    names = {model["name"] for model in catalog(config, root)["models"]}
    for key, choices, default in (
        ("models", names, ["Microsoft-Decision-1", "gpt-4.1", "gpt-5.4-mini"]),
        ("tiers", {"default", "priority"}, ["default", "priority"]),
    ):
        value = data.get(key, default)
        if (not isinstance(value, list) or not value
                or any(not isinstance(item, str) or item not in choices for item in value)
                or len(value) != len(set(value))):
            raise ValueError(f"Selecao invalida: {key}.")
        clean[key] = value
    return clean


def profiles(models, tiers):
    return [(model, tier) for model in models
            for tier in (["decision"] if model["kind"] == "decision" else tiers)]


def expected_requests(models, settings):
    return len(profiles(models, settings["tiers"])) * settings["cases"] * settings["repetitions"]


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["phase"] == "measure":
            groups[(row["model"], row["version"], row["kind"], row["requested_tier"])].append(row)
    output = []
    for key, group in sorted(groups.items()):
        comparable = [row for row in group if row["comparable"]]
        item = dict(zip(("model", "version", "kind", "requested_tier"), key))
        item.update({
            "attempts": len(group), "valid_calls": sum(row["status"] == "ok" for row in group),
            "correct": sum(row["correct"] for row in group),
            "accuracy_pct": 100 * sum(row["correct"] for row in group) / len(group),
            "errors": sum(row["status"] != "ok" for row in group),
            "comparable": len(comparable), "excluded": len(group) - len(comparable),
            "http_429": sum(row.get("http_status") == 429 for row in group),
        })
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
            values = [row[field] for row in group if row.get(field) is not None]
            item[field + "_sum"] = sum(values) if values else None
            item[field + "_reported"] = len(values)
        for metric in METRICS:
            values = [row[metric] for row in comparable if row.get(metric) is not None]
            mean = statistics.mean(values) if values else None
            stddev = statistics.stdev(values) if len(values) > 1 else None
            item[metric + "_n"] = len(values)
            for label, value in (
                ("p50", percentile(values, .5)), ("p90", percentile(values, .9)),
                ("p95", percentile(values, .95)), ("p99", percentile(values, .99)),
                ("mean", mean), ("min", min(values) if values else None),
                ("max", max(values) if values else None), ("stddev", stddev),
                ("cv_pct", stddev / mean * 100 if stddev is not None and mean else None),
            ):
                item[metric + "_" + label] = value
        output.append(item)
    return output


def read_journal(folder):
    path = folder / "requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line.endswith("\n") and line.strip()]


def write_report(folder, rows):
    summary = summarize(rows)
    write_csv(folder / "requests.csv", rows)
    write_csv(folder / "summary.csv", summary)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def load_run(folder, manifest=None, rows=None):
    manifest_path = folder / "run.json"
    if manifest is None:
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "tools":
        raise ValueError("Not a tool-routing benchmark manifest.")
    rows = read_journal(folder) if rows is None else rows
    measured = [row for row in rows if row["phase"] == "measure"]
    from dashboard import report_details
    details = report_details({
        **manifest, "arguments": manifest.get("settings", {}),
    }, rows)
    return {
        **manifest, **details, "settings": manifest.get("settings", {}),
        "execution": historical_context(manifest),
        "concurrency": manifest.get("concurrency", 1), "interval": manifest.get("settings", {}).get("interval"),
        "id": manifest["run_id"], "started": manifest.get("started_utc"),
        "ended": manifest.get("ended_utc"), "summary": summarize(rows), "rows": measured,
        "measured": len(measured),
        "phases": {phase: {"requests": sum(row["phase"] == phase for row in rows),
                          "errors": sum(row["phase"] == phase and row["status"] != "ok" for row in rows)}
                   for phase in ("warmup", "measure")},
    }


def load_runs(results, exclude=None):
    if not results.exists():
        return []
    return [run for folder in sorted(results.iterdir())
            if folder != exclude and folder.is_dir() and (folder / "run.json").is_file()
            if (run := load_run(folder)) is not None]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "benchmark.json")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--result-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--cancel-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    data = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {"confirm_billing": True}
    settings = validate_request(data, config)
    info = catalog(config)
    models = [model for model in info["models"] if model["name"] in settings["models"]]
    selected_profiles = profiles(models, settings["tiers"])
    expected = expected_requests(models, settings)
    print(json.dumps({"settings": settings, "profiles": [(m["name"], t) for m, t in selected_profiles],
                      "measured_requests": expected,
                      "warmup_requests": len(selected_profiles) * settings["warmups"]}, indent=2), flush=True)
    if not args.execute:
        print("Plan only; --execute sends billable requests.")
        return 0
    identifier = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-tools-" + uuid.uuid4().hex[:8]
    folder = args.result_dir or ROOT / "tool-results" / identifier
    folder.mkdir(parents=True, exist_ok=False)
    cases = CASES[:settings["cases"]]
    manifest = {
        "run_id": identifier, "kind": "tools", "profile": info["profile"],
        "started_utc": datetime.now(timezone.utc).isoformat(), "ended_utc": None,
        "completed": False, "cancelled": False, "expected": expected,
        "account": config["account"], "project": config["project"], "location": config["location"],
        "models": models, "tools": TOOLS, "cases": cases, "settings": {**settings, "seed": 42},
        "instructions": INSTRUCTIONS, "concurrency": 1,
        "execution": execution_context(),
        "dataset_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("tool_agent.py", "tool_benchmark.py")},
        "methodology": "Local single-step agent; non-stream HTTP; valid complete selection, then local dispatch; no final LLM answer.",
        "connection_policy": "Shared HTTP/1.1 pool; sequential; no retries; auth and serialization outside timings.",
    }
    rows = []

    def save():
        path = folder / "run.tmp"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        path.replace(folder / "run.json")

    def cancelled():
        return args.cancel_file is not None and args.cancel_file.exists()

    def wait():
        deadline = time.monotonic() + settings["interval"]
        while time.monotonic() < deadline and not cancelled():
            time.sleep(min(.1, max(0, deadline - time.monotonic())))

    save()
    try:
        encoding = tiktoken.get_encoding("o200k_base")
        padding = " x" * settings["padding_tokens"]
        if len(encoding.encode(padding)) != settings["padding_tokens"]:
            raise ValueError("Unexpected local padding tokenization.")
        schedule = [("warmup", repetition, cases[repetition % len(cases)], model, tier)
                    for repetition in range(settings["warmups"]) for model, tier in selected_profiles]
        rng = random.Random(42)
        for repetition in range(settings["repetitions"]):
            round_calls = [("measure", repetition, case, model, tier)
                           for case in cases for model, tier in selected_profiles]
            rng.shuffle(round_calls)
            schedule.extend(round_calls)
        with runtime_credential(AzureCliCredential) as credential, httpx.Client(
            http2=False, trust_env=False, timeout=settings["timeout"],
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2, keepalive_expiry=120),
        ) as client, (folder / "requests.jsonl").open("a", encoding="utf-8") as journal:
            agent = ToolSelectionAgent(client, credential, config)
            for phase, repetition, case, model, tier in schedule:
                if cancelled():
                    break
                nonce = uuid.uuid4().hex
                text = f"Benchmark nonce (ignore): {nonce}\nSynthetic padding (ignore):{padding}\nCustomer request:\n{case['text']}"
                row = {
                    "phase": phase, "repetition": repetition, "model": model["name"], "version": model["version"],
                    "kind": model["kind"], "requested_tier": tier, "case_id": case["id"],
                    "expected_tool": case["expected_tool"], "timestamp": datetime.now(timezone.utc).isoformat(),
                    "nonce": nonce, "padding_tokens_local": settings["padding_tokens"],
                    **agent.invoke(model, text, tier, case["expected_tool"], settings["timeout"]),
                }
                rows.append(row)
                journal.write(json.dumps(row, ensure_ascii=True) + "\n")
                journal.flush()
                print(f"{phase} {model['name']} {tier} {case['id']}: "
                      f"{row['predicted_tool']} correct={row['correct']} comparable={row['comparable']} "
                      f"selection_ms={row['selection_ms']} error={row['error']}", flush=True)
                wait()
        manifest["cancelled"] = cancelled()
        manifest["completed"] = not manifest["cancelled"]
    finally:
        manifest["ended_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        write_report(folder, rows)
    print("Results:", folder, flush=True)
    return 3 if manifest["cancelled"] else (0 if all(row["comparable"] for row in rows if row["phase"] == "measure") else 2)


if __name__ == "__main__":
    sys.exit(main())
