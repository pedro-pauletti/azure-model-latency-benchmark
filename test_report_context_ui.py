"""Nonbillable browser coverage for integrated reports and runner selection."""

import importlib.util
import json
import shutil
import struct
import unittest
import uuid

from benchmark import classify, summarize, usage_totals
from dashboard import render_dashboard
from test_benchmark import valid_row
from test_tool_ui import CATALOG, EDGE, MODELS, ROOT, fake_server, tool_run


EXECUTION = {
    "target": "containerapps", "label": "Azure Container Apps · Brazil South",
    "region": "brazilsouth", "resource_id": "/subscriptions/test/jobs/benchmark-runner",
    "cpu": 1, "memory": "2Gi", "parallelism": 1, "replica_retry_limit": 0,
    "run_id": "runner-test", "image": "test.azurecr.io/benchmark@sha256:123",
    "revision_or_execution": "job-execution-test", "authentication": "managed_identity",
}
TARGETS = [
    {"id": "local", "label": "Local · este computador", "available": True},
    {"id": "containerapps", "label": EXECUTION["label"], "available": True, "region": "brazilsouth"},
]


def token_run():
    rows = []
    for model in ["gpt-4.1", "gpt-5.4-mini"]:
        for tier in ["default", "priority"]:
            rows.append({**valid_row(), "model": model, "version": "2026-test",
                         "requested_tier": tier, "actual_tier": tier})
    excluded = {**rows[1], "actual_tier": "default"}
    classify(excluded, 0, 0)
    rows += [excluded, {**rows[0], "phase": "warmup"}, {**rows[0], "phase": "calibrate"}]
    return {
        "id": "integrated-token-run", "completed": True, "started": "2026-11-16T12:00:00Z",
        "ended": "2026-11-16T12:01:00Z", "duration_s": 60, "account": "model-account",
        "project": "model-project", "location": "eastus2", "execution": EXECUTION.copy(),
        "concurrency": 1, "interval": 2, "expected": 5,
        "settings": {"repetitions": 7, "warmups": 1, "timeout": 180, "seed": 42,
                     "input_tolerance": 0, "output_tolerance": 0},
        "models": [{"name": model, "version": "2026-test", "reasoning_effort": "none", "verbosity": "low"}
                   for model in ["gpt-4.1", "gpt-5.4-mini"]],
        "summary": summarize(rows),
        "phases": {phase: {"requests": sum(row["phase"] == phase for row in rows), "errors": 0}
                   for phase in ["calibrate", "warmup", "measure"]},
        "usage": {"all": usage_totals(rows), "phases": {
            phase: usage_totals([row for row in rows if row["phase"] == phase])
            for phase in ["calibrate", "warmup", "measure"]}},
    }


def rich_tool_run():
    run = tool_run()
    run.update(execution=EXECUTION.copy(), location="eastus2", concurrency=1, interval=1, duration_s=60)

    def totals(requests):
        return {
            "requests": requests, "errors": 0, "prompt_tokens_sum": requests * 42,
            "completion_tokens_sum": requests * 3, "total_tokens_sum": requests * 45,
            "cached_tokens_sum": 0, "reasoning_tokens_sum": 0,
            **{field + "_reported": requests for field in
               ["prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"]},
        }

    for row in run["summary"]:
        if row["prompt_tokens_reported"]:
            row.update(completion_tokens_sum=3, completion_tokens_reported=1,
                       total_tokens_sum=45, total_tokens_reported=1)
    run["usage"] = {"all": totals(12), "phases": {"warmup": totals(2), "measure": totals(10)}}
    return run


def nine_model_runs(count=9):
    token, tools = token_run(), rich_tool_run()
    models = [{"name": f"gpt-fixture-{index+1:02}", "version": "2026-11-16", "kind": "llm",
               "reasoning_effort": "none", "verbosity": "low"} for index in range(count)]
    rows = [{**valid_row(), "model": model["name"], "version": model["version"],
             "requested_tier": tier, "actual_tier": tier, "total_ms": 100 + index * 30 + offset * 10}
            for index, model in enumerate(models) for offset, tier in enumerate(["default", "priority"])]
    token.update(models=models, summary=summarize(rows), expected=len(rows), measured=len(rows),
                 usage={"all": usage_totals(rows), "phases": {"measure": usage_totals(rows)}})
    template = tools["summary"][1]
    tools.update(models=models, summary=[
        {**template, "model": model["name"], "version": model["version"], "requested_tier": tier}
        for model in models for tier in ["default", "priority"]])
    return token, tools


