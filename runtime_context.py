"""Authentication and provenance shared by local and Container Apps runners."""

import json
import os

from azure.identity import AzureCliCredential, ManagedIdentityCredential


def credential(local_factory=AzureCliCredential):
    if os.environ.get("BENCHMARK_EXECUTION_TARGET") == "containerapps":
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError("The Container Apps runner requires its managed identity client ID.")
        return ManagedIdentityCredential(client_id=client_id)
    return local_factory(process_timeout=60)


def execution_context():
    raw = os.environ.get("BENCHMARK_EXECUTION_CONTEXT")
    if raw:
        context = json.loads(raw)
        if not isinstance(context, dict) or context.get("target") != "containerapps":
            raise ValueError("Invalid trusted runner execution context.")
        return context
    return {
        "target": "local", "label": "Local", "region": None, "resource_id": None,
        "authentication": "azure_cli", "parallelism": 1, "cpu": None, "memory": None,
    }


def historical_context(manifest):
    return manifest.get("execution") or {
        "target": "unknown", "label": "Historico (executor/regiao nao registrados)",
        "region": None, "authentication": "unknown",
    }
