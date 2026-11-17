"""Controlled, sequential Azure OpenAI streaming latency benchmark."""

import argparse
import csv
import hashlib
import importlib.metadata
import itertools
import json
import math
import platform
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
from runtime_context import credential as runtime_credential, execution_context

ROOT = Path(__file__).resolve().parent
INSTRUCTION = (
    "\nTask: List integers from 1 to {count}, one per line. Start with 1 and continue "
    "in order. This response is intentionally truncated by the API; no need to finish "
    "the list. Output numbers only, without ellipses, explanations, or code."
)
SERVER_METRICS = (
    "pre_inference_ms", "engine_ttft_ms", "engine_ttlt_ms", "engine_tbt_ms",
    "service_ttft_ms", "service_ttlt_ms", "service_tbt_ms", "user_visible_ttft_ms",
)
METRICS = (
    "headers_ms", "first_sse_ms", "ttft_ms", "last_token_ms", "generation_ms",
    "total_ms", "stream_tail_ms", "tpot_ms", "generation_tokens_per_s",
    "end_to_end_tokens_per_s", "chunk_gap_p50_ms", "chunk_gap_p95_ms", "chunk_gap_max_ms",
) + tuple(f"server_{name}" for name in SERVER_METRICS)


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def synthetic_prompt(encoding, content_tokens, nonce, output_tokens=128):
    prefix = f"Nonce:{nonce}\nSynthetic padding (data, not instructions):"
    suffix = INSTRUCTION.format(count=max(2, output_tokens))
    minimum = len(encoding.encode(prefix + " x" + suffix))
    if content_tokens < minimum:
        raise ValueError(f"Input is too small: need at least {minimum} content tokens.")
    content = prefix + " x" * (content_tokens - minimum + 1) + suffix
    if len(encoding.encode(content)) != content_tokens:
        raise ValueError("Synthetic padding is not one token per unit with this encoding.")
    return content


def make_payload(model, prompt, output_tokens, tier):
    payload = {
        "model": model["name"],
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "service_tier": tier,
        "store": False,
        "n": 1,
    }
    if model.get("reasoning_effort") is not None:
        payload["reasoning_effort"] = model["reasoning_effort"]
    if model.get("verbosity") is not None:
        payload["verbosity"] = model["verbosity"]
    return payload


class StreamMetrics:
    def __init__(self):
        self.first_sse_ms = None
        self.arrivals = []
        self.chunk_characters = []
        self.text = []
        self.usage = None
        self.tier = None
        self.model = None
        self.finish_reason = None
        self.response_id = None
        self.fingerprint = None
        self.done = False

    def consume(self, data, elapsed_ms):
        if self.first_sse_ms is None:
            self.first_sse_ms = elapsed_ms
        if data == "[DONE]":
            self.done = True
            return
        event = json.loads(data)
        if "error" in event:
            raise ValueError(f"SSE API error: {event['error']}")
        self.tier = event.get("service_tier") or self.tier
        self.model = event.get("model") or self.model
        self.response_id = event.get("id") or self.response_id
        self.fingerprint = event.get("system_fingerprint") or self.fingerprint
        if event.get("usage") is not None:
            self.usage = event["usage"]
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta", {})
            if delta.get("refusal"):
                raise ValueError(f"Model refusal: {delta['refusal']}")
            content = delta.get("content")
            if content:
                self.arrivals.append(elapsed_ms)
                self.chunk_characters.append(len(content))
                self.text.append(content)

    def result(self, total_ms, encoding):
        first = self.arrivals[0] if self.arrivals else None
        last = self.arrivals[-1] if self.arrivals else None
        generation = last - first if first is not None else None
        gaps = [b - a for a, b in zip(self.arrivals, self.arrivals[1:])]
        usage = self.usage or {}
        output = usage.get("completion_tokens")
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        visible = output - reasoning if output is not None and reasoning is not None else None
        emitted_estimate = len(encoding.encode("".join(self.text))) if self.text else 0
        result = {
            "first_sse_ms": self.first_sse_ms, "ttft_ms": first, "last_token_ms": last,
            "generation_ms": generation, "total_ms": total_ms,
            "stream_tail_ms": total_ms - last if last is not None else None,
            "tpot_ms": (
                generation / (visible - 1)
                if visible and visible > 1 and generation and generation > 0 else None
            ),
            "generation_tokens_per_s": (
                (visible - 1) * 1000 / generation
                if visible and visible > 1 and generation and generation > 0 else None
            ),
            "end_to_end_tokens_per_s": visible * 1000 / total_ms if visible is not None else None,
            "chunk_gap_p50_ms": percentile(gaps, 0.5),
            "chunk_gap_p95_ms": percentile(gaps, 0.95),
            "chunk_gap_max_ms": max(gaps) if gaps else None,
            "content_chunks": len(self.arrivals),
            "chunk_arrivals_ms": self.arrivals, "chunk_characters": self.chunk_characters,
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": output,
            "reasoning_tokens": reasoning, "cached_tokens": cached,
            "visible_tokens": visible, "visible_tokens_local_estimate": emitted_estimate,
            "actual_tier": self.tier, "returned_model": self.model,
            "response_id": self.response_id, "system_fingerprint": self.fingerprint,
            "finish_reason": self.finish_reason, "stream_done": self.done,
            "usage": self.usage,
        }
        checkpoints = usage.get("latency_checkpoint") or {}
        result.update({f"server_{name}": checkpoints.get(name) for name in SERVER_METRICS})
        return result