@unittest.skipUnless(importlib.util.find_spec("playwright") and EDGE.exists(), "Optional Playwright + Edge browser check.")
class ReportContextUiTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel="msedge", headless=True)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.fixture_dir = ROOT / ("ui-report-fixture-" + uuid.uuid4().hex)
        self.fixture_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.fixture_dir)
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)

    def offline(self, token=None, tools=None):
        template = (ROOT / "dashboard.template.html").read_text(encoding="utf-8")
        historical = token_run()
        historical["id"] = "historical"
        historical.pop("execution")
        historical_tools = rich_tool_run()
        historical_tools["id"] = "tools-historical"
        historical_tools.pop("execution")
        path = self.fixture_dir / "dashboard.html"
        path.write_text(render_dashboard([token or token_run(), historical], template, "2026-11-16", MODELS,
                                         tool_runs=[tools or rich_tool_run(), historical_tools], tool_catalog=CATALOG), encoding="utf-8")
        self.page.goto(path.as_uri())
        self.page.locator("#run").select_option("integrated-token-run")
        self.page.locator("#tools-run").select_option("tools-fixture", force=True)

    def capture_canvas(self):
        self.page.evaluate("""() => {
          window.drawnText=[]; window.drawnBars=[];
          const text=CanvasRenderingContext2D.prototype.fillText, rect=CanvasRenderingContext2D.prototype.fillRect;
          CanvasRenderingContext2D.prototype.fillText=function(value,...args) {
            window.drawnText.push(String(value)); return text.call(this,value,...args);
          };
          CanvasRenderingContext2D.prototype.fillRect=function(x,y,w,h) {
            window.drawnBars.push({color:this.fillStyle,x,y,w,h}); return rect.call(this,x,y,w,h);
          };
        }""")

    def png(self, button):
        with self.page.expect_download() as downloaded:
            self.page.locator(button).click()
        data = downloaded.value.path().read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual(width, 1600)
        self.assertLessEqual(height, 16384)
        self.last_png_dimensions = (width, height)
        self.last_png_name = downloaded.value.suggested_filename
        return "\n".join(self.page.evaluate("() => window.drawnText"))

    def test_chart_context_and_real_bar_png_keep_entire_usage_separate_from_selection(self):
        page = self.page
        self.offline()
        text = page.locator("#chart-overview").inner_text()
        for value in ["512 input → 3 output cap", "eastus2", "brazilsouth", "7 repetições", "1 warmup", "concorrência 1"]:
            self.assertIn(value, text)
        self.assertFalse(page.locator("#token-technical").evaluate("(element) => element.open"))
        self.assertEqual(page.locator("#chart-report .report-overview").count(), 0)
        usage = page.locator("#chart-usage").inner_text()
        self.assertIn("Tokens medidos: 2.575", usage)
        self.assertIn("usage 5/5", usage)
        self.assertNotIn("3.605", usage)
        page.locator("#token-technical > summary").click()
        self.assertIn("total 3.605", page.locator("#report-usage").inner_text())
        self.assertIn("7/7", page.locator("#report-usage").inner_text())
        self.assertIn("Calibração · execução inteira", page.locator("#report-usage").inner_text())
        page.locator("#token-technical > summary").click()
        page.locator("#result-search").fill("gpt-4.1")
        usage = page.locator("#chart-usage").inner_text()
        self.assertIn("Tokens medidos: 1.545", usage)
        self.assertIn("3/3", usage)
        self.assertIn("inclui excluídas", page.locator("#chart-caveats").inner_text())
        page.locator("#excluded").check()
        self.assertEqual(page.locator("#chart-usage").inner_text(), usage)
        page.locator("#result-search").fill("")
        page.evaluate("() => document.documentElement.dataset.theme='dark'")
        self.capture_canvas()
        drawn = self.png("#export-chart-image")
        for value in ["integrated-token-run", "eastus2", "brazilsouth", "7 repetições",
                      "Tokens medidos: 2.575", "gpt-4.1", "gpt-5.4-mini", "Standard:", "Priority:",
                      "ganho positivo", "p50", "Filtro:", "n pequeno", "Cold start"]:
            self.assertIn(value, drawn)
        for value in ["managed_identity", "2Gi", "seed 42", "runner-test", "3.605"]:
            self.assertNotIn(value, drawn)
        self.assertEqual(self.last_png_dimensions, (1600, 900))
        # These are actual Canvas2D bar rectangles, not a table screenshot labelled as a chart.
        bar_colors = page.evaluate("""() => {
          const ctx=document.createElement('canvas').getContext('2d'),css=getComputedStyle(document.documentElement);
          return ['accent','border-strong'].map(key => {ctx.fillStyle=css.getPropertyValue('--cp-'+key);return ctx.fillStyle;});
        }""")
        bars = [bar for bar in page.evaluate("() => window.drawnBars")
                if bar["color"] in bar_colors and bar["h"] == 12 and bar["w"] > 0]
        self.assertEqual(len(bars), 4)
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.locator("#chart-report").bounding_box()["width"], 390)
        self.assertEqual(self.errors, [])
        page.evaluate("() => HTMLCanvasElement.prototype.toBlob=callback => callback(null)")
        page.locator("#export-chart-image").click()
        self.assertIn("codificar", page.locator("#chart-export-error").inner_text())

    def test_historical_runner_is_unknown_not_inferred_from_model_region_or_next_form(self):
        self.offline()
        page = self.page
        page.locator("#run").select_option("historical")
        page.locator("#live-input-number").evaluate("(input) => { input.value='999'; }")
        text = page.locator("#chart-overview").inner_text()
        self.assertIn("Executor não registrado", text)
        self.assertIn("Recurso modelo · eastus2", text)
        self.assertNotIn("Azure Container Apps", text)
        self.assertIn("512 input", text)
        self.assertNotIn("999 input", text)
        page.locator("#tools-tab").click()
        page.locator("#tools-run").select_option("tools-historical")
        text = page.locator("#tools-overview").inner_text()
        self.assertIn("Executor não registrado", text)
        self.assertNotIn("Azure Container Apps", text)
        self.assertEqual(self.errors, [])

    def test_tools_structured_context_and_png_expose_controls_phase_usage_and_filters(self):
        self.offline()
        page = self.page
        page.locator("#tools-tab").click()
        self.assertFalse(page.locator("#tools-technical").evaluate("(element) => element.open"))
        brief = page.locator("#tools-overview").inner_text()
        self.assertIn("6 casos", brief)
        self.assertIn("brazilsouth", brief)
        self.assertIn("Tokens medidos: 180* (4/10 chamadas)", page.locator("#tools-brief").inner_text())
        page.locator("#tools-technical > summary").click()
        text = page.locator("#tools-context").inner_text()
        for value in ["tools-fixture", "eastus2", "brazilsouth", "2Gi", "Repetições 3",
                      "warmups 1", "Concorrência 1", "intervalo 1", "timeout 60", "seed 42",
                      "6 casos", "padding local", "tool_choice=required"]:
            self.assertIn(value, text)
        usage = page.locator("#tools-usage").inner_text()
        self.assertIn("total 540", usage)
        self.assertIn("total 90", usage)
        self.assertIn("total 180* (4/10 chamadas)", usage)
        self.assertIn("Warmup · execução inteira", usage)
        page.locator("#tools-technical > summary").click()
        page.locator("#tools-search").fill("gpt-4.1")
        usage = page.locator("#tools-brief").inner_text()
        self.assertNotIn("540", usage)
        self.assertIn("Tokens medidos: 90* (2/4 chamadas)", usage)
        self.capture_canvas()
        drawn = self.png("#tools-export-image")
        for value in ["tools-fixture", "brazilsouth", "eastus2", "padding 0",
                      "Tokens medidos: 90* (2/4 chamadas)", "usage 2/4", "gpt-4.1",
                      "function call", "busca gpt-4.1"]:
            self.assertIn(value, drawn)
        self.assertNotIn("seed 42", drawn)
        self.assertNotIn("Warmup · execução inteira", drawn)
        self.assertNotIn("Microsoft-Decision-1", drawn)
        self.assertNotIn('{"models"', drawn)
        self.assertEqual(self.errors, [])

    def test_execution_targets_defaults_payloads_and_disabled_azure_fallback(self):
        from playwright.sync_api import expect

        page = self.page
        with fake_server(execution_targets=TARGETS, default_execution="containerapps") as (url, state):
            page.goto(url)
            expect(page.locator("#live-settings")).to_be_enabled()
            self.assertEqual(page.locator("#live-execution").input_value(), "containerapps")
            expect(page.locator("#live-estimate")).to_contain_text("Runner: Azure Container Apps")
            writes = []

            def token_start(route):
                writes.append(json.loads(route.request.post_data))
                route.fulfill(status=400, json={"error": "nonbillable fixture intercepted"})

            page.route("**/api/start", token_start)
            page.locator("#billing").check()
            page.locator("#start").click()
            expect(page.locator("#live-error")).to_contain_text("fixture intercepted")
            self.assertEqual(writes[0]["execution_target"], "containerapps")
            page.locator("#tools-tab").click()
            expect(page.locator("#tools-settings")).to_be_enabled()
            self.assertEqual(page.locator("#tools-execution").input_value(), "containerapps")
            expect(page.locator("#tools-estimate")).to_contain_text("Runner: Azure Container Apps")
            page.locator("#tools-billing").check()
            page.locator("#tools-start").click()
            expect(page.locator("#tools-cancel")).to_be_enabled()
            self.assertEqual(state["posts"][0][1]["execution_target"], "containerapps")
            state["job"]["execution"] = EXECUTION
            state["job"]["state"] = "starting"
            state["job"]["startup_duration_s"] = 12.5
            expect(page.locator("#tools-job-startup")).to_contain_text("12,5 s", timeout=10000)
        unavailable = [TARGETS[0], {**TARGETS[1], "available": False}]
        with fake_server(execution_targets=unavailable, default_execution="containerapps") as (url, state):
            page.goto(url)
            expect(page.locator("#live-settings")).to_be_enabled()
            for selector in ["#live-execution", "#tools-execution"]:
                self.assertEqual(page.locator(selector).input_value(), "local")
                expect(page.locator(selector + ' option[value="containerapps"]')).to_have_js_property("disabled", True)
            expect(page.locator("#live-estimate")).to_contain_text("Runner: Local")
            page.locator("#billing").check()
            page.locator("#start").click()
            expect(page.locator("#live-error")).to_contain_text("fixture intercepted")
            self.assertEqual(writes[-1]["execution_target"], "local")
            self.assertEqual(state["posts"], [])
        self.assertEqual(self.errors, [])

    def test_nine_pairs_fit_capture_view_and_compact_native_pngs_without_hiding_results(self):
        from playwright.sync_api import expect

        page = self.page
        token, tools = nine_model_runs()
        self.offline(token, tools)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#share-chart").click()
        expect(page.locator("#live-panel")).to_be_hidden()
        expect(page.locator(".filters-panel")).to_be_hidden()
        self.assertEqual(page.locator("#chart .chart-row").count(), 9)
        self.assertEqual(page.locator("#chart .track").count(), 18)
        report = page.locator("#chart-report").bounding_box()
        last = page.locator("#chart .chart-row").last.bounding_box()
        self.assertLessEqual(report["y"] + report["height"], 1000)
        self.assertLessEqual(last["y"] + last["height"], 1000)
        self.assertEqual(page.locator("#chart-report details").count(), 0)
        expect(page.locator("#chart-usage")).to_contain_text("usage 18/18")
        page.keyboard.press("Escape")
        expect(page.locator("#live-panel")).to_be_visible()
        self.capture_canvas()
        drawn = self.png("#export-chart-image")
        self.assertEqual(self.last_png_dimensions, (1600, 900))
        self.assertTrue(self.last_png_name.startswith("grafico-benchmark-integrated-token-run"))
        for index in range(9):
            self.assertIn(f"gpt-fixture-{index+1:02}", drawn)
        self.assertNotIn("managed_identity", drawn)
        # Summary table keeps all 18 tier rows, with a small essential header.
        self.page.evaluate("() => {window.drawnText=[];}")
        table_text = self.png("#export-image")
        self.assertLessEqual(self.last_png_dimensions[1], 1200)
        for index in range(9):
            self.assertIn(f"gpt-fixture-{index+1:02}", table_text)
        page.locator("#tools-tab").click()
        page.locator("#share-tools").click()
        expect(page.locator("#tools-settings")).to_be_hidden()
        self.assertEqual(page.locator("#tools-table-body tr").count(), 18)
        tools_box = page.locator("#tools-report").bounding_box()
        self.assertLessEqual(tools_box["y"] + tools_box["height"], 1100)
        self.assertFalse(page.locator("#tools-technical").evaluate("(element) => element.open"))
        page.keyboard.press("Escape")
        self.page.evaluate("() => {window.drawnText=[];}")
        tools_text = self.png("#tools-export-image")
        self.assertEqual(self.last_png_name, "tools-tools-fixture.png")
        self.assertLessEqual(self.last_png_dimensions[1], 1200)
        for index in range(9):
            self.assertIn(f"gpt-fixture-{index+1:02}", tools_text)
        self.assertEqual(self.errors, [])

    def test_larger_selection_grows_png_without_dropping_model_rows(self):
        token, tools = nine_model_runs(24)
        self.offline(token, tools)
        self.capture_canvas()
        drawn = self.png("#export-chart-image")
        self.assertGreater(self.last_png_dimensions[1], 900)
        for index in range(24):
            self.assertIn(f"gpt-fixture-{index+1:02}", drawn)
        self.assertEqual(self.page.locator("#chart .chart-row").count(), 24)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
