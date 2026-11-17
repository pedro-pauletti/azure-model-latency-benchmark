"""Validate/provision a private Container Apps Job for the benchmark (no public ingress)."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import time
import uuid

from azure.identity import AzureCliCredential

from azure_io import AzureIO

from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_VERSION = "2025-01-01"
TAGS = {"workload": "model-latency-benchmark", "managed-by": "benchmark-deployer"}
NETWORK_VERSION = "2024-07-01"
STORAGE_VERSION = "2025-06-01"


def names(config):
    subscription = "/subscriptions/" + config["subscription_id"]
    group = subscription + "/resourceGroups/" + config["resource_group"]
    suffix = hashlib.sha256(group.lower().encode()).hexdigest()[:10]
    return {
        "subscription": subscription, "group": group,
        "registry": "acrlatbench" + suffix, "storage": "stlatbench" + suffix,
        "identity": group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-latency-benchmark",
        "environment": group + "/providers/Microsoft.App/managedEnvironments/cae-latency-benchmark",
        "job": group + "/providers/Microsoft.App/jobs/caj-latency-benchmark",
        "account": group + "/providers/Microsoft.CognitiveServices/accounts/" + config["account"],
        "perimeter": group + "/providers/Microsoft.Network/networkSecurityPerimeters/nsp-latency-benchmark",
    }


def operator_prefix(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global:
        raise ValueError("The operator rule requires one public egress IP address.")
    return str(ip) + ("/32" if ip.version == 4 else "/128")


def perimeter_resources(config, operator_ip):
    n = names(config)
    perimeter, profile = n["perimeter"], n["perimeter"] + "/profiles/benchmark"
    storage = n["group"] + "/providers/Microsoft.Storage/storageAccounts/" + n["storage"]
    return [
        {"type": "Microsoft.Network/networkSecurityPerimeters", "apiVersion": NETWORK_VERSION,
         "name": "nsp-latency-benchmark", "location": config["location"], "tags": TAGS, "properties": {}},
        {"type": "Microsoft.Network/networkSecurityPerimeters/profiles", "apiVersion": NETWORK_VERSION,
         "name": "nsp-latency-benchmark/benchmark", "dependsOn": [perimeter], "properties": {}},
        {"type": "Microsoft.Network/networkSecurityPerimeters/profiles/accessRules", "apiVersion": NETWORK_VERSION,
         "name": "nsp-latency-benchmark/benchmark/operator", "dependsOn": [profile],
         "properties": {"direction": "Inbound", "addressPrefixes": [operator_prefix(operator_ip)]}},
        {"type": "Microsoft.Network/networkSecurityPerimeters/profiles/accessRules", "apiVersion": NETWORK_VERSION,
         "name": "nsp-latency-benchmark/benchmark/azure-worker", "dependsOn": [profile],
         "properties": {"direction": "Inbound", "subscriptions": [{"id": n["subscription"]}]}},
        {"type": "Microsoft.Network/networkSecurityPerimeters/resourceAssociations", "apiVersion": NETWORK_VERSION,
         "name": "nsp-latency-benchmark/benchmark-storage", "dependsOn": [
             storage, profile, profile + "/accessRules/operator", profile + "/accessRules/azure-worker"],
         "properties": {"accessMode": "Enforced", "privateLinkResource": {"id": storage}, "profile": {"id": profile}}},
    ]


def infrastructure(config, operator_ip):
    n = names(config)
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "resources": [
            {"type": "Microsoft.ManagedIdentity/userAssignedIdentities", "apiVersion": "2023-01-31",
             "name": "id-latency-benchmark", "location": config["location"], "tags": TAGS},
            {"type": "Microsoft.ContainerRegistry/registries", "apiVersion": "2023-07-01",
             "name": n["registry"], "location": config["location"], "tags": TAGS,
             "sku": {"name": "Basic"}, "properties": {"adminUserEnabled": False, "anonymousPullEnabled": False}},
            {"type": "Microsoft.Storage/storageAccounts", "apiVersion": STORAGE_VERSION,
             "name": n["storage"], "location": config["location"], "tags": TAGS,
             "kind": "StorageV2", "sku": {"name": "Standard_LRS"}, "properties": {
                 "minimumTlsVersion": "TLS1_2", "supportsHttpsTrafficOnly": True,
                 "allowBlobPublicAccess": False, "allowSharedKeyAccess": False,
                 "defaultToOAuthAuthentication": True, "publicNetworkAccess": "SecuredByPerimeter",
                 "networkAcls": {"defaultAction": "Deny", "bypass": "None"},
             }},
            {"type": "Microsoft.Storage/storageAccounts/blobServices/containers", "apiVersion": "2023-05-01",
             "name": n["storage"] + "/default/benchmark-runs",
             "dependsOn": [n["group"] + "/providers/Microsoft.Storage/storageAccounts/" + n["storage"]],
             "properties": {"publicAccess": "None"}},
            {"type": "Microsoft.App/managedEnvironments", "apiVersion": APP_VERSION,
             "name": "cae-latency-benchmark", "location": config["location"], "tags": TAGS,
             "properties": {
                 "appLogsConfiguration": {"destination": ""},
                 "workloadProfiles": [{"name": "Consumption", "workloadProfileType": "Consumption"}],
                 "zoneRedundant": False,
             }},
        ] + perimeter_resources(config, operator_ip),
    }


def wait_resource(io, resource, version, timeout=900):
    deadline = time.monotonic() + timeout
    while True:
        result = io.arm("GET", resource, version)
        state = result.get("properties", {}).get("provisioningState", "Succeeded")
        if state.lower() == "succeeded":
            return result
        if state.lower() in {"failed", "canceled"} or time.monotonic() > deadline:
            raise RuntimeError(f"Provisioning did not succeed: {resource}: {state}")
        time.sleep(5)


def assign(io, subscription, scope, principal, role_id):
    role_path = subscription + "/providers/Microsoft.Authorization/roleDefinitions/" + role_id
    assignment = str(uuid.uuid5(uuid.NAMESPACE_URL, scope.lower() + principal + role_id))
    return io.arm("PUT", scope + "/providers/Microsoft.Authorization/roleAssignments/" + assignment, "2022-04-01",
                  json={"properties": {"roleDefinitionId": role_path, "principalId": principal}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Deploy infrastructure after ARM validation.")
    parser.add_argument("--build", action="store_true", help="Build and deploy the current image through ACR Tasks.")
    parser.add_argument("--operator-ip", required=True, help="Single current public egress IP for the enforced perimeter.")
    args = parser.parse_args()
    if args.build and not args.apply:
        parser.error("--build requires --apply.")
    config = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
    n = names(config)
    template = infrastructure(config, args.operator_ip)
    artifact = ROOT / "infra" / "containerapps.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(template, indent=2), encoding="utf-8")
    print(json.dumps({"subscription": config["subscription_id"], "resource_group": config["resource_group"],
                      "location": config["location"], "job": n["job"],
                      "cpu": 1, "memory": "2Gi", "parallelism": 1, "replica_retry_limit": 0,
                      "ingress": "none", "secrets": "none; managed identity + private Blob",
                      "apply": args.apply, "build": args.build}, indent=2), flush=True)
    with AzureCliCredential(process_timeout=60) as credential:
        io = AzureIO(credential)
        try:
            for provider in ("Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.Storage", "Microsoft.ManagedIdentity", "Microsoft.Network"):
                info = io.arm("GET", n["subscription"] + "/providers/" + provider, "2021-04-01")
                if info["registrationState"] != "Registered":
                    raise RuntimeError(f"Provider must be registered: {provider}")
            deployment_path = n["group"] + "/providers/Microsoft.Resources/deployments/latency-containerapps"
            body = {"properties": {"mode": "Incremental", "template": template}}
            result = io.arm("POST", deployment_path + "/validate", "2022-09-01", json=body)
            if result.get("error"):
                raise RuntimeError(json.dumps(result["error"]))
            print("ARM template validation passed.", flush=True)
            if not args.apply:
                return
            io.arm("PUT", deployment_path, "2022-09-01", json=body)
            wait_resource(io, deployment_path, "2022-09-01")
            identity = io.arm("GET", n["identity"], "2023-01-31")["properties"]
            principal = identity["principalId"]
            registry_id = n["group"] + "/providers/Microsoft.ContainerRegistry/registries/" + n["registry"]
            storage_id = n["group"] + "/providers/Microsoft.Storage/storageAccounts/" + n["storage"]
            container_id = storage_id + "/blobServices/default/containers/benchmark-runs"
            storage_url = f"https://{n['storage']}.blob.core.windows.net/benchmark-runs"
            claims = credential.get_token("https://management.azure.com/.default").token.split(".")[1]
            caller = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))["oid"]
            decision_role = str(uuid.uuid5(uuid.NAMESPACE_URL, n["account"] + "/benchmark-decision-inference"))
            for scope, who, role in (
                (registry_id, principal, "7f951dda-4ed3-4680-a7ca-43fe172d538d"),
                (container_id, principal, "ba92f5b4-2d11-453d-a403-e96b0029c9fe"),
                (container_id, caller, "ba92f5b4-2d11-453d-a403-e96b0029c9fe"),
                (n["account"], principal, "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"),
                (n["account"], principal, decision_role),
            ):
                assign(io, n["subscription"], scope, who, role)
            print("Scoped identity/registry/blob/inference roles persisted.", flush=True)
            storage = io.arm("GET", storage_id, STORAGE_VERSION)
            association = io.arm("GET", n["perimeter"] + "/resourceAssociations/benchmark-storage", NETWORK_VERSION)
            if (storage["properties"].get("publicNetworkAccess") != "SecuredByPerimeter"
                    or association["properties"]["accessMode"] != "Enforced"):
                raise RuntimeError("Storage must be secured by its Enforced network perimeter; refusing open access.")
            probe = "health/" + str(uuid.uuid4()) + ".json"
            deadline = time.monotonic() + 180
            while True:
                try:
                    io.put_json(storage_url, probe, {"transport": "entra", "perimeter": "Enforced"})
                    if io.get_json(storage_url, probe) != {"transport": "entra", "perimeter": "Enforced"}:
                        raise RuntimeError("Blob transport round-trip mismatch.")
                    break
                except RuntimeError as error:
                    if "Blob 403:" not in str(error) or time.monotonic() >= deadline:
                        raise
                    print("Waiting for perimeter/RBAC propagation (control probe only, no inference).", flush=True)
                    time.sleep(10)
            print("Enforced perimeter and authenticated Blob round-trip verified.", flush=True)
            if not args.build:
                existing = ROOT / "containerapps.json"
                if existing.exists():
                    recorded = json.loads(existing.read_text(encoding="utf-8"))
                    recorded.update(ready=True, perimeter_id=n["perimeter"])
                    recorded.pop("unavailable_reason", None)
                    existing.write_text(json.dumps(recorded, indent=2), encoding="utf-8")
                print("Infrastructure and transport ready. Use --apply --build to update the runner image.")
                return
            source_hash = hashlib.sha256()
            for path in sorted(ROOT.iterdir()):
                if path.suffix in {".py", ".json", ".txt", ".html"} or path.name == "Dockerfile":
                    if path.name != "containerapps.json" and not path.name.startswith("test_"):
                        source_hash.update(path.name.encode())
                        source_hash.update(path.read_bytes())
            tag = source_hash.hexdigest()[:16]
            repository = "latency-benchmark"
            az = shutil.which("az")
            if not az:
                raise RuntimeError("Azure CLI is required for the remote ACR build.")
            subprocess.run([az, "acr", "build", "--registry", n["registry"],
                            "--subscription", config["subscription_id"], "--image", f"{repository}:{tag}",
                            "--file", str(ROOT / "Dockerfile"), "--no-logs", str(ROOT)],
                           check=True, env={**os.environ, "PYTHONUTF8": "1"})
            digest = subprocess.run([az, "acr", "repository", "show", "--name", n["registry"],
                                     "--image", f"{repository}:{tag}", "--query", "digest", "-o", "tsv"],
                                    check=True, capture_output=True, text=True).stdout.strip()
            if not digest.startswith("sha256:") or len(digest) != 71:
                raise RuntimeError("ACR did not return a valid immutable image digest.")
            image = f"{n['registry']}.azurecr.io/{repository}@{digest}"
            job = {
                "location": config["location"], "tags": TAGS,
                "identity": {"type": "UserAssigned", "userAssignedIdentities": {n["identity"]: {}}},
                "properties": {
                    "environmentId": n["environment"], "workloadProfileName": "Consumption",
                    "configuration": {
                        "triggerType": "Manual", "replicaTimeout": 86400, "replicaRetryLimit": 0,
                        "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                        "registries": [{"server": n["registry"] + ".azurecr.io", "identity": n["identity"]}],
                    },
                    "template": {"containers": [{
                        "name": "benchmark", "image": image, "resources": {"cpu": 1, "memory": "2Gi"},
                        "env": [{"name": key, "value": value} for key, value in {
                            "AZURE_CLIENT_ID": identity["clientId"],
                            "BENCHMARK_EXECUTION_TARGET": "containerapps",
                            "BENCHMARK_STORAGE_CONTAINER": storage_url,
                            "BENCHMARK_REGION": config["location"], "BENCHMARK_JOB_ID": n["job"],
                            "BENCHMARK_IMAGE": image,
                        }.items()],
                    }]},
                },
            }
            # Validate the actual job through ARM before applying it.
            job_template = {**template, "resources": [{
                "type": "Microsoft.App/jobs", "apiVersion": APP_VERSION,
                "name": "caj-latency-benchmark", **job,
            }]}
            validation = io.arm("POST", deployment_path + "/validate", "2022-09-01",
                                json={"properties": {"mode": "Incremental", "template": job_template}})
            if validation.get("error"):
                raise RuntimeError(json.dumps(validation["error"]))
            io.arm("PUT", n["job"], APP_VERSION, json=job)
            deployed = wait_resource(io, n["job"], APP_VERSION)
            if deployed["properties"]["template"]["containers"][0]["image"] != image:
                raise RuntimeError("The deployed image differs from the immutable build.")
            output = {
                "job_id": n["job"], "storage_container": storage_url, "region": config["location"],
                "image": image, "identity_id": n["identity"], "client_id": identity["clientId"],
                "cpu": 1, "memory": "2Gi", "parallelism": 1, "replica_retry_limit": 0,
                "ready": True,
                "perimeter_id": n["perimeter"],
            }
            (ROOT / "containerapps.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
            print("Private Container Apps Job ready:", n["job"], flush=True)
        finally:
            io.close()


if __name__ == "__main__":
    main()
