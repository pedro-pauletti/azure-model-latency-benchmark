"""Build an offline HTML dashboard from existing benchmark results; no Azure calls."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmark import summarize, usage_totals
from runtime_context import historical_context

ROOT = Path(__file__).resolve().parent
PLACEHOLDER = "__BENCHMARK_DATA__"


def report_details(manifest, rows):
    arguments = manifest.get("arguments", {})
    started, ended = manifest.get("started_utc"), manifest.get("ended_utc")
    return {
        "ended": ended,
        "execution": historical_context(manifest),
        "duration_s": (
            (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
            if started and ended else None
        ),
        "settings": {key: arguments[key] for key in (
            "repetitions", "warmups", "timeout", "seed", "encoding", "tiers",
            "input_tolerance", "output_tolerance",
            "interval", "input_tokens", "output_tokens", "cases", "padding_tokens",
        ) if key in arguments},
        "connection_policy": manifest.get("connection_policy"),
        "usage": {
            "all": usage_totals(rows),
            "phases": {phase: usage_totals([row for row in rows if row["phase"] == phase])
                       for phase in ("calibrate", "warmup", "measure")},
        } if rows is not None else None,
    }


def load_runs(results, exclude=None, allow_empty=False):
    runs = []
    for folder in sorted(results.iterdir()):
        if folder == exclude or not folder.is_dir() or not (folder / "run.json").is_file():
            continue
        manifest = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        summary_path = folder / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else []
        rows_path = folder / "requests.jsonl"
        phases = {}
        rows = []
        if rows_path.exists():
            for line in rows_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(row)
                phase = phases.setdefault(row["phase"], {"requests": 0, "errors": 0})
                phase["requests"] += 1
                phase["errors"] += row["status"] != "ok"
            summary = summarize(rows)
        config = manifest.get("config", {})
        runs.append({
            "id": manifest.get("run_id", folder.name),
            "started": manifest.get("started_utc"),
            "completed": manifest.get("completed", False),
            "expected": manifest.get("measured_requests_expected", manifest.get("plan", {}).get("measured_requests")),
            "account": config.get("account"),
            "project": config.get("project"),
            "location": config.get("location"),
            "concurrency": manifest.get("plan", {}).get("concurrency"),
            "interval": manifest.get("plan", {}).get("interval_s"),
            "source_hash": manifest.get("benchmark_sha256"),
            "models": manifest.get("selected_models", config.get("models", [])),
            "cancelled": manifest.get("cancelled", False),
            "phases": phases,
            "summary": summary,
            **report_details(manifest, rows if rows_path.exists() else None),
        })
    if not runs and exclude is None and not allow_empty:
        raise ValueError(f"No benchmark runs found in {results}")
    return runs


def render_dashboard(runs, template, generated_at, models=None, tool_runs=None, tool_catalog=None):
    payload = json.dumps(
        {"generated_at": generated_at, "runs": runs, "models": models or [],
         "tool_runs": tool_runs or [], "tool_catalog": tool_catalog or {}}, ensure_ascii=True, allow_nan=False,
    )
    # JSON lives in a script data element: prevent HTML parser termination/injection.
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError("Dashboard template must contain exactly one data placeholder.")
    return template.replace(PLACEHOLDER, payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "dashboard.html")
    args = parser.parse_args()
    runs = load_runs(args.results)
    template = (ROOT / "dashboard.template.html").read_text(encoding="utf-8")
    config = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
    from tool_benchmark import catalog, load_runs as load_tool_runs
    html = render_dashboard(
        runs, template, datetime.now(timezone.utc).isoformat(), config["models"],
        tool_runs=load_tool_runs(ROOT / "tool-results"), tool_catalog=catalog(config),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Dashboard: {args.output.resolve()}")
    print(f"Embedded runs: {len(runs)}; no network or new inference requests.")


if __name__ == "__main__":
    main()