def request_once(client, credential, config, payload, encoding, timeout):
    # Refresh credentials and serialize outside the measured HTTP interval.
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token.token}", "Content-Type": "application/json",
        "x-ms-client-request-id": str(uuid.uuid4()),
    }
    collector = StreamMetrics()
    result = {"status": "error", "http_status": None, "headers_ms": None}
    start = time.perf_counter()
    deadline = start + timeout
    try:
        with client.stream(
            "POST", config["endpoint"].rstrip("/") + "/chat/completions",
            content=body, headers=headers,
        ) as response:
            result.update({
                "headers_ms": (time.perf_counter() - start) * 1000,
                "http_status": response.status_code,
                "request_id": response.headers.get("apim-request-id") or response.headers.get("x-request-id"),
                "response_headers": {
                    key: value for key, value in response.headers.items()
                    if key.lower().startswith(("x-ratelimit-", "x-ms-region", "retry-after", "openai-processing-ms"))
                },
            })
            if response.is_error:
                response.read()
                raise ValueError(f"HTTP {response.status_code}: {response.text[:4000]}")
            data_lines = []
            for line in response.iter_lines():
                now = time.perf_counter()
                if now > deadline:
                    raise TimeoutError("Total request deadline exceeded.")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                elif not line and data_lines:
                    collector.consume("\n".join(data_lines), (now - start) * 1000)
                    data_lines = []
                    if collector.done:
                        break
            if not collector.done or not collector.usage or not collector.finish_reason:
                raise ValueError("Incomplete SSE: missing DONE, usage, or finish_reason.")
            if not collector.arrivals:
                raise ValueError("Stream completed without visible output.")
            if collector.finish_reason not in {"stop", "length"}:
                raise ValueError(f"Unexpected finish_reason: {collector.finish_reason}")
            result["status"] = "ok"
    except (httpx.HTTPError, ValueError, TimeoutError) as error:
        result["error"] = str(error)
    result.update(collector.result((time.perf_counter() - start) * 1000, encoding))
    return result


def classify(row, input_tolerance, output_tolerance):
    reasons = []
    if row["status"] != "ok":
        reasons.append("request_error")
    if row.get("actual_tier") != row["requested_tier"]:
        reasons.append("tier_mismatch_or_missing")
    if row.get("prompt_tokens") is None or abs(row["prompt_tokens"] - row["input_target"]) > input_tolerance:
        reasons.append("input_tokens_mismatch")
    if row.get("cached_tokens") is None:
        reasons.append("cache_usage_unknown")
    elif row["cached_tokens"] != 0:
        reasons.append("cached_input")
    if row.get("reasoning_tokens") is None:
        reasons.append("reasoning_usage_unknown")
    elif row["reasoning_tokens"] != 0:
        reasons.append("reasoning_output")
    if row.get("visible_tokens") is None or abs(row["visible_tokens"] - row["output_target"]) > output_tolerance:
        reasons.append("output_tokens_mismatch")
    row["exclusions"] = reasons
    row["comparable"] = not reasons


