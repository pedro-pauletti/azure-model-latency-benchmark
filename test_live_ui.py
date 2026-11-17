import importlib.util
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import BenchApp, make_server
from benchmark import classify, summarize
from dashboard import render_dashboard
from test_benchmark import valid_row


EDGE = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"


@unittest.skipUnless(importlib.util.find_spec("playwright") and EDGE.exists(), "Optional Playwright + Edge browser check.")
class LiveUiTests(unittest.TestCase):
    def test_report_metrics_context_and_full_png_export_without_inference(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary)
            folder = results / "report-test"
            folder.mkdir()
            first = {**valid_row(), "model": "gpt-4.1", "version": "2025-04-14"}
            second = {**first, "total_ms": 340}
            standard = {**first, "requested_tier": "default", "actual_tier": "default", "total_ms": 300}
            warmup = {**first, "phase": "warmup"}
            downgraded = {**first, "actual_tier": "default"}
            classify(downgraded, 0, 0)
            rows = [first, second, standard, warmup, downgraded]
            (folder / "requests.jsonl").write_text(
                "".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8",
            )
            (folder / "run.json").write_text(json.dumps({
                "run_id": "report-test", "completed": True,
                "started_utc": "2026-11-13T16:00:00+00:00", "ended_utc": "2026-11-13T16:00:10+00:00",
                "config": {"account": "test-foundry", "location": "brazilsouth",
                           "models": [{"name": "gpt-4.1", "version": "2025-04-14", "reasoning_effort": None}]},
                "arguments": {"repetitions": 2, "warmups": 1, "seed": 42, "timeout": 30},
                "plan": {"concurrency": 1, "interval_s": 0, "measured_requests": 4},
            }), encoding="utf-8")
            app = BenchApp(results=results)
            server = make_server(app, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                    page = browser.new_page(viewport={"width": 1200, "height": 800})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}")
                    expect(page.locator("#live-settings")).to_be_enabled()
                    settled = {
                        "id": "report-test", "state": "completed", "phases": {},
                        "measured": 4, "expected": 4, "log": [], "run": app.history()[0],
                    }
                    page.route("**/api/state", lambda route: route.fulfill(json={"job": settled}))
                    expect(page.locator("#job-status")).to_contain_text("Concluído", timeout=10000)
                    page.evaluate("() => { window.settledRow=document.querySelector('#table-body tr'); }")
                    page.wait_for_timeout(1500)
                    self.assertTrue(page.evaluate("() => window.settledRow===document.querySelector('#table-body tr')"))
                    self.assertIn("512 input → 3 output cap", page.locator("#chart-overview").inner_text())
                    self.assertFalse(page.locator("#token-technical").evaluate("(element) => element.open"))
                    page.locator("#token-technical > summary").click()
                    self.assertIn("2.060", page.locator("#report-usage").inner_text())
                    self.assertIn("2.575", page.locator("#report-usage").inner_text())
                    self.assertEqual(page.locator("#table-body tr").count(), 2)
                    self.assertEqual(page.locator('#table-body td[data-field="tier"]').all_text_contents(), ["Standard", "Priority"])
                    page.locator("#table-view").select_option("latency")
                    self.assertEqual(page.locator('th[data-field="total_ms_p99"]').count(), 1)
                    self.assertEqual(page.locator('#table-body td[data-field="total_ms_p99"]').nth(1).inner_text(), "339")
                    page.locator("#table-view").select_option("variability")
                    self.assertEqual(page.locator('#table-body td[data-field="total_ms_cv_pct"]').first.inner_text(), "N/D")
                    self.assertNotEqual(page.locator('#table-body td[data-field="total_ms_cv_pct"]').last.inner_text(), "N/D")
                    page.locator("#stat").select_option("cv_pct")
                    self.assertIn("Escala compartilhada em %", page.locator("#chart-note").inner_text())
                    page.locator("#stat").select_option("p50")
                    page.locator("#table-view").select_option("tokens")
                    self.assertEqual(page.locator('#table-body td[data-field="total_tokens_sum"]').nth(1).inner_text(), "1.030")
                    page.locator("#excluded").check()
                    self.assertIn("pedido Priority", page.locator("#table-body tr").last.inner_text())
                    page.evaluate("""() => {
                      window.reportText=[];
                      const original=CanvasRenderingContext2D.prototype.fillText;
                      CanvasRenderingContext2D.prototype.fillText=function(text,...args) {
                        window.reportText.push(text); return original.call(this,text,...args);
                      };
                    }""")
                    with page.expect_download() as downloaded:
                        page.locator("#export-image").click()
                    data = downloaded.value.path().read_bytes()
                    self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                    width, height = struct.unpack(">II", data[16:24])
                    self.assertEqual(width, 1600)
                    self.assertGreater(height, 800)
                    self.assertLessEqual(height, 1100)
                    drawn = "\n".join(page.evaluate("() => window.reportText"))
                    for text in ("report-test", "512 input", "2.060", "gpt-4.1",
                                 "Total (soma)", "Reasoning", "1.030", "Excluído", "usage 4/4"):
                        self.assertIn(text, drawn)
                    self.assertNotIn("seed", drawn)
                    self.assertIsNone(app.job)
                    self.assertEqual(errors, [])
                    page.evaluate("() => { HTMLCanvasElement.prototype.toBlob=callback => callback(null); }")
                    page.locator("#export-image").click()
                    expect(page.locator("#report-export-error")).to_contain_text("codificar")
                    browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

    def test_post_run_filters_and_sorting_keep_model_tiers_together_in_exports(self):
        import csv
        import io
        from playwright.sync_api import sync_playwright

        models = [
            {"name": "gpt-4.1", "version": "2025-04-14"},
            {"name": "gpt-5.2", "version": "2025-12-11"},
            {"name": "gpt-5.6-sol", "version": "2026-07-09"},
        ]
        rows = []
        for model, tier, total in (
            (models[0], "default", 2500), (models[0], "priority", 1000),
            (models[1], "default", 500), (models[1], "priority", 2000),
            (models[2], "default", 400),
        ):
            rows.append({**valid_row(), "model": model["name"], "version": model["version"],
                         "requested_tier": tier, "actual_tier": tier, "total_ms": total})
        downgraded = {**rows[1], "actual_tier": "default"}
        classify(downgraded, 0, 0)
        rows.append(downgraded)
        run = {"id": "grouped", "completed": True, "models": models, "summary": summarize(rows), "phases": {}}
        template = (Path(__file__).parent / "dashboard.template.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
            path = Path(temporary) / "dashboard.html"
            path.write_text(render_dashboard([run], template, "2026-11-13", models), encoding="utf-8")
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            errors, writes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
            page.goto(path.as_uri())

            def names():
                return page.locator('#table-body td[data-field="model"] .model-name').all_text_contents()

            expected = ["gpt-4.1", "gpt-4.1", "gpt-5.2", "gpt-5.2", "gpt-5.6-sol"]
            self.assertEqual(names(), expected)
            self.assertEqual(page.locator('#table-body td[data-field="tier"]').all_text_contents(),
                             ["Standard", "Priority", "Standard", "Priority", "Standard"])
            self.assertEqual(page.locator('#table-body td[data-field="rank"]').all_text_contents(), ["1", "1", "2", "2", "3"])
            page.locator('th[data-field="total_ms_p50"] button').click()
            self.assertEqual(names(), ["gpt-4.1", "gpt-4.1", "gpt-5.2", "gpt-5.2", "gpt-5.6-sol"])
            page.locator('th[data-field="total_ms_p50"] button').click()
            self.assertEqual(names(), ["gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1", "gpt-5.6-sol"])
            self.assertEqual(page.locator('th[data-field="total_ms_p50"]').get_attribute("aria-sort"), "descending")
            page.locator("#table-reference").select_option("default")
            self.assertEqual(names(), expected)
            page.locator("#table-direction").select_option("asc")
            self.assertEqual(names(), ["gpt-5.6-sol", "gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1"])
            self.assertEqual(page.locator("#run-parameters .model-name").all_text_contents(),
                             ["gpt-5.6-sol", "gpt-5.2", "gpt-4.1"])
            page.locator("#table-sort").select_option("model")
            page.locator("#table-direction").select_option("desc")
            self.assertEqual(names(), ["gpt-5.6-sol", "gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1"])
            page.locator("#table-sort").select_option("chart")
            page.locator("#sort").select_option("standard")
            self.assertEqual(names(), ["gpt-5.6-sol", "gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1"])
            page.locator("#pair-filter").select_option("paired")
            self.assertEqual(names(), ["gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1"])
            page.locator("#result-search").fill("2025-04")
            self.assertEqual(names(), ["gpt-4.1", "gpt-4.1"])
            page.locator("#excluded").check()
            self.assertEqual(names(), ["gpt-4.1"] * 3)
            self.assertIn("pedido Priority", page.locator("#table-body tr").last.inner_text())
            page.locator("#clear-result-filters").click()
            self.assertEqual(names(), ["gpt-5.6-sol", "gpt-5.2", "gpt-5.2", "gpt-4.1", "gpt-4.1", "gpt-4.1"])
            page.locator("#excluded").uncheck()
            page.locator("#result-search").fill("GPT-5.2")
            self.assertEqual(names(), ["gpt-5.2", "gpt-5.2"])
            with page.expect_download() as downloaded:
                page.locator("#export").click()
            exported = list(csv.DictReader(io.StringIO(downloaded.value.path().read_text(encoding="utf-8-sig"))))
            self.assertEqual([row["model"] for row in exported], ["gpt-5.2"] * 2)
            self.assertEqual([row["requested_tier"] for row in exported], ["default", "priority"])
            page.evaluate("""() => {
              window.reportText=[];
              const original=CanvasRenderingContext2D.prototype.fillText;
              CanvasRenderingContext2D.prototype.fillText=function(text,...args) {
                window.reportText.push(text); return original.call(this,text,...args);
              };
            }""")
            with page.expect_download() as downloaded:
                page.locator("#export-image").click()
            self.assertEqual(downloaded.value.path().read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            drawn = page.evaluate("() => window.reportText")
            self.assertLess(drawn.index("Standard"), drawn.index("Priority"))
            self.assertNotIn("gpt-4.1", drawn)
            self.assertIn("gpt-5.2", drawn)
            page.locator("#result-search").fill("no-match")
            self.assertEqual(names(), [])
            self.assertTrue(page.locator("#export-image").is_disabled())
            self.assertEqual(page.locator("#run-parameters tr").count(), 0)
            page.locator("#clear-result-filters").click()
            self.assertEqual(errors, [])
            self.assertEqual(writes, [])
            browser.close()

    def test_sliders_billing_gate_start_and_cancel_are_nonbillable_until_start(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            methods = []
            errors = []

            def fake_worker(job):
                with app.lock:
                    job["state"] = "running"
                deadline = time.monotonic() + 15
                while not job["cancel_path"].exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                with app.lock:
                    job["state"] = "cancelled" if job["cancel_path"].exists() else "failed"

            server = make_server(app, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with patch.object(app, "_run", side_effect=fake_worker), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                    page = browser.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: methods.append((request.method, request.url)))
                    page.goto(f"http://127.0.0.1:{server.server_port}")
                    expect(page.locator("#live-settings")).to_be_enabled()
                    page.locator("#live-execution").select_option("local")
                    page.locator("#live-input").fill("4096")
                    page.locator("#live-input").dispatch_event("input")
                    self.assertEqual(page.locator("#live-input-number").input_value(), "4096")
                    page.locator("#live-input-number").fill("512")
                    self.assertEqual(page.locator("#live-input").input_value(), "512")
                    self.assertIsNone(app.job)
                    self.assertTrue(page.locator("#start").is_disabled())
                    page.locator("#billing").check()
                    page.locator("#start").click()
                    expect(page.locator("#cancel")).to_be_enabled()
                    page.locator("#cancel").click()
                    expect(page.locator("#job-status")).to_contain_text("Cancelado", timeout=10000)
                    self.assertTrue(app.job["cancel_path"].exists())
                    self.assertEqual([method for method, url in methods if url.endswith("/api/start")], ["POST"])
                    self.assertEqual([method for method, url in methods if url.endswith("/api/cancel")], ["POST"])
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                if app.job and app.job["state"] in {"starting", "running", "stopping"}:
                    app.cancel()
                if app.worker:
                    app.worker.join(timeout=3)
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
