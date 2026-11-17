"""Optional, nonbillable Edge checks for the tool-routing dashboard."""

import csv
import importlib.util
import io
import json
import os
import shutil
import struct
import threading
import unittest
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashboard import render_dashboard


ROOT = Path(__file__).resolve().parent
EDGE = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
MODELS = [
    {"name": "Microsoft-Decision-1", "version": "1", "kind": "decision",
     "reasoning_effort": None, "verbosity": None},
    {"name": "gpt-4.1", "version": "2025-04-14", "kind": "llm",
     "reasoning_effort": None, "verbosity": None},
    {"name": "gpt-5.4-mini", "version": "2026-03-17", "kind": "llm",
     "reasoning_effort": "none", "verbosity": "low"},
]
TOOLS = [
    {"name": "route_billing", "description": "Encaminhar cobrança"},
    {"name": "route_technical", "description": "Encaminhar suporte técnico"},
    {"name": "route_account", "description": "Encaminhar conta"},
]
CATALOG = {
    "models": MODELS, "tools": TOOLS, "profile": "tool-routing-v1",
    "decision_model": MODELS[0],
    "cases": [
        {"id": f"case-{index + 1}", "text": f"Solicitação {index + 1}",
         "expected_tool": TOOLS[index % 3]["name"]}
        for index in range(12)
    ],
    "limits": {
        "cases": {"min": 3, "max": 12, "default": 6},
        "repetitions": {"min": 1, "max": 50, "default": 3},
        "warmups": {"min": 0, "max": 3, "default": 1},
        "padding_tokens": {"min": 0, "max": 16384, "default": 0},
        "interval": {"min": 0, "max": 30, "default": 1},
        "timeout": {"min": 10, "max": 300, "default": 60},
    },
}


def tool_run():
    summary, rows = [], []
    for model, tier, accuracy, comparable, latency in (
        (MODELS[0], "decision", 100, 1, 40),
        (MODELS[1], "default", 100, 1, 80),
        (MODELS[1], "priority", 100, 1, 20),
        (MODELS[2], "default", 0, 0, 1),
        ({"name": "missing-usage", "version": "1", "kind": "llm"}, "default", 100, 1, None),
    ):
        summary.append({
            "model": model["name"], "version": model["version"], "kind": model["kind"],
            "requested_tier": tier, "attempts": 2, "valid_calls": 1, "correct": 1 if accuracy else 0,
            "errors": 1, "accuracy_pct": accuracy, "comparable": comparable, "excluded": 2 - comparable,
            "http_429": 0, "selection_ms_p50": latency, "selection_ms_p95": latency,
            "agent_total_ms_p50": latency, "selection_ms_stddev": None, "selection_ms_cv_pct": None,
            "prompt_tokens_sum": None if latency is None else 42, "prompt_tokens_reported": 0 if latency is None else 1,
            "completion_tokens_sum": None, "completion_tokens_reported": 0,
            "total_tokens_sum": None, "total_tokens_reported": 0,
        })
        rows.append({
            "phase": "measure", "model": model["name"], "version": model["version"], "kind": model["kind"],
            "requested_tier": tier, "actual_tier": None if tier == "decision" else tier,
            "case_id": "case-1", "expected_tool": "route_billing",
            "predicted_tool": "route_billing" if accuracy else "route_technical",
            "status": "ok", "correct": bool(accuracy), "comparable": bool(comparable),
            "exclusions": [] if comparable else ["incorrect_tool"], "selection_ms": latency,
            "http_total_ms": latency, "headers_ms": None, "tool_ms": .1 if accuracy else None,
            "agent_total_ms": latency, "http_status": 200, "error": None,
            "tool_call": {"id": "test", "type": "function",
                          "function": {"name": "route_billing", "arguments": "{}"}},
            "tool_output": {"route": "billing"}, "prompt_tokens": 42 if latency else None,
            "completion_tokens": None, "total_tokens": None, "cached_tokens": 0, "reasoning_tokens": 0,
        })
    rows.append({
        **rows[1], "case_id": "case-2", "status": "error", "correct": False,
        "comparable": False, "selection_ms": None, "predicted_tool": None,
        "error": "=UNTRUSTED()", "exclusions": ["request_error"], "actual_tier": "default",
    })
    rows.append({
        **rows[2], "case_id": "case-3", "actual_tier": "default",
        "comparable": False, "exclusions": ["tier_mismatch_or_missing"],
    })
    return {
        "id": "tools-fixture", "kind": "tools", "started": "2026-11-13T16:00:00Z",
        "ended": "2026-11-13T16:01:00Z", "completed": True, "cancelled": False,
        "expected": len(rows), "measured": len(rows), "account": "fake-account",
        "project": "fake-project", "location": "brazilsouth", "profile": "tool-routing-v1",
        "settings": {"models": [model["name"] for model in MODELS], "tiers": ["default", "priority"],
                     "cases": 6, "repetitions": 3, "warmups": 1, "padding_tokens": 0,
                     "interval": 1, "timeout": 60, "seed": 42},
        "models": MODELS, "tools": TOOLS, "cases": CATALOG["cases"][:6], "summary": summary, "rows": rows,
    }


