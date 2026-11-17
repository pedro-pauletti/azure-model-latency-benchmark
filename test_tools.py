import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from server import BenchApp, make_server
from tool_agent import CASES, REGISTRY, ToolSelectionAgent, make_payload, read_call
from tool_benchmark import catalog, expected_requests, load_runs, profiles, summarize, validate_request
import tool_benchmark

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
DECISION = catalog(CONFIG)["decision_model"]
LLM = {**CONFIG["models"][-1], "kind": "llm"}
SETTINGS = {
    "models": ["Microsoft-Decision-1", "gpt-4.1"], "tiers": ["default", "priority"],
    "cases": 3, "repetitions": 1, "warmups": 0, "padding_tokens": 0,
    "interval": 0, "timeout": 30, "confirm_billing": True,
}


def llm_response(name="route_billing", arguments="{}", tier="default"):
    return {
        "service_tier": tier, "model": "gpt-4.1",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}],
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 11, "total_tokens": 111,
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }


def invoke(response, model=LLM, tier="default", expected="route_billing", status=200):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status, json=response)

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(
        token="not-a-real-credential", expires_on=int(time.time()) + 3600))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        row = ToolSelectionAgent(client, credential, CONFIG).invoke(model, "synthetic ticket", tier, expected, 30)
    return row, seen


def measured(row):
    return {"phase": "measure", "model": LLM["name"], "version": LLM["version"],
            "kind": "llm", "requested_tier": "default", "case_id": CASES[0]["id"],
            "expected_tool": "route_billing", **row}


