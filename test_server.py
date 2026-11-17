import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from server import BenchApp, make_server, read_journal, validate_request
from test_benchmark import valid_row


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
SETTINGS = {
    "models": ["gpt-6-sol", "gpt-4.1"], "tiers": ["default", "priority"],
    "input_tokens": 512, "output_tokens": 128, "repetitions": 1,
    "warmups": 0, "interval": 0, "timeout": 30, "confirm_billing": True,
}


class ServerTests(unittest.TestCase):
    def test_validation_enforces_billing_bounds_and_no_client_endpoints_or_commands(self):
        self.assertEqual(validate_request(SETTINGS, CONFIG)["input_tokens"], 512)
        invalid = (
            {"confirm_billing": False}, {"input_tokens": 255}, {"output_tokens": 8193},
            {"repetitions": 1.5}, {"interval": float("nan")}, {"input_tokens": True},
            {"models": []}, {"models": ["gpt-fake"]}, {"tiers": ["auto"]},
            {"models": ["gpt-4.1", "gpt-4.1"]}, {"endpoint": "https://example.test/"},
            {"command": "ignored"}, {"reasoning_effort": "high"},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_request({**SETTINGS, **change}, CONFIG)

    def test_journal_ignores_only_unfinished_last_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = folder / "requests.jsonl"
            path.write_text('{"phase":"measure"}\n{"phase":', encoding="utf-8")
            self.assertEqual(read_journal(folder), [{"phase": "measure"}])
            path.write_text("invalid\n", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                read_journal(folder)

    def test_http_guard_and_duplicate_start_and_cooperative_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            server = make_server(app, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with httpx.Client(base_url=url, trust_env=False) as client:
                    self.assertEqual(client.get("/api/bootstrap", headers={"Host": "attacker.test"}).status_code, 403)
                    self.assertEqual(client.get("/api/bootstrap", headers={"Origin": "https://attacker.test"}).status_code, 403)
                    bootstrap = client.get("/api/bootstrap").json()
                    headers = {"X-Benchmark-Token": bootstrap["csrf_token"], "Origin": url}
                    self.assertEqual(client.post("/api/start", json=SETTINGS).status_code, 403)
                    self.assertEqual(client.post("/api/start", headers={**headers, "Origin": "null"}, json=SETTINGS).status_code, 403)
                    self.assertEqual(client.get("/../../benchmark.json").status_code, 404)
                    self.assertEqual(client.get("/api/runs").json()["runs"], [])
                    self.assertEqual(client.post("/api/start", headers=headers, json={**SETTINGS, "output_tokens": 0}).status_code, 400)
                    # Keep this test local and non-billable; worker launch is tested separately.
                    with patch.object(app, "_run"):
                        self.assertEqual(client.post("/api/start", headers=headers, json=SETTINGS).status_code, 202)
                    self.assertEqual(client.post("/api/start", headers=headers, json=SETTINGS).status_code, 409)
                    self.assertEqual(client.post("/api/cancel", headers=headers, json={}).status_code, 202)
                    self.assertTrue(app.job["cancel_path"].exists())
                    state = client.get("/api/state").json()["job"]
                    self.assertEqual(state["state"], "stopping")
                    self.assertEqual(state["expected"], 4)
                    self.assertNotIn(app.token, json.dumps(state))
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()

    def test_worker_uses_fixed_argv_persists_partial_results_and_does_not_invoke_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            seen = []

            class Process:
                def __init__(self, command, **kwargs):
                    seen.append((command, kwargs))
                    folder = Path(command[command.index("--result-dir") + 1])
                    folder.mkdir()
                    row = valid_row()
                    (folder / "requests.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                    (folder / "run.json").write_text(json.dumps({
                        "run_id": command[command.index("--run-id") + 1],
                        "completed": False,
                    }), encoding="utf-8")

                def wait(self):
                    return 3

            with patch("server.subprocess.Popen", Process):
                app.start(copy.deepcopy(SETTINGS))
                deadline = time.monotonic() + 5
                while app.job.get("ended") is None and time.monotonic() < deadline:
                    time.sleep(.02)
            self.assertEqual(app.job["state"], "cancelled")
            command, kwargs = seen[0]
            self.assertFalse(kwargs["shell"])
            self.assertIn("--execute", command)
            self.assertTrue((app.job["folder"] / "summary.json").exists())
            config = json.loads(app.job["config_path"].read_text(encoding="utf-8"))
            self.assertEqual(len(config["models"]), 2)
            self.assertEqual(config["models"][0]["reasoning_effort"], "none")
            self.assertEqual(config["models"][0]["verbosity"], "low")
            state = app.state()["job"]
            self.assertEqual(state["measured"], 1)
            self.assertEqual(len(state["run"]["summary"]), 1)


if __name__ == "__main__":
    unittest.main()