def html_fixture(runs=None):
    template = (ROOT / "dashboard.template.html").read_text(encoding="utf-8")
    return render_dashboard([], template, "2026-11-13T16:00:00Z", MODELS[1:],
                            tool_runs=runs if runs is not None else [tool_run()], tool_catalog=CATALOG)


def tool_job(run, state, error=None):
    return {
        "id": run["id"], "kind": "tools", "state": state, "settings": run["settings"],
        "started": run["started"], "phases": {}, "measured": run["measured"],
        "expected": run["expected"], "run": run, "log": [], "error": error,
    }


@contextmanager
def fake_server(unavailable=False, execution_targets=None, default_execution="local"):
    state = {"posts": [], "job": None, "text_busy": False, "state_reads": 0, "runs_reads": 0,
             "runs": [tool_run()]}
    html = html_fixture().encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'")
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/bootstrap":
                if unavailable:
                    self.send_json({"error": "fixture unavailable"}, 503)
                else:
                    self.send_json({"csrf_token": "fixture-token", "tool_benchmark": CATALOG,
                                    "execution_targets": execution_targets or [
                                        {"id": "local", "label": "Local · este computador", "available": True}],
                                    "default_execution": default_execution})
            elif self.path == "/api/runs":
                self.send_json({"runs": []})
            elif self.path == "/api/tools/runs":
                state["runs_reads"] += 1
                self.send_json({"runs": state["runs"]})
            elif self.path == "/api/state":
                self.send_json({"job": None, "busy": state["text_busy"] or bool(state["job"] and state["job"]["state"] == "running")})
            elif self.path == "/api/tools/state":
                state["state_reads"] += 1
                job = state["job"]
                self.send_json({"job": job, "busy": state["text_busy"] or bool(job and job["state"] == "running")})
            else:
                self.send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.headers.get("X-Benchmark-Token") != "fixture-token":
                self.send_json({"error": "Invalid CSRF token"}, 403)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append((self.path, payload, dict(self.headers)))
            if self.path == "/api/tools/start":
                run = tool_run()
                run["id"] = "new-tool-run"
                run["completed"] = False
                state["job"] = {"id": run["id"], "kind": "tools", "state": "running", "settings": payload,
                                "started": run["started"], "phases": {"measure": {"requests": 1, "errors": 0}},
                                "measured": 1, "expected": 90, "run": run, "log": ["fake tool dispatch"]}
                self.send_json(state["job"])
            elif self.path == "/api/cancel":
                state["job"]["state"] = "cancelled"
                state["job"]["run"]["cancelled"] = True
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Unexpected write"}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@unittest.skipUnless(importlib.util.find_spec("playwright") and EDGE.exists(), "Optional Playwright + Edge browser check.")
class ToolUiTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel="msedge", headless=True)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.fixture_dir = ROOT / ("ui-tool-fixture-" + uuid.uuid4().hex)
        self.fixture_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.fixture_dir)
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)

    def offline(self, runs=None):
        path = self.fixture_dir / "dashboard.html"
        path.write_text(html_fixture(runs), encoding="utf-8")
        self.page.goto(path.as_uri())

    def names(self):
        return self.page.locator("#tools-table-body .model-name").all_text_contents()

    def test_offline_tabs_preserve_token_controls_and_load_saved_agent_results(self):
        from playwright.sync_api import expect

        page = self.page
        writes = []
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
        self.offline()
        expect(page.get_by_role("tab", name="Tokens · latência LLM")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#token-panel")).to_be_visible()
        expect(page.locator("#tools-panel")).to_be_hidden()
        page.locator("#live-input-number").evaluate("(input) => { input.value='4096'; }")
        page.locator("#tools-tab").focus()
        page.locator("#tools-tab").press("Enter")
        expect(page.locator("#tools-panel")).to_be_visible()
        expect(page.locator("#token-panel")).to_be_hidden()
        expect(page.locator("#tools-connection")).to_contain_text("Modo offline")
        expect(page.locator("#tools-start")).to_be_disabled()
        self.assertEqual(self.names(), ["Microsoft-Decision-1", "gpt-4.1", "gpt-4.1", "missing-usage", "gpt-5.4-mini"])
        self.assertEqual(page.locator("#tools-cases-preview li").count(), 6)
        self.assertEqual(page.locator("#tools-catalog li").count(), 3)
        expect(page.locator("#tools-context")).to_contain_text("fake-account")
        page.locator("#tools-tab").press("ArrowLeft")
        expect(page.locator("#token-tab")).to_be_focused()
        expect(page.locator("#token-panel")).to_be_visible()
        self.assertEqual(page.locator("#live-input-number").input_value(), "4096")
        self.assertEqual(page.locator("#live-input-number").count(), 1)
        self.assertEqual(writes, [])
        self.assertEqual(self.errors, [])

    def test_grouped_sort_missing_metrics_raw_filters_and_safe_csv_png_exports(self):
        from playwright.sync_api import expect

        page = self.page
        self.offline()
        page.locator("#tools-tab").click()
        processing = page.locator("#tools-table-body tr td:nth-child(3)").all_text_contents()
        self.assertEqual(processing[:3], ["API Decision · Global Standard (sem Priority)", "Standard", "Priority"])
        self.assertEqual(self.names().count("Microsoft-Decision-1"), 1)
        self.assertEqual(page.locator('#tools-table-body tr').last.locator('[data-field="selection_ms_p50"]').inner_text(), "N/D")
        page.locator("#tools-sort").select_option("selection_ms_p50")
        self.assertEqual(self.names()[:3], ["Microsoft-Decision-1", "gpt-4.1", "gpt-4.1"])
        self.assertEqual(set(self.names()[-2:]), {"gpt-5.4-mini", "missing-usage"})
        page.locator("#tools-direction").select_option("desc")
        self.assertEqual(self.names()[:3], ["gpt-4.1", "gpt-4.1", "Microsoft-Decision-1"])
        page.locator("#tools-search").fill("GPT-4.1")
        self.assertEqual(self.names(), ["gpt-4.1", "gpt-4.1"])
        with page.expect_download() as downloaded:
            page.locator("#tools-export").click()
        summary = list(csv.DictReader(io.StringIO(downloaded.value.path().read_text(encoding="utf-8-sig"))))
        self.assertEqual([row["requested_tier"] for row in summary], ["default", "priority"])
        with page.expect_download() as downloaded:
            page.locator("#tools-export-image").click()
        image = downloaded.value.path().read_bytes()
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", image[16:24])
        self.assertGreater(width, 1200)
        self.assertGreater(height, 200)
        page.locator("#tools-search").fill("missing-usage")
        page.locator("#tools-view").select_option("tokens")
        expect(page.locator('#tools-table-body [data-field="prompt_tokens_sum"]')).to_have_text("N/D")
        expect(page.locator('#tools-table-body [data-field="prompt_tokens_reported"]')).to_have_text("0 / 2")
        page.locator("#tools-search").fill("gpt-4.1")
        expect(page.locator('#tools-table-body [data-field="prompt_tokens_sum"]').first).to_have_text("42 *")
        page.locator("#tools-raw-status").select_option("incorrect")
        expect(page.locator("#tools-raw-body tr")).to_have_count(1)
        expect(page.locator("#tools-raw-body")).to_contain_text("route_technical")
        page.locator("#tools-raw-status").select_option("success")
        expect(page.locator("#tools-raw-body tr")).to_have_count(4)
        page.locator("#tools-raw-status").select_option("excluded")
        expect(page.locator("#tools-raw-body tr")).to_have_count(3)
        page.locator("#tools-raw-model").select_option("gpt-4.1")
        page.locator("#tools-raw-case").select_option("case-3")
        expect(page.locator("#tools-raw-body tr")).to_have_count(1)
        tier = page.locator("#tools-raw-body tr td").nth(1)
        self.assertEqual(tier.inner_text(), "Priority")
        self.assertIn("retornado: default", tier.get_attribute("title"))
        page.locator("#tools-raw-status").select_option("error")
        page.locator("#tools-raw-case").select_option("all")
        expect(page.locator("#tools-raw-body tr")).to_have_count(1)
        with page.expect_download() as downloaded:
            page.locator("#tools-export-raw").click()
        raw = list(csv.DictReader(io.StringIO(downloaded.value.path().read_text(encoding="utf-8-sig"))))
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["error"], "'=UNTRUSTED()")
        self.assertIn('"arguments":"{}"', raw[0]["tool_call"].replace(" ", ""))
        page.locator("#tools-search").fill("no-match")
        expect(page.locator("#tools-export")).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_live_defaults_payload_billing_global_busy_cancel_and_stable_terminal_table(self):
        from playwright.sync_api import expect

        page = self.page
        with fake_server() as (url, state):
            page.goto(url)
            expect(page.locator("#live-input-number")).to_be_enabled()
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-repetitions")).to_be_enabled()
            expect(page.locator("#tools-start")).to_be_disabled()
            self.assertEqual(state["posts"], [])
            self.assertEqual(page.locator("#tools-models input:checked").count(), 3)
            expect(page.locator("#tools-estimate")).to_contain_text("90 medições")
            expect(page.locator("#tools-estimate")).to_contain_text("5 warmups")
            page.locator("#tools-priority").uncheck()
            expect(page.locator("#tools-estimate")).to_contain_text("54 medições")
            page.locator("#tools-standard").uncheck()
            expect(page.locator("#tools-estimate")).to_contain_text("18 medições")
            page.locator("#tools-billing").check()
            expect(page.locator("#tools-start")).to_be_disabled()
            page.locator("#tools-billing").uncheck()
            page.locator("#tools-standard").check()
            page.locator("#tools-priority").check()
            page.locator("#tools-padding").fill("1024")
            page.locator("#tools-padding").dispatch_event("input")
            self.assertEqual(page.locator("#tools-padding-number").input_value(), "1024")
            page.locator("#tools-padding-number").fill("0")
            self.assertEqual(page.locator("#tools-padding").input_value(), "0")
            page.locator("#tools-repetitions").fill("0")
            page.locator("#tools-billing").check()
            expect(page.locator("#tools-start")).to_be_disabled()
            page.locator("#tools-repetitions").fill("3")
            page.locator("#tools-start").click()
            expect(page.locator("#tools-cancel")).to_be_enabled()
            expect(page.locator("#tools-repetitions")).to_be_disabled()
            expect(page.locator("#start")).to_be_disabled()
            payload = state["posts"][0][1]
            self.assertEqual(state["posts"][0][0], "/api/tools/start")
            self.assertEqual(payload, {
                "models": [model["name"] for model in MODELS], "tiers": ["default", "priority"],
                "cases": 6, "repetitions": 3, "warmups": 1, "padding_tokens": 0,
                "interval": 1, "timeout": 60, "execution_target": "local", "confirm_billing": True,
            })
            headers = {key.lower(): value for key, value in state["posts"][0][2].items()}
            self.assertEqual(headers["x-benchmark-token"], "fixture-token")
            page.locator("#tools-cancel").click()
            expect(page.locator("#tools-job-status")).to_contain_text("Cancelado")
            expect(page.locator("#tools-repetitions")).to_be_enabled()
            self.assertEqual([path for path, _, _ in state["posts"]], ["/api/tools/start", "/api/cancel"])
            page.locator("#tools-search").fill("gpt-4.1")
            page.locator("#tools-direction").select_option("desc")
            page.locator("#tools-table-body tr").first.evaluate("(row) => window.savedToolRow = row")
            reads = state["state_reads"]
            page.wait_for_timeout(1400)
            self.assertGreater(state["state_reads"], reads)
            self.assertTrue(page.locator("#tools-table-body tr").first.evaluate("(row) => row === window.savedToolRow"))
            self.assertEqual(self.names(), ["gpt-4.1", "gpt-4.1"])
            self.assertEqual(page.locator("#run option").count(), 1)
            self.assertNotIn("new-tool-run", page.locator("#run").inner_text())
            state["text_busy"] = True
            expect(page.locator("#tools-repetitions")).to_be_disabled(timeout=5000)
            expect(page.locator("#tools-cancel")).to_be_disabled()
            self.assertEqual(self.errors, [])

    def test_unavailable_backend_keeps_offline_results_and_never_posts(self):
        from playwright.sync_api import expect

        page = self.page
        with fake_server(unavailable=True) as (url, state):
            page.goto(url)
            expect(page.locator("#live-error")).to_contain_text("Backend indisponível")
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-start")).to_be_disabled()
            self.assertEqual(len(self.names()), 5)
            self.assertEqual(state["posts"], [])
            self.assertEqual(self.errors, [])

    def test_polling_follows_external_jobs_clears_errors_and_preserves_explicit_history_selection(self):
        from playwright.sync_api import expect

        page = self.page
        older = tool_run()
        older.update(id="older-failed", completed=False, measured=2, rows=older["rows"][:2])
        running = tool_run()
        running.update(id="externally-started", completed=False, measured=3, rows=running["rows"][:3])
        with fake_server() as (url, state):
            state["runs"] = [older]
            state["job"] = tool_job(older, "failed", "Falha no benchmark antigo.")
            page.goto(url)
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            expect(page.locator("#tools-error")).to_have_text("Falha no benchmark antigo.")
            page.locator("#tools-search").fill("gpt-4.1")
            page.locator("#tools-direction").select_option("desc")
            page.locator("#tools-view").select_option("latency")

            state["job"] = tool_job(running, "running")
            expect(page.locator("#tools-run")).to_have_value(running["id"])
            expect(page.locator("#tools-job-status")).to_contain_text("Executando")
            expect(page.locator("#tools-error")).to_be_empty()
            expect(page.locator("#tools-context")).to_contain_text(running["id"])
            expect(page.locator("#tools-raw-body tr")).to_have_count(3)
            expect(page.locator("#tools-search")).to_have_value("gpt-4.1")
            expect(page.locator("#tools-direction")).to_have_value("desc")
            expect(page.locator("#tools-view")).to_have_value("latency")

            state["job"] = tool_job(running, "running", "Erro transitório do job atual.")
            expect(page.locator("#tools-error")).to_have_text("Erro transitório do job atual.")
            state["job"] = tool_job(running, "running")
            expect(page.locator("#tools-error")).to_be_empty()

            completed = tool_run()
            completed["id"] = running["id"]
            state["job"] = tool_job(completed, "completed")
            expect(page.locator("#tools-job-status")).to_contain_text("Concluído")
            expect(page.locator("#tools-run")).to_have_value(completed["id"])
            expect(page.locator("#tools-raw-body tr")).to_have_count(len(completed["rows"]))
            expect(page.locator("#tools-error")).to_be_empty()
            page.locator("#tools-table-body tr").first.evaluate("(row) => window.followedToolRow = row")
            page.wait_for_timeout(1200)
            self.assertTrue(page.locator("#tools-table-body tr").first.evaluate("(row) => row === window.followedToolRow"))

            page.locator("#tools-run").select_option(older["id"])
            expect(page.locator("#tools-context")).to_contain_text(older["id"])
            page.locator("#tools-table-body tr").first.evaluate("(row) => window.historicalToolRow = row")
            page.wait_for_timeout(1200)
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            self.assertTrue(page.locator("#tools-table-body tr").first.evaluate("(row) => row === window.historicalToolRow"))
            another = tool_run()
            another.update(id="another-external-job", completed=False)
            state["job"] = tool_job(another, "running")
            expect(page.locator("#tools-job-status")).to_contain_text("Executando")
            expect(page.locator("#tools-run option[value='another-external-job']")).to_have_count(1)
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            expect(page.locator("#tools-search")).to_have_value("gpt-4.1")
            self.assertEqual(state["posts"], [])
            self.assertEqual(self.errors, [])

    def test_fresh_page_follows_current_terminal_job_over_older_partial_history(self):
        from playwright.sync_api import expect

        page = self.page
        older = tool_run()
        older.update(id="older-partial", completed=False, measured=2, rows=older["rows"][:2])
        current = tool_run()
        current["id"] = "newest-terminal"
        with fake_server() as (url, state):
            state["runs"] = [older]
            state["job"] = tool_job(current, "completed")
            page.goto(url)
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-job-status")).to_contain_text("Concluído")
            expect(page.locator("#tools-run")).to_have_value(current["id"])
            expect(page.locator("#tools-context")).to_contain_text(current["id"])
            expect(page.locator("#tools-raw-body tr")).to_have_count(len(current["rows"]))
            expect(page.locator("#tools-error")).to_be_empty()
            with page.expect_download() as downloaded:
                page.locator("#tools-export").click()
            summary = list(csv.DictReader(io.StringIO(downloaded.value.path().read_text(encoding="utf-8-sig"))))
            self.assertTrue(summary)
            self.assertEqual({row["run_id"] for row in summary}, {current["id"]})
            page.locator("#tools-run").select_option(older["id"])
            page.wait_for_timeout(1200)
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            state["runs"] = [older, current]
            page.reload()
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-run")).to_have_value(current["id"])
            self.assertEqual(state["posts"], [])
            self.assertEqual(self.errors, [])

    def test_newest_completed_history_is_default_offline_and_after_server_restart(self):
        from playwright.sync_api import expect

        page = self.page
        partial = tool_run()
        partial.update(id="newer-partial", started="2026-11-13T19:00:00Z", completed=False)
        older = tool_run()
        older.update(id="older-complete", started="2026-11-12T16:00:00Z")
        newest = tool_run()
        newest.update(id="newest-complete", started="2026-11-13T17:00:00Z")
        runs = [partial, older, newest]
        self.offline(runs)
        page.locator("#tools-tab").click()
        expect(page.locator("#tools-run")).to_have_value(newest["id"])
        expect(page.locator("#tools-context")).to_contain_text(newest["id"])
        with fake_server() as (url, state):
            state["runs"] = runs
            page.goto(url)
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-run")).to_have_value(newest["id"])
            expect(page.locator("#tools-job-status")).to_have_text("Nenhuma execução do agente ativa.")
            page.locator("#tools-run").select_option(older["id"])
            page.wait_for_timeout(1200)
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            self.assertEqual(state["posts"], [])
            self.assertEqual(self.errors, [])

    def test_late_initial_state_does_not_override_explicit_history_selection(self):
        from playwright.sync_api import expect

        page = self.page
        older = tool_run()
        older.update(id="older-history-choice", started="2026-11-12T16:00:00Z", completed=False,
                     measured=2, rows=older["rows"][:2])
        current = tool_run()
        current.update(id="current-terminal", started="2026-11-13T17:00:00Z")
        delayed = []
        with fake_server() as (url, state):
            state["runs"] = [older, current]
            state["job"] = tool_job(current, "completed")
            page.route("**/api/tools/state", lambda route: delayed.append(route))
            page.goto(url)
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-run")).to_have_value(current["id"])
            page.locator("#tools-run").select_option(older["id"])
            self.assertTrue(delayed)
            page.unroute("**/api/tools/state")
            for route in delayed:
                route.fulfill(json={"job": state["job"], "busy": False})
            expect(page.locator("#tools-job-status")).to_contain_text("Concluído")
            expect(page.locator("#tools-run")).to_have_value(older["id"])
            expect(page.locator("#tools-context")).to_contain_text(older["id"])
            expect(page.locator("#tools-raw-body tr")).to_have_count(2)
            self.assertEqual(state["posts"], [])
            self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