def usage_totals(rows):
    totals = {"requests": len(rows), "errors": sum(row["status"] != "ok" for row in rows)}
    for metric in ("prompt_tokens", "completion_tokens", "visible_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"):
        values = []
        for row in rows:
            value = row.get(metric)
            if metric == "total_tokens":
                prompt, completion = row.get("prompt_tokens"), row.get("completion_tokens")
                value = prompt + completion if prompt is not None and completion is not None else None
            if value is not None:
                values.append(value)
        totals[f"{metric}_sum"] = sum(values) if values or not rows else None
        totals[f"{metric}_reported"] = len(values)
    return totals


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["phase"] == "measure":
            key = (
                row["model"], row["version"], row["input_target"], row["output_target"],
                row["requested_tier"], row.get("actual_tier"), row["comparable"],
            )
            groups[key].append(row)
    summary = []
    for key, group in sorted(groups.items(), key=lambda item: str(item[0])):
        item = dict(zip((
            "model", "version", "input_target", "output_target",
            "requested_tier", "actual_tier", "comparable",
        ), key))
        item.update({
            "attempts": len(group), "successes": sum(row["status"] == "ok" for row in group),
            "errors": sum(row["status"] != "ok" for row in group),
            "http_429": sum(row.get("http_status") == 429 for row in group),
            "exclusions": sorted({reason for row in group for reason in row["exclusions"]}),
        })
        item.update(usage_totals(group))
        for metric in METRICS + ("prompt_tokens", "completion_tokens", "visible_tokens", "cached_tokens", "reasoning_tokens"):
            values = [row[metric] for row in group if row["status"] == "ok" and row.get(metric) is not None]
            mean = statistics.mean(values) if values else None
            stddev = statistics.stdev(values) if len(values) > 1 else None
            item[f"{metric}_n"] = len(values)
            for label, value in (
                ("mean", mean),
                ("p50", percentile(values, 0.5)), ("p90", percentile(values, 0.9)),
                ("p95", percentile(values, 0.95)), ("p99", percentile(values, 0.99)),
                ("min", min(values) if values else None), ("max", max(values) if values else None),
                ("stddev", stddev),
                ("cv_pct", stddev / mean * 100 if stddev is not None and mean else None),
            ):
                item[f"{metric}_{label}"] = value
        summary.append(item)
    return summary


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })


