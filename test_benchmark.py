import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import tiktoken

from benchmark import (
    StreamMetrics, classify, make_payload, percentile, request_once,
    main, summarize, synthetic_prompt, usage_totals, write_report,
)


ENCODING = tiktoken.get_encoding("o200k_base")


def event(content=None, **extra):
    delta = {} if content is None else {"content": content}
    return json.dumps({
        "id": "test-response", "model": "gpt-test-2026-01-01",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        **extra,
    })


def complete_collector(tier="priority", output=3):
    collector = StreamMetrics()
    collector.consume(event(choices=[]), 10)
    collector.consume(event(choices=[{"delta": {"role": "assistant"}}]), 20)
    collector.consume(event("blue", service_tier=tier), 100)
    collector.consume(event(" blue"), 150)
    collector.consume(event(" blue"), 200)
    collector.consume(event(choices=[{"finish_reason": "length"}]), 210)
    collector.consume(event(
        choices=[],
        usage={
            "prompt_tokens": 512, "completion_tokens": output,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    ), 220)
    collector.consume("[DONE]", 230)
    return collector


def valid_row():
    row = {
        "model": "gpt-test", "version": "2026-01-01",
        "phase": "measure", "status": "ok", "http_status": 200,
        "input_target": 512, "output_target": 3, "requested_tier": "priority",
    }
    row.update(complete_collector().result(240, ENCODING))
    classify(row, 0, 0)
    return row


class BenchmarkTests(unittest.TestCase):
    def test_exact_synthetic_lengths_and_cache_busting_prefix(self):
        for size in (128, 505, 4089, 16377):
            first = synthetic_prompt(ENCODING, size, "abc")
            second = synthetic_prompt(ENCODING, size, "z" * 48)
            self.assertEqual(len(ENCODING.encode(first)), size)
            self.assertEqual(len(ENCODING.encode(second)), size)
            self.assertNotEqual(first[:64], second[:64])
        with self.assertRaises(ValueError):
            synthetic_prompt(ENCODING, 1, "abc")

    def test_ttft_ignores_role_and_filter_events_and_total_includes_tail(self):
        row = valid_row()
        self.assertEqual(row["first_sse_ms"], 10)
        self.assertEqual(row["ttft_ms"], 100)
        self.assertEqual(row["generation_ms"], 100)
        self.assertEqual(row["total_ms"], 240)
        self.assertEqual(row["stream_tail_ms"], 40)
        self.assertEqual(row["tpot_ms"], 50)
        self.assertEqual(row["generation_tokens_per_s"], 20)
        self.assertEqual(row["end_to_end_tokens_per_s"], 12.5)
        self.assertEqual(row["chunk_gap_p95_ms"], 50)
        self.assertTrue(row["comparable"])

    def test_single_chunk_does_not_invent_decode_rate(self):
        collector = StreamMetrics()
        collector.consume(event("blue blue blue", usage={
            "prompt_tokens": 10, "completion_tokens": 3,
            "completion_tokens_details": {"reasoning_tokens": 0},
        }), 100)
        result = collector.result(150, ENCODING)
        self.assertEqual(result["ttft_ms"], 100)
        self.assertIsNone(result["tpot_ms"])
        self.assertIsNone(result["generation_tokens_per_s"])
        self.assertIsNone(result["chunk_gap_p50_ms"])

    def test_unknown_usage_not_silently_zero(self):
        collector = StreamMetrics()
        collector.consume(event("blue", usage={"prompt_tokens": 512, "completion_tokens": 1}), 100)
        result = collector.result(120, ENCODING)
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["reasoning_tokens"])
        self.assertIsNone(result["visible_tokens"])
        self.assertIsNone(result["server_engine_ttft_ms"])

    def test_server_checkpoints_are_preserved_without_replacing_client_timings(self):
        collector = complete_collector()
        collector.usage["latency_checkpoint"] = {
            "engine_ttft_ms": 75, "pre_inference_ms": 10, "service_tbt_ms": 0,
        }
        result = collector.result(240, ENCODING)
        self.assertEqual(result["ttft_ms"], 100)
        self.assertEqual(result["server_engine_ttft_ms"], 75)
        self.assertEqual(result["server_pre_inference_ms"], 10)
        self.assertEqual(result["server_service_tbt_ms"], 0)
        self.assertIsNone(result["server_service_ttlt_ms"])

    def test_exclude_fallback_cache_reasoning_and_wrong_token_lengths(self):
        changes = (
            ({"actual_tier": "default"}, "tier_mismatch_or_missing"),
            ({"actual_tier": None}, "tier_mismatch_or_missing"),
            ({"cached_tokens": 256}, "cached_input"),
            ({"reasoning_tokens": 2}, "reasoning_output"),
            ({"prompt_tokens": 513}, "input_tokens_mismatch"),
            ({"visible_tokens": 2}, "output_tokens_mismatch"),
        )
        for change, reason in changes:
            row = {**valid_row(), **change}
            classify(row, 0, 0)
            self.assertFalse(row["comparable"])
            self.assertIn(reason, row["exclusions"])

    def test_output_budget_is_not_a_promise_of_output_length(self):
        row = {**valid_row(), "output_target": 128, "finish_reason": "stop"}
        classify(row, 0, 0)
        self.assertIn("output_tokens_mismatch", row["exclusions"])

    def test_request_has_explicit_tier_and_no_reasoning_for_legacy_model(self):
        model = {"name": "gpt-test", "reasoning_effort": "none", "verbosity": "low"}
        payload = make_payload(model, "synthetic", 128, "default")
        self.assertEqual(payload["service_tier"], "default")
        self.assertEqual(payload["max_completion_tokens"], 128)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["verbosity"], "low")
        self.assertEqual(payload["n"], 1)
        self.assertNotIn("reasoning_effort", make_payload({"name": "gpt-4.1"}, "x", 128, "priority"))
        self.assertNotIn("verbosity", make_payload({"name": "gpt-4.1"}, "x", 128, "priority"))

    def test_http_and_truncated_stream_are_recorded_as_failures(self):
        credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test"))
        config = {"endpoint": "https://example.test/openai/v1/"}
        cases = (
            (429, '{"error":{"code":"RateLimitReached"}}', "HTTP 429"),
            (200, "data: " + event("blue") + "\n\n", "Incomplete SSE"),
            (200, 'data: {"error":{"message":"stream failed"}}\n\n', "SSE API error"),
        )
        for status, content, message in cases:
            seen = []

            def handle(request):
                seen.append(request)
                return httpx.Response(status, content=content, headers={"apim-request-id": "correlation"})

            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                row = request_once(client, credential, config, {}, ENCODING, 30)
            self.assertEqual(row["status"], "error")
            self.assertIn(message, row["error"])
            self.assertEqual(row["request_id"], "correlation")
            self.assertEqual(len(seen), 1)
            self.assertGreater(row["total_ms"], 0)

    def test_complete_stream_and_usage_are_parsed(self):
        content = "\n\n".join([
            "data: " + event("blue", service_tier="priority"),
            "data: " + event(
                choices=[{"finish_reason": "length"}],
                usage={
                    "prompt_tokens": 10, "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            ),
            "data: [DONE]", "",
        ]) + "\n"
        credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test"))
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content),
        )) as client:
            row = request_once(client, credential, {"endpoint": "https://example.test/"}, {}, ENCODING, 30)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["actual_tier"], "priority")
        self.assertEqual(row["visible_tokens"], 1)
        self.assertTrue(row["stream_done"])

    def test_percentiles_and_summary_keep_failed_and_downgraded_cohorts_separate(self):
        self.assertEqual(percentile([100, 200], 0.95), 195)
        self.assertIsNone(percentile([], 0.95))
        good = valid_row()
        downgraded = {**valid_row(), "actual_tier": "default"}
        classify(downgraded, 0, 0)
        failed = {**valid_row(), "status": "error", "http_status": 429}
        classify(failed, 0, 0)
        warmup = {**valid_row(), "phase": "warmup"}
        summary = summarize([good, downgraded, failed, warmup])
        self.assertEqual(sum(item["attempts"] for item in summary), 3)
        self.assertEqual(sum(item["http_429"] for item in summary), 1)
        comparable = [item for item in summary if item["comparable"]]
        self.assertEqual(len(comparable), 1)
        self.assertEqual(comparable[0]["attempts"], 1)
        with tempfile.TemporaryDirectory() as folder:
            write_report(Path(folder), [good, downgraded, failed, warmup])
            report = (Path(folder) / "report.md").read_text(encoding="utf-8")
            self.assertIn("Measured: 3; comparable: 1; errors: 1", report)
            self.assertTrue((Path(folder) / "summary.csv").exists())

    def test_usage_totals_do_not_double_count_reasoning_or_invent_missing_usage(self):
        rows = [
            valid_row(),
            {**valid_row(), "completion_tokens": 5, "reasoning_tokens": 2},
            {**valid_row(), "status": "error", "prompt_tokens": None, "completion_tokens": None,
             "visible_tokens": None, "reasoning_tokens": None, "cached_tokens": None},
        ]
        totals = usage_totals(rows)
        self.assertEqual(totals["requests"], 3)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(totals["total_tokens_sum"], 1032)
        self.assertEqual(totals["total_tokens_reported"], 2)
        self.assertEqual(totals["completion_tokens_sum"], 8)
        self.assertEqual(totals["reasoning_tokens_sum"], 2)
        self.assertIsNone(usage_totals(rows[2:])["total_tokens_sum"])
        self.assertEqual(usage_totals([])["total_tokens_sum"], 0)

    def test_dispersion_and_metric_counts_use_only_available_successful_samples(self):
        first = {**valid_row(), "server_pre_inference_ms": 0}
        second = {**valid_row(), "total_ms": 340, "server_pre_inference_ms": None}
        summary = summarize([first, second])[0]
        self.assertEqual(summary["total_ms_n"], 2)
        self.assertEqual(summary["total_ms_mean"], 290)
        self.assertAlmostEqual(summary["total_ms_stddev"], math.sqrt(5000))
        self.assertAlmostEqual(summary["total_ms_cv_pct"], math.sqrt(5000)/290*100)
        self.assertEqual(summary["server_pre_inference_ms_n"], 1)
        self.assertEqual(summary["server_pre_inference_ms_p50"], 0)
        self.assertIsNone(summary["server_pre_inference_ms_cv_pct"])
        self.assertIsNone(summarize([first])[0]["total_ms_cv_pct"])
        self.assertEqual(summary["total_tokens_sum"], 1030)

    def test_full_matrix_calibrates_server_overhead_and_persists_distinct_phases(self):
        def fake_request(client, credential, config, payload, encoding, timeout):
            collector = complete_collector(payload["service_tier"], payload["max_completion_tokens"])
            collector.usage["prompt_tokens"] = len(encoding.encode(payload["messages"][0]["content"])) + 11
            return {"status": "ok", "http_status": 200, **collector.result(240, encoding)}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({
                "models": [{"name": "gpt-test", "version": "2026-01-01", "reasoning_effort": "none"}],
                "endpoint": "https://example.test/",
            }), encoding="utf-8")
            output = root / "run"
            arguments = [
                "benchmark.py", "--config", str(config), "--result-dir", str(output),
                "--input-tokens", "128", "--output-tokens", "3", "--repetitions", "2",
                "--interval", "0", "--execute",
            ]
            with patch("sys.argv", arguments), patch("benchmark.AzureCliCredential"), patch(
                "benchmark.request_once", side_effect=fake_request,
            ), redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            rows = [
                json.loads(line)
                for line in (output / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["phase"] for row in rows].count("calibrate"), 2)
            self.assertEqual([row["phase"] for row in rows].count("warmup"), 2)
            measured = [row for row in rows if row["phase"] == "measure"]
            self.assertEqual(len(measured), 4)
            self.assertTrue(all(row["prompt_tokens"] == 128 and row["comparable"] for row in measured))
            self.assertEqual({row["actual_tier"] for row in measured}, {"default", "priority"})
            self.assertEqual(len({row["prompt_sha256"] for row in rows}), len(rows))
            manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["completed"])
            self.assertEqual(manifest["comparable_requests"], 4)

    def test_precancelled_run_persists_incomplete_results_without_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cancel = root / "cancel"
            cancel.touch()
            output = root / "results"
            arguments = [
                "benchmark.py", "--models", "gpt-4.1", "--input-tokens", "256",
                "--output-tokens", "16", "--repetitions", "1", "--warmups", "0",
                "--tiers", "priority", "--result-dir", str(output),
                "--cancel-file", str(cancel), "--execute",
            ]
            with patch("sys.argv", arguments), patch("benchmark.AzureCliCredential"), patch(
                "benchmark.request_once",
            ) as request, redirect_stdout(StringIO()):
                self.assertEqual(main(), 3)
                request.assert_not_called()
            manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["cancelled"])
            self.assertFalse(manifest["completed"])
            self.assertEqual(manifest["plan"]["measured_requests"], 1)
            self.assertEqual(manifest["plan"]["warmup_requests"], 0)
            self.assertEqual(manifest["measured_requests_completed"], 0)


if __name__ == "__main__":
    unittest.main()
