"""Verify or deploy Microsoft-Decision-1 in the existing benchmark Foundry account."""

import argparse
import base64
import json
import time
import uuid

import httpx
from azure.identity import AzureCliCredential

from provision import API_VERSION, ROOT

MODEL = {"format": "Microsoft", "name": "Microsoft-Decision-1", "version": "1"}
DEPLOYMENT = "decision-1"
CAPACITY = 100


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--grant-inference", action="store_true",
                        help="Grant only the Decision provider inference action to the signed-in caller at this account.")
    args = parser.parse_args()
    config = json.loads((ROOT / "benchmark.json").read_text(encoding="utf-8"))
    subscription = "/subscriptions/" + config["subscription_id"]
    account = (subscription + "/resourceGroups/" + config["resource_group"]
               + "/providers/Microsoft.CognitiveServices/accounts/" + config["account"])
    location = subscription + "/providers/Microsoft.CognitiveServices/locations/" + config["location"]
    with AzureCliCredential() as credential, httpx.Client(
        base_url="https://management.azure.com",
        headers={"Authorization": "Bearer " + credential.get_token("https://management.azure.com/.default").token},
        timeout=120,
    ) as client:
        def arm(method, path, **kwargs):
            params = {"api-version": "2026-09-01" if "/deployments" in path else API_VERSION,
                      **kwargs.pop("params", {})}
            response = client.request(method, path, params=params, **kwargs)
            if response.is_error:
                raise RuntimeError(f"ARM {response.status_code}: {response.text}")
            return response.json()

        def listing(path, **kwargs):
            data = arm("GET", path, **kwargs)
            result = data["value"]
            while data.get("nextLink"):
                response = client.get(data["nextLink"])
                response.raise_for_status()
                data = response.json()
                result.extend(data["value"])
            return result

        resource = arm("GET", account)
        project = arm("GET", account + "/projects/" + config["project"])
        if resource["location"].lower() != config["location"] or project["properties"]["provisioningState"] != "Succeeded":
            raise ValueError("The specified account region or project is not ready.")
        models = [entry["model"] for entry in listing(location + "/models")
                  if all(entry["model"].get(key) == value for key, value in MODEL.items())]
        skus = [sku for model in models for sku in model["skus"] if sku["name"] == "GlobalStandard"]
        if not skus or models[0].get("capabilities", {}).get("decision") != "true":
            raise ValueError("Decision model/version/SKU unavailable in the requested region.")
        quota = next(item for item in listing(location + "/usages") if item["name"]["value"] == skus[0]["usageName"])
        deployments = listing(account + "/deployments")
        existing = next((item for item in deployments if item["name"] == DEPLOYMENT), None)
        if existing:
            if (existing["properties"]["model"] != MODEL or existing["sku"]["name"] != "GlobalStandard"
                    or existing["sku"]["capacity"] != CAPACITY):
                raise ValueError("Existing Decision deployment differs; will not overwrite.")
        else:
            capacities = listing(location + "/modelCapacities", params={
                "api-version": "2024-10-01", "modelFormat": MODEL["format"],
                "modelName": MODEL["name"], "modelVersion": MODEL["version"],
            })
            available = [item["properties"]["availableCapacity"] for item in capacities
                         if item["properties"]["skuName"] == "GlobalStandard"]
            if quota["limit"] - quota["currentValue"] < CAPACITY or not available or max(available) < CAPACITY:
                raise ValueError("Insufficient subscription quota or regional capacity.")
        print(json.dumps({
            "account": config["account"], "project": config["project"], "region": config["location"],
            "model": MODEL, "sku": "GlobalStandard", "capacity": CAPACITY,
            "priority": "Not supported by the Decision API", "exists": existing is not None,
        }, indent=2))
        if not args.apply:
            print("Read-only preflight completed; use --apply to deploy.")
            return
        path = account + "/deployments/" + DEPLOYMENT
        if not existing:
            arm("PUT", path, json={
                "sku": {"name": "GlobalStandard", "capacity": CAPACITY},
                "properties": {"model": MODEL},
            })
        deadline = time.monotonic() + 300
        while True:
            deployment = arm("GET", path)
            state = deployment["properties"]["provisioningState"]
            if state == "Succeeded":
                break
            if state in {"Failed", "Canceled"} or time.monotonic() >= deadline:
                raise RuntimeError(f"Decision deployment did not complete: {state}")
            time.sleep(3)
        endpoint = resource["properties"]["endpoints"]["Azure AI Model Inference API"]
        inventory = {
            "verified_at_unix": time.time(), "account_id": resource["id"], "project_id": project["id"],
            "location": resource["location"], "deployment": deployment,
            "decision_endpoint": endpoint.rstrip("/") + "/providers/microsoft/v1/systemone",
        }
        if args.grant_inference:
            token_body = credential.get_token("https://management.azure.com/.default").token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(token_body + "=" * (-len(token_body) % 4)))
            role_id = str(uuid.uuid5(uuid.NAMESPACE_URL, account + "/benchmark-decision-inference"))
            role_path = subscription + "/providers/Microsoft.Authorization/roleDefinitions/" + role_id
            actions = [
                "Microsoft.CognitiveServices/accounts/AIServices/providers/action",
            ]
            arm("PUT", role_path, params={"api-version": "2022-04-01"}, json={"properties": {
                "roleName": "Model Latency Benchmark Decision Inference", "type": "CustomRole",
                "description": "Invoke the Microsoft Decision provider; no keys, management, or agent permissions.",
                "assignableScopes": [subscription + "/resourceGroups/" + config["resource_group"]],
                "permissions": [{"actions": [], "notActions": [], "dataActions": actions, "notDataActions": []}],
            }})
            assignment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, account + claims["oid"] + role_id))
            assignment_path = account + "/providers/Microsoft.Authorization/roleAssignments/" + assignment_id
            assignment = arm("PUT", assignment_path, params={"api-version": "2022-04-01"}, json={"properties": {
                "roleDefinitionId": role_path, "principalId": claims["oid"],
            }})
            inventory["inference_role_assignment"] = assignment["id"]
            print("Granted only", actions, "at the benchmark account. RBAC propagation can take several minutes.")
        output = ROOT / "tool-results" / "inventory.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
        print(f"Ready: {DEPLOYMENT}; {inventory['decision_endpoint']}")
        print("Rate limits:", deployment["properties"].get("rateLimits"))


if __name__ == "__main__":
    main()