def write_report(folder, rows):
    summary = summarize(rows)
    write_csv(folder / "requests.csv", rows)
    write_csv(folder / "summary.csv", summary)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    measured = [row for row in rows if row["phase"] == "measure"]
    valid = [row for row in measured if row["comparable"]]
    lines = [
        "# Azure OpenAI latency benchmark", "",
        f"Measured: {len(measured)}; comparable: {len(valid)}; "
        f"errors: {sum(row['status'] != 'ok' for row in measured)}.", "",
        "Client-observed timings; GlobalStandard routing is global, not Brazil-only.",
        "Only uncached, zero-reasoning, matching-token, matching-tier samples enter this table.",
        "Small samples are smoke/pilot evidence, not a statistically reliable ranking.", "",
        "| Model | Input | Output | Tier | n | TTFT p50 ms | Total p50 ms | Total p95 ms | Decode TPS p50 |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]

    def number(value):
        return f"{value:.2f}" if value is not None else "N/A"

    for item in summary:
        if item["comparable"]:
            lines.append(
                f"| {item['model']} | {item['input_target']} | {item['output_target']} "
                f"| {item['actual_tier']} | {item['attempts']} "
                f"| {number(item['ttft_ms_p50'])} | {number(item['total_ms_p50'])} "
                f"| {number(item['total_ms_p95'])} | {number(item['generation_tokens_per_s_p50'])} |"
            )
    lines += ["", "## Priority versus Standard", "",
              "Improvement = 100 * (Standard p50 - Priority p50) / Standard p50; positive is faster.",
              "Unpaired aggregate comparison within the same input/output cell.", "",
              "| Model | Input | Output | n default/priority | TTFT improvement | Total improvement |",
              "| --- | ---: | ---: | --- | ---: | ---: |"]
    comparisons = defaultdict(dict)
    for item in summary:
        if item["comparable"]:
            comparisons[(item["model"], item["input_target"], item["output_target"])][item["actual_tier"]] = item
    for (model, inp, out), tiers in sorted(comparisons.items()):
        if "default" in tiers and "priority" in tiers:
            standard, priority = tiers["default"], tiers["priority"]
            gains = [
                100 * (standard[key] - priority[key]) / standard[key]
                for key in ("ttft_ms_p50", "total_ms_p50")
            ]
            lines.append(
                f"| {model} | {inp} | {out} | {standard['attempts']}/{priority['attempts']} "
                f"| {gains[0]:.1f}% | {gains[1]:.1f}% |"
            )
    lines += ["", "## Exclusions and failures", ""]
    excluded = [item for item in summary if not item["comparable"]]
    if not excluded:
        lines.append("None.")
    for item in excluded:
        lines.append(
            f"- {item['model']} {item['input_target']}/{item['output_target']} "
            f"{item['requested_tier']} -> {item['actual_tier']}: n={item['attempts']}, "
            f"errors={item['errors']}; {', '.join(item['exclusions'])}."
        )
    lines += ["", "Raw errors and per-chunk arrival times: requests.jsonl. "
              "All cohorts, including excluded samples: summary.csv.", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return parsed


def nonnegative_int(value):
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Must be nonnegative.")
    return parsed


def nonnegative_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("Must be finite and nonnegative.")
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "benchmark.json")
    parser.add_argument("--models", nargs="+", help="Subset of deployment names.")
    parser.add_argument("--input-tokens", nargs="+", type=positive_int, default=[512, 4096, 16384])
    parser.add_argument("--output-tokens", nargs="+", type=positive_int, default=[128, 512])
    parser.add_argument("--repetitions", type=positive_int, default=20)
    parser.add_argument("--warmups", type=nonnegative_int, default=1, help="Per model/input/output/tier cell.")
    parser.add_argument("--tiers", nargs="+", choices=["default", "priority"], default=["default", "priority"])
    parser.add_argument("--interval", type=nonnegative_float, default=3, help="Seconds after each request.")
    parser.add_argument("--timeout", type=positive_int, default=180, help="Per-I/O timeout and deadline checked between SSE lines.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--input-tolerance", type=nonnegative_float, default=0)
    parser.add_argument("--output-tolerance", type=nonnegative_float, default=0)
    parser.add_argument("--encoding", default="o200k_base")
    parser.add_argument("--result-dir", type=Path)
    parser.add_argument("--cancel-file", type=Path, help="Local control file; stop after the in-flight request.")
    parser.add_argument("--run-id", help="Optional identifier for a local UI-managed run.")
    parser.add_argument("--execute", action="store_true", help="Send billable requests; otherwise print plan only.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    models = config["models"]
    if args.models:
        if set(args.models) - {model["name"] for model in models}:
            parser.error("Unknown model in --models.")
        models = [model for model in models if model["name"] in args.models]
    inputs, outputs = sorted(set(args.input_tokens)), sorted(set(args.output_tokens))
    selected_tiers = list(dict.fromkeys(args.tiers))
    tier_count = len(selected_tiers)
    calls = len(models) * len(inputs) * len(outputs) * tier_count * (args.repetitions + args.warmups)
    estimate = {
        "measured_requests": len(models) * len(inputs) * len(outputs) * tier_count * args.repetitions,
        "warmup_requests": calls - len(models) * len(inputs) * len(outputs) * tier_count * args.repetitions,
        "calibration_requests_max": len(models) * len(inputs) * 4,
        "input_tokens_excluding_calibration": len(models) * sum(inputs) * len(outputs) * tier_count * (args.repetitions + args.warmups),
        "max_output_tokens_excluding_calibration": len(models) * len(inputs) * sum(outputs) * tier_count * (args.repetitions + args.warmups),
        "concurrency": 1, "interval_s": args.interval,
    }
    print(json.dumps(estimate, indent=2), flush=True)
    if not args.execute:
        print("Plan only. --execute sends billable Standard and Priority requests.")
        return 0
    encoding = tiktoken.get_encoding(args.encoding)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = args.result_dir or ROOT / "results" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    rng = random.Random(f"{args.seed}:{run_id}")
    order_rng = random.Random(args.seed)
    rows = []
    metadata = {
        "run_id": run_id, "started_utc": datetime.now(timezone.utc).isoformat(),
        "config": config, "arguments": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "plan": estimate, "platform": platform.platform(), "python": sys.version,
        "packages": {name: importlib.metadata.version(name) for name in ("httpx", "azure-identity", "tiktoken")},
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "selected_models": models,
        "timing_scope": "HTTP serialization and Entra token acquisition excluded; connection and network included.",
        "client_location": "Execution host; not verified to be in Brazil South.",
        "execution": execution_context(),
        "connection_policy": "One HTTP/1.1 connection pool, 120s keepalive expiry, sequential requests, no automatic retries.",
    }
    def save_metadata():
        temporary = folder / "run.tmp"
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temporary.replace(folder / "run.json")

    save_metadata()
    print(f"Results: {folder}", flush=True)

    def prompt_for(target, overhead, output):
        nonce = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=48))
        return synthetic_prompt(encoding, target - overhead, nonce, output)

    class RunCancelled(Exception):
        pass

    def check_cancelled():
        if args.cancel_file and args.cancel_file.exists():
            raise RunCancelled()

    with runtime_credential(AzureCliCredential) as credential, httpx.Client(
        timeout=httpx.Timeout(args.timeout, connect=min(30, args.timeout)),
        transport=httpx.HTTPTransport(
            retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=120),
        ),
        http2=False,
    ) as client, (folder / "requests.jsonl").open("w", encoding="utf-8", buffering=1) as journal:

        def send(model, target, output, tier, phase, repetition, overhead):
            check_cancelled()
            prompt = prompt_for(target, overhead, output)
            payload = make_payload(model, prompt, output, tier)
            row = {
                "run_id": run_id, "started_utc": datetime.now(timezone.utc).isoformat(),
                "sequence": len(rows), "model": model["name"], "version": model["version"],
                "reasoning_effort": model.get("reasoning_effort"),
                "request_parameters": {key: value for key, value in payload.items() if key != "messages"},
                "phase": phase, "repetition": repetition, "input_target": target,
                "output_target": output, "requested_tier": tier,
                "prompt_content_tokens_local": len(encoding.encode(prompt)),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
            row.update(request_once(
                client, credential, config, payload,
                encoding, args.timeout,
            ))
            classify(row, args.input_tolerance, args.output_tolerance)
            rows.append(row)
            journal.write(json.dumps(row) + "\n")
            print(
                f"{phase} {model['name']} {target}/{output} {tier}->{row.get('actual_tier')} "
                f"{row['status']} total={row['total_ms']:.0f}ms "
                f"tokens={row.get('prompt_tokens')}/{row.get('visible_tokens')} "
                f"excluded={','.join(row['exclusions']) or '-'}",
                flush=True,
            )
            end_wait = time.monotonic() + args.interval
            while time.monotonic() < end_wait:
                check_cancelled()
                time.sleep(min(0.2, max(0, end_wait - time.monotonic())))
            return row

        try:
            overheads = {}
            for model, target in itertools.product(models, inputs):
                overhead = 7
                for attempt in range(4):
                    row = send(model, target, 16, "default", "calibrate", attempt, overhead)
                    if row["status"] != "ok":
                        raise RuntimeError(f"Calibration failed: {row.get('error')}")
                    if row["prompt_tokens"] == target:
                        overheads[(model["name"], target)] = overhead
                        break
                    overhead += row["prompt_tokens"] - target
                else:
                    raise RuntimeError(f"Unable to calibrate exact input tokens for {model['name']}.")
            cells = list(itertools.product(models, inputs, outputs))
            for phase, repetitions in (("warmup", args.warmups), ("measure", args.repetitions)):
                for repetition in range(repetitions):
                    order_rng.shuffle(cells)
                    for model, target, output in cells:
                        tiers = selected_tiers.copy()
                        order_rng.shuffle(tiers)
                        for tier in tiers:
                            send(model, target, output, tier, phase, repetition, overheads[(model["name"], target)])
        except RunCancelled:
            metadata["cancelled"] = True
            print("Cancelled: partial results preserved.", flush=True)
        finally:
            write_report(folder, rows)
            metadata["ended_utc"] = datetime.now(timezone.utc).isoformat()
            metadata["measured_requests_completed"] = sum(row["phase"] == "measure" for row in rows)
            metadata["measured_requests_expected"] = estimate["measured_requests"]
            metadata["completed"] = (
                not metadata.get("cancelled", False)
                and metadata["measured_requests_completed"] == estimate["measured_requests"]
            )
            metadata["comparable_requests"] = sum(row["comparable"] for row in rows if row["phase"] == "measure")
            save_metadata()
    measured = [row for row in rows if row["phase"] == "measure"]
    if metadata.get("cancelled"):
        return 3
    return 0 if all(row["comparable"] for row in measured) else 2


if __name__ == "__main__":
    sys.exit(main())
