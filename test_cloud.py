import gzip
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from azure.core.credentials import AccessToken

import cloud_runner
import cloud_worker
import runtime_context
from azure_io import AzureIO
from server import BenchApp
from test_server import SETTINGS
from deploy_containerapps import infrastructure, names, operator_prefix


class CloudTests(unittest.TestCase):
    def test_authentication_and_historical_provenance_never_infer_runner_region(self):
        with patch.dict(os.environ, {}, clear=True):
            local = MagicMock()
            runtime_context.credential(local)
            local.assert_called_once_with(process_timeout=60)
            self.assertEqual(runtime_context.execution_context()["target"], "local")
            self.assertEqual(runtime_context.historical_context({"location": "brazilsouth"})["target"], "unknown")
        with patch.dict(os.environ, {"BENCHMARK_EXECUTION_TARGET": "containerapps"}, clear=True):
            with self.assertRaises(RuntimeError):
                runtime_context.credential()
            with patch.dict(os.environ, {"AZURE_CLIENT_ID": "worker"}), patch.object(runtime_context, "ManagedIdentityCredential") as managed:
                runtime_context.credential()
                managed.assert_called_once_with(client_id="worker")

    def test_blob_optional_statuses_do_not_hide_auth_or_other_errors(self):
        credential = MagicMock(spec=["get_token"])
        credential.get_token.return_value = AccessToken("not-a-real-token", 9999999999)
        io = AzureIO(credential)
        try:
            for status, kwargs in ((404, {"missing_ok": True}), (412, {"exists_ok": True})):
                with patch.object(io.client, "request", return_value=httpx.Response(status)):
                    self.assertIsNone(io.blob("GET", "https://account.blob.core.windows.net/runs", "item", **kwargs))
            with patch.object(io.client, "request", return_value=httpx.Response(403, text="denied")):
                with self.assertRaisesRegex(RuntimeError, "403"):
                    io.blob("GET", "https://account.blob.core.windows.net/runs", "item", missing_ok=True, exists_ok=True)
        finally:
            io.close()

    def test_snapshot_is_path_allowlisted_and_preserves_partial_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"controls": root, "folder": root / "result"}
            snapshot = {"files": {"run.json": '{"completed":false}', "requests.jsonl": '{"phase":"measure"}\n{"pha',
                                  "runner.log": "progress"}}
            cloud_runner.apply_snapshot(snapshot, job)
            self.assertEqual((job["folder"] / "requests.jsonl").read_text(), snapshot["files"]["requests.jsonl"])
            self.assertEqual((root / "runner.log").read_text(), "progress")
            with self.assertRaises(ValueError):
                cloud_runner.apply_snapshot({"files": {"../escape": "data"}}, job)
            self.assertFalse((root.parent / "escape").exists())

    def test_cloud_bridge_sends_cancellation_and_persists_final_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cancel = root / "cancel"
            cancel.touch()
            job = {"id": "test-run-identifier", "kind": "text", "settings": SETTINGS,
                   "folder": root / "result", "controls": root, "state": "starting", "cancel_path": cancel}
            io = MagicMock()
            io.arm.side_effect = [
                {"value": []},
                {"properties": {"template": {"containers": [{"name": "benchmark", "env": [], "imageType": "ContainerImage",
                    "resources": {"cpu": 1, "memory": "2Gi", "ephemeralStorage": ""}}],
                    "volumes": [], "initContainers": None}}},
                {"name": "execution-123"},
            ]
            io.blob.return_value = httpx.Response(200, headers={"etag": "1"}, content=gzip.compress(json.dumps({
                "files": {"run.json": '{"execution":{"target":"containerapps"}}'}, "finished": True, "exit_code": 2,
            }).encode()))
            lock = threading.Lock()
            apply = cloud_runner.apply_snapshot

            def guarded_snapshot(snapshot, target):
                self.assertTrue(lock.locked(), "Windows file replacement must exclude API file readers.")
                apply(snapshot, target)

            with patch.object(cloud_runner, "AzureIO", return_value=io), patch.object(cloud_runner, "AzureCliCredential"):
                with patch.object(cloud_runner, "apply_snapshot", side_effect=guarded_snapshot):
                    result = cloud_runner.execute({"storage_container": "https://blob.test/runs", "job_id": "/job"}, job, lock)
            self.assertEqual(result, 2)
            self.assertEqual(job["cloud_execution"], "execution-123")
            self.assertEqual(io.put_json.call_args_list[-1].args[1], "runs/test-run-identifier/cancel.json")
            start_template = io.arm.call_args_list[2].kwargs["json"]
            self.assertEqual(set(start_template), {"containers"})
            self.assertNotIn("imageType", start_template["containers"][0])
            self.assertEqual(start_template["containers"][0]["resources"], {"cpu": 1, "memory": "2Gi"})
            self.assertIn({"name": "BENCHMARK_RUN_ID", "value": job["id"]}, start_template["containers"][0]["env"])
            self.assertTrue((job["folder"] / "run.json").exists())
            io.close.assert_called_once()

    def test_existing_execution_resumes_snapshot_without_restarting_or_rebilling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"id": "test-existing-run", "cloud_execution": "existing-azure-execution",
                   "folder": root / "result", "controls": root, "state": "running", "cancel_path": root / "cancel"}
            io = MagicMock()
            io.blob.return_value = httpx.Response(200, headers={"etag": "finished"}, content=gzip.compress(json.dumps({
                "files": {"run.json": '{"completed":true}'}, "finished": True, "exit_code": 0,
            }).encode()))
            with patch.object(cloud_runner, "AzureIO", return_value=io), patch.object(cloud_runner, "AzureCliCredential"):
                self.assertEqual(cloud_runner.execute(
                    {"storage_container": "https://blob.test/runs", "job_id": "/job"}, job, threading.Lock()), 0)
            io.arm.assert_not_called()
            io.put_json.assert_not_called()
            self.assertTrue(json.loads((job["folder"] / "run.json").read_text())["completed"])

    def test_duplicate_worker_claim_never_overwrites_previous_snapshot_or_starts_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            io = MagicMock()
            io.put_json.return_value = None
            with patch.dict(os.environ, {"BENCHMARK_RUN_ID": "duplicate-run-123", "BENCHMARK_STORAGE_CONTAINER": "https://blob.test/runs"}):
                with patch.object(cloud_worker, "credential"), patch.object(cloud_worker, "AzureIO", return_value=io):
                    with patch.object(cloud_worker, "Path", return_value=Path(temporary)), patch.object(cloud_worker.subprocess, "Popen") as process:
                        with self.assertRaisesRegex(RuntimeError, "already claimed"):
                            cloud_worker.main()
            io.blob.assert_not_called()
            process.assert_not_called()

    def test_cloud_selection_is_explicit_and_never_falls_back_to_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = BenchApp(results=Path(temporary))
            app.cloud = None
            with self.assertRaises(ValueError):
                app.start({**SETTINGS, "execution_target": "containerapps"})
            with self.assertRaises(ValueError):
                app.start({**SETTINGS, "execution_target": "https://arbitrary.test"})
            app.cloud = {"job_id": "/trusted-job"}
            with patch.object(app, "_run") as worker:
                app.start({**SETTINGS, "execution_target": "containerapps"})
                app.worker.join()
                self.assertEqual(app.job["execution_target"], "containerapps")
                self.assertNotIn("execution_target", app.job["settings"])
                worker.assert_called_once()
            with patch.object(cloud_runner, "execute", side_effect=httpx.ConnectError("network unavailable")):
                app._run(app.job)
            self.assertEqual(app.job["state"], "failed")
            self.assertIn("network unavailable", app.job["error"])
            self.assertTrue((app.job["controls"] / "job.json").exists())
            app.cloud = {"region": "brazilsouth", "ready": False, "unavailable_reason": "Blob network blocked"}
            self.assertEqual(app.catalog()["default_execution"], "local")
            self.assertFalse(app.catalog()["execution_targets"][1]["available"])
            with self.assertRaisesRegex(ValueError, "Blob network blocked"):
                app.start({**SETTINGS, "execution_target": "containerapps"})

    def test_perimeter_is_enforced_with_narrow_operator_and_unchanged_data_rbac(self):
        config = json.loads((Path(__file__).parent / "benchmark.json").read_text())
        resources = infrastructure(config, "8.8.8.8")["resources"]
        storage = next(r for r in resources if r["type"] == "Microsoft.Storage/storageAccounts")
        self.assertEqual(storage["properties"]["publicNetworkAccess"], "SecuredByPerimeter")
        self.assertEqual(storage["properties"]["networkAcls"]["defaultAction"], "Deny")
        self.assertFalse(storage["properties"]["allowSharedKeyAccess"])
        self.assertFalse(storage["properties"]["allowBlobPublicAccess"])
        association = next(r for r in resources if r["type"].endswith("/resourceAssociations"))
        self.assertEqual(association["properties"]["accessMode"], "Enforced")
        rules = [r for r in resources if r["type"].endswith("/accessRules")]
        self.assertEqual(rules[0]["properties"]["addressPrefixes"], ["8.8.8.8/32"])
        self.assertEqual(rules[1]["properties"]["subscriptions"], [{"id": names(config)["subscription"]}])
        for invalid in ("0.0.0.0/0", "10.0.0.1", "127.0.0.1", "::", "not-an-address"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                operator_prefix(invalid)


if __name__ == "__main__":
    unittest.main()
