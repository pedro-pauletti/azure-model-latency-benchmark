import json
import tempfile
import unittest
from pathlib import Path

from dashboard import load_runs, render_dashboard
from test_benchmark import valid_row


class DashboardTests(unittest.TestCase):
    def test_reads_runs_without_pooling_or_including_raw_sensitive_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary)
            for name, complete in (("pilot", True), ("failed", False)):
                folder = results / name
                folder.mkdir()
                (folder / "run.json").write_text(json.dumps({
                    "run_id": name, "completed": complete,
                    "arguments": {"config": "private-local-path"},
                    "config": {"account": "foundry-test", "project": "test-project"},
                }), encoding="utf-8")
                (folder / "summary.json").write_text(
                    json.dumps([{"model": name}] if complete else []), encoding="utf-8",
                )
                (folder / "requests.jsonl").write_text(json.dumps({
                    **valid_row(), "model": name,
                    "phase": "measure" if complete else "calibrate",
                    "status": "ok" if complete else "error",
                    "error": "private error content",
                }) + "\n", encoding="utf-8")
            runs = load_runs(results)
            self.assertEqual(len(runs), 2)
            pilot = next(run for run in runs if run["id"] == "pilot")
            failed = next(run for run in runs if run["id"] == "failed")
            self.assertEqual(pilot["summary"][0]["model"], "pilot")
            self.assertEqual(pilot["summary"][0]["total_tokens_sum"], 515)
            self.assertEqual(failed["summary"], [])
            self.assertEqual(failed["phases"]["calibrate"]["errors"], 1)
            self.assertNotIn("private", json.dumps(runs))

    def test_report_context_preserves_scope_exact_usage_and_safe_parameters(self):
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary)
            folder = results / "run"
            folder.mkdir()
            (folder / "run.json").write_text(json.dumps({
                "started_utc": "2026-11-13T16:00:00+00:00",
                "ended_utc": "2026-11-13T16:00:10+00:00",
                "arguments": {"repetitions": 1, "warmups": 1, "seed": 42, "config": "private-path"},
            }), encoding="utf-8")
            rows = [valid_row(), {**valid_row(), "phase": "warmup"}, {**valid_row(), "phase": "calibrate"}]
            (folder / "requests.jsonl").write_text(
                "".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8",
            )
            run = load_runs(results)[0]
            self.assertEqual(run["duration_s"], 10)
            self.assertEqual(run["usage"]["all"]["total_tokens_sum"], 1545)
            self.assertEqual(run["usage"]["phases"]["measure"]["total_tokens_sum"], 515)
            self.assertEqual(run["summary"][0]["attempts"], 1)
            self.assertEqual(run["settings"], {"repetitions": 1, "warmups": 1, "seed": 42})

    def test_embedded_json_cannot_terminate_script_or_inject_markup(self):
        malicious = "</script><img src=x onerror=alert(1)>&"
        template = '<script id="benchmark-data" type="application/json">__BENCHMARK_DATA__</script>'
        html = render_dashboard([{"id": malicious}], template, "2026-11-13")
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<img", html)
        payload = html.split(">", 1)[1].rsplit("</script>", 1)[0]
        self.assertEqual(json.loads(payload)["runs"][0]["id"], malicious)

    def test_empty_results_and_invalid_template_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "No benchmark runs"):
                load_runs(Path(temporary))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            render_dashboard([], "invalid", "now")


if __name__ == "__main__":
    unittest.main()