class ToolTests(unittest.TestCase):
    def test_selectors_have_identical_zero_argument_tools_but_distinct_native_protocols(self):
        llm = make_payload(LLM, "same state", "priority")
        decision = make_payload(DECISION, "same state", "decision")
        self.assertEqual(llm["messages"][-1]["content"], decision["state"])
        self.assertEqual({tool["function"]["name"]: tool["function"]["description"] for tool in llm["tools"]},
                         decision["questions"]["tool"]["criteria"])
        self.assertEqual(decision["model"], "decision-1")
        self.assertNotIn("service_tier", decision)
        self.assertNotIn("tools", decision)
        self.assertFalse(llm["stream"])
        self.assertFalse(llm["parallel_tool_calls"])
        self.assertEqual(llm["tool_choice"], "required")
        self.assertNotIn("reasoning_effort", llm)
        gpt = make_payload({**CONFIG["models"][0], "kind": "llm"}, "state", "default")
        self.assertEqual(gpt["reasoning_effort"], "none")
        self.assertEqual(gpt["verbosity"], "low")
        with self.assertRaises(ValueError):
            make_payload(DECISION, "state", "priority")

    def test_agent_executes_the_same_allowlisted_tool_for_native_and_adapted_calls(self):
        llm, seen = invoke(llm_response())
        decision, _ = invoke({"answers": {"tool": {
            "type": "choice", "choice": "route_billing",
            "probabilities": {"route_billing": .98, "route_technical": .01, "route_account": .01},
        }}}, DECISION, "decision")
        for row in (llm, decision):
            self.assertEqual(row["status"], "ok")
            self.assertTrue(row["correct"])
            self.assertTrue(row["comparable"])
            self.assertEqual(row["tool_output"], REGISTRY["route_billing"]())
            self.assertLessEqual(row["headers_ms"], row["http_total_ms"])
            self.assertLessEqual(row["http_total_ms"], row["selection_ms"])
            self.assertLessEqual(row["selection_ms"], row["agent_total_ms"])
            self.assertNotIn("not-a-real-credential", json.dumps(row))
        self.assertEqual(seen[0]["max_completion_tokens"], 128)
        self.assertIsNone(decision["actual_tier"])
        self.assertIsNone(decision["completion_tokens"])
        self.assertIsNone(decision["cached_tokens"])
        self.assertEqual(llm["total_tokens"], 111)

    def test_agent_reuses_unexpired_token_instead_of_spawning_cli_for_every_call(self):
        requests = []
        token_calls = []

        def get_token(scope, **kwargs):
            token_calls.append(scope)
            return SimpleNamespace(token="local-only-token", expires_on=int(time.time()) + 3600)

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=llm_response())

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            agent = ToolSelectionAgent(client, SimpleNamespace(get_token=get_token), CONFIG)
            for _ in range(2):
                result = agent.invoke(LLM, "Synthetic ticket", "default", "route_billing", 30)
                self.assertEqual(result["status"], "ok")
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(token_calls), 1)

    def test_wrong_choice_downgrade_and_cache_never_enter_comparable_latencies(self):
        wrong, _ = invoke(llm_response("route_technical"))
        downgraded, _ = invoke(llm_response(), tier="priority")
        cached_data = llm_response()
        cached_data["usage"]["prompt_tokens_details"]["cached_tokens"] = 20
        cached, _ = invoke(cached_data)
        self.assertEqual(wrong["status"], "ok")
        self.assertFalse(wrong["correct"])
        self.assertIn("wrong_tool", wrong["exclusions"])
        self.assertIn("tier_mismatch_or_missing", downgraded["exclusions"])
        self.assertIn("cached_input", cached["exclusions"])
        summary = summarize([measured(row) for row in (wrong, downgraded, cached)])[0]
        self.assertEqual(summary["comparable"], 0)
        self.assertEqual(summary["selection_ms_n"], 0)
        self.assertIsNone(summary["selection_ms_p50"])

    def test_invalid_or_multiple_calls_and_provider_errors_are_visible_without_dispatch(self):
        invalid = [llm_response(arguments='{"queue":"production"}'),
                   llm_response(arguments="[]"), llm_response("delete_everything")]
        multiple = llm_response()
        multiple["choices"][0]["message"]["tool_calls"] *= 2
        invalid.append(multiple)
        for data in invalid:
            with self.subTest(data=data), patch.dict(REGISTRY, {"route_billing": lambda: self.fail("unsafe dispatch")}):
                row, _ = invoke(data)
                self.assertEqual(row["status"], "error")
                self.assertFalse(row["correct"])
                self.assertFalse(row["comparable"])
                self.assertTrue(row["error"])
                self.assertIsNone(row["tool_output"])
        row, _ = invoke({"error": "quota exhausted"}, status=429)
        self.assertEqual(row["http_status"], 429)
        self.assertIn("quota exhausted", row["error"])
        for data in ({"answers": []}, {"answers": {"tool": {"type": "noul"}}},
                     {"answers": {"tool": {"type": "choice", "choice": "unsafe"}}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                read_call(data, "decision")

    def test_accuracy_counts_errors_and_incorrect_attempts_not_just_successes(self):
        good, _ = invoke(llm_response())
        wrong, _ = invoke(llm_response("route_account"))
        error, _ = invoke({}, status=500)
        result = summarize([measured(row) for row in (good, wrong, error)])[0]
        self.assertAlmostEqual(result["accuracy_pct"], 100 / 3)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["valid_calls"], 2)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(result["comparable"], 1)
        self.assertEqual(result["selection_ms_n"], 1)
        self.assertIsNone(result["selection_ms_stddev"])
        self.assertEqual(result["total_tokens_sum"], 222)
        self.assertEqual(result["total_tokens_reported"], 2)

    def test_request_validation_and_balanced_schedule_do_not_duplicate_decision_for_tiers(self):
        settings = validate_request(SETTINGS, CONFIG)
        models = [DECISION, LLM]
        self.assertEqual(len(profiles(models, settings["tiers"])), 3)
        self.assertEqual(expected_requests(models, settings), 9)
        for count in (3, 6, 9, 12):
            self.assertEqual({sum(case["expected_tool"] == tool for case in CASES[:count])
                              for tool in REGISTRY}, {count // 3})
        for change in ({"cases": 4}, {"models": ["unknown"]}, {"models": []},
                       {"tiers": ["decision"]}, {"padding_tokens": True},
                       {"interval": float("inf")}, {"confirm_billing": False},
                       {"endpoint": "https://attacker.test"}, {"command": "unsafe"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_request({**SETTINGS, **change}, CONFIG)

    def test_global_job_lock_and_api_results_remain_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            server = make_server(app, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with httpx.Client(base_url=url, trust_env=False) as client:
                    token = client.get("/api/bootstrap").json()["csrf_token"]
                    headers = {"X-Benchmark-Token": token, "Origin": url}
                    self.assertEqual(client.post("/api/tools/start", json=SETTINGS).status_code, 403)
                    with patch.object(app, "_run"):
                        self.assertEqual(client.post("/api/tools/start", headers=headers, json=SETTINGS).status_code, 202)
                    self.assertEqual(client.get("/api/state").json(), {"job": None, "busy": True})
                    state = client.get("/api/tools/state").json()
                    self.assertEqual(state["job"]["expected"], 9)
                    self.assertEqual(state["job"]["run"]["kind"], "tools")
                    self.assertEqual(client.get("/api/runs").json()["runs"], [])
                    self.assertEqual(client.get("/api/tools/runs").json()["runs"], [])
                    self.assertEqual(client.post("/api/tools/start", headers=headers, json=SETTINGS).status_code, 409)
                    text_settings = {"confirm_billing": True, "models": ["gpt-4.1"], "tiers": ["default"]}
                    self.assertEqual(client.post("/api/start", headers=headers, json=text_settings).status_code, 409)
                    self.assertEqual(client.post("/api/cancel", headers=headers, json={}).status_code, 202)
                    self.assertTrue(app.job["cancel_path"].exists())
            finally:
                server.shutdown()
                thread.join(3)
                server.server_close()

    def test_tool_history_preserves_partial_complete_lines_and_never_uses_text_summarizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "partial"
            folder.mkdir()
            row, _ = invoke(llm_response())
            (folder / "requests.jsonl").write_text(json.dumps(measured(row)) + '\n{"unfinished":', encoding="utf-8")
            (folder / "run.json").write_text(json.dumps({
                "run_id": "partial", "kind": "tools", "completed": False, "cancelled": True,
            }), encoding="utf-8")
            run = load_runs(root)[0]
            self.assertEqual(run["measured"], 1)
            self.assertEqual(run["summary"][0]["accuracy_pct"], 100)
            self.assertFalse(run["completed"])
            self.assertTrue(run["cancelled"])

    def test_precancelled_tool_runner_preserves_manifest_without_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings.json"
            settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
            cancel = root / "cancel"
            cancel.touch()
            folder = root / "result"
            args = ["tool_benchmark.py", "--settings", str(settings), "--cancel-file", str(cancel),
                    "--result-dir", str(folder), "--run-id", "cancelled-tools", "--execute"]
            with patch("sys.argv", args), patch.object(ToolSelectionAgent, "invoke") as inference:
                self.assertEqual(tool_benchmark.main(), 3)
                inference.assert_not_called()
            run = tool_benchmark.load_run(folder)
            self.assertTrue(run["cancelled"])
            self.assertFalse(run["completed"])
            self.assertEqual(run["measured"], 0)
            self.assertEqual(run["expected"], 9)

    def test_tool_worker_launch_and_partial_recovery_use_only_the_tool_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            seen = []
            row, _ = invoke(llm_response())

            class Process:
                def __init__(self, command, **kwargs):
                    seen.append((command, kwargs))
                    folder = Path(command[command.index("--result-dir") + 1])
                    folder.mkdir()
                    (folder / "requests.jsonl").write_text(json.dumps(measured(row)) + "\n", encoding="utf-8")
                    (folder / "run.json").write_text(json.dumps({
                        "run_id": "partial-tools", "kind": "tools", "completed": False, "cancelled": True,
                    }), encoding="utf-8")

                def wait(self):
                    return 3

            with patch("server.subprocess.Popen", Process):
                app.start(copy.deepcopy(SETTINGS), "tools")
                app.worker.join(5)
            self.assertFalse(app.worker.is_alive())
            command, kwargs = seen[0]
            self.assertFalse(kwargs["shell"])
            self.assertIn(str(ROOT / "tool_benchmark.py"), command)
            self.assertNotIn("--input-tokens", command)
            self.assertIn("--settings", command)
            self.assertEqual(app.job["state"], "cancelled")
            self.assertEqual(app.state(), {"job": None, "busy": False})
            state = app.state("tools")["job"]
            self.assertEqual(state["run"]["summary"][0]["correct"], 1)
            self.assertEqual(state["measured"], 1)
            self.assertTrue((app.job["folder"] / "summary.csv").exists())
            self.assertEqual(len(app.history("tools")), 1)
            self.assertEqual(app.history("tools")[0]["id"], "partial-tools")


if __name__ == "__main__":
    unittest.main()
