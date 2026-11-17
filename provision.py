"""Verify or provision the explicitly named Azure OpenAI benchmark deployments."""

import argparse
import json
import time
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential

API_VERSION = "2025-06-01"
ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "benchmark.json")
    parser.add_argument("--apply", action="store_true", help="Create missing deployments.")
    parser.add_argument("--inventory", type=Path, default=ROOT / "results" / "inventory.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    subscription = f"/subscriptions/{config['subscription_id']}"
    account = (
        f"{subscription}/resourceGroups/{config['resource_group']}"
        f"/providers/Microsoft.CognitiveServices/accounts/{config['account']}"
    )
    location = f"{subscription}/providers/Microsoft.CognitiveServices/locations/{config['location']}"
    credential = AzureCliCredential()
    token = credential.get_token("https://management.azure.com/.default")
    with httpx.Client(
        base_url="https://management.azure.com",
        headers={"Authorization": f"Bearer {token.token}"},
        timeout=120,
    ) as client:

        def arm(method, path, **kwargs):
            version = "2026-09-01" if "/deployments" in path else API_VERSION
            params = {"api-version": version, **kwargs.pop("params", {})}
            response = client.request(method, path, params=params, **kwargs)
            if response.is_error:
                raise RuntimeError(f"ARM {response.status_code}: {response.text}")
            return response.json()

        def listing(path, **kwargs):
            data = arm("GET", path, **kwargs)
            values = data["value"]
            while data.get("nextLink"):
                response = client.get(data["nextLink"])
                response.raise_for_status()
                data = response.json()
                values.extend(data["value"])
            return values

        resource = arm("GET", account)
        project = arm("GET", f"{account}/projects/{config['project']}")
        if resource["location"].lower() != config["location"]:
            raise ValueError("The account region differs from the requested region.")
        if project["properties"]["provisioningState"] != "Succeeded":
            raise ValueError("The named Foundry project is not ready.")
        catalog = listing(f"{location}/models")
        usage = {item["name"]["value"]: item for item in listing(f"{location}/usages")}
        existing = {item["name"]: item for item in listing(f"{account}/deployments")}
        plan = []
        for model in config["models"]:
            matches = [
                item["model"] for item in catalog
                if item["model"]["name"] == model["name"]
                and item["model"]["version"] == model["version"]
                and item["model"]["format"] == "OpenAI"
            ]
            if not matches:
                raise ValueError(f"Exact model/version unavailable: {model}")
            entry = matches[0]
            skus = [
                sku for sku in entry["skus"]
                if sku["name"] == "GlobalStandard" and not sku["usageName"].endswith("-finetune")
            ]
            if not skus or "GlobalStandard" not in entry["capabilities"].get("priorityTierSkus", ""):
                raise ValueError(f"GlobalStandard with Priority unsupported: {model['name']}")
            sku = skus[0]
            capacity = config["capacity"]
            bounds = sku["capacity"]
            if capacity < (bounds.get("minimum") or 1) or capacity > bounds["maximum"]:
                raise ValueError(f"Capacity outside catalog bounds: {model['name']}")
            if bounds.get("step") and capacity % bounds["step"]:
                raise ValueError(f"Invalid capacity step: {model['name']}")
            if bounds.get("allowedValues") and capacity not in bounds["allowedValues"]:
                raise ValueError(f"Capacity not in allowedValues: {model['name']}")
            deployment = existing.get(model["name"])
            if deployment:
                properties = deployment["properties"]
                if (
                    properties["model"]["name"] != model["name"]
                    or properties["model"]["version"] != model["version"]
                    or deployment["sku"]["name"] != "GlobalStandard"
                    or deployment["sku"]["capacity"] != capacity
                    or properties.get("serviceTier") != "Priority"
                    or properties.get("versionUpgradeOption") != "NoAutoUpgrade"
                    or properties.get("provisioningState") != "Succeeded"
                ):
                    raise ValueError(f"Existing deployment differs; will not overwrite: {model['name']}")
            else:
                quota = usage[sku["usageName"]]
                if quota["limit"] - quota["currentValue"] < capacity:
                    raise ValueError(f"Insufficient subscription quota: {model['name']}")
                available = listing(
                    f"{location}/modelCapacities",
                    params={
                        "api-version": "2024-10-01",
                        "modelFormat": "OpenAI",
                        "modelName": model["name"],
                        "modelVersion": model["version"],
                    },
                )
                available = [
                    item["properties"]["availableCapacity"] for item in available
                    if item["properties"]["skuName"] == "GlobalStandard"
                ]
                if not available or max(available) < capacity:
                    raise ValueError(f"Insufficient regional capacity: {model['name']}")
            plan.append({**model, "capacity": capacity, "exists": deployment is not None})
        print(json.dumps({
            "account": config["account"], "project": config["project"],
            "resource_group": config["resource_group"], "location": config["location"],
            "sku": "GlobalStandard", "deployment_tier": "priority",
            "request_tiers": ["default", "priority"], "plan": plan,
        }, indent=2))
        if not args.apply:
            print("Read-only preflight completed. Use --apply to create missing deployments.")
            return
        inventory = []
        for model in plan:
            path = f"{account}/deployments/{model['name']}"
            if not model["exists"]:
                arm("PUT", path, json={
                    "sku": {"name": "GlobalStandard", "capacity": model["capacity"]},
                    "properties": {
                        "model": {"format": "OpenAI", "name": model["name"], "version": model["version"]},
                        "versionUpgradeOption": "NoAutoUpgrade",
                        "raiPolicyName": "Microsoft.DefaultV2",
                        "serviceTier": "Priority",
                    },
                })
            deadline = time.monotonic() + 300
            while True:
                deployment = arm("GET", path)
                state = deployment["properties"]["provisioningState"]
                if state == "Succeeded":
                    break
                if state in {"Failed", "Canceled"} or time.monotonic() > deadline:
                    raise RuntimeError(f"Deployment {model['name']}: {state}")
                time.sleep(5)
            properties = deployment["properties"]
            if properties.get("serviceTier") != "Priority":
                raise RuntimeError(f"Priority setting was not persisted: {model['name']}")
            inventory.append({
                "name": model["name"], "model": properties["model"], "sku": deployment["sku"],
                "state": state, "service_tier": properties["serviceTier"],
                "version_upgrade": properties["versionUpgradeOption"],
                "rate_limits": properties.get("rateLimits"),
            })
            print(f"Ready: {model['name']} {model['version']}")
            args.inventory.parent.mkdir(parents=True, exist_ok=True)
            args.inventory.write_text(json.dumps({
                "verified_at_unix": time.time(), "account_id": account,
                "project_id": project["id"], "location": config["location"],
                "account_dynamic_throttling": resource["properties"].get("dynamicThrottlingEnabled"),
                "deployments": inventory,
            }, indent=2), encoding="utf-8")
    credential.close()


if __name__ == "__main__":
    main()
