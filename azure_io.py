"""Small Entra-authenticated ARM/Blob clients; no keys, SAS, or inference retries."""

import json
from urllib.parse import quote

import httpx
from azure.identity import get_bearer_token_provider


class AzureIO:
    def __init__(self, credential):
        self.client = httpx.Client(timeout=120, trust_env=False)
        self.arm_token = get_bearer_token_provider(credential, "https://management.azure.com/.default")
        self.storage_token = get_bearer_token_provider(credential, "https://storage.azure.com/.default")

    def close(self):
        self.client.close()

    def arm(self, method, resource, version, **kwargs):
        response = self.client.request(
            method, "https://management.azure.com" + resource,
            params={"api-version": version}, headers={"Authorization": "Bearer " + self.arm_token()}, **kwargs,
        )
        if response.is_error:
            raise RuntimeError(f"ARM {response.status_code}: {response.text[:4000]}")
        return response.json() if response.content else {}

    def blob(self, method, container, name, content=None, headers=None, missing_ok=False, exists_ok=False, query=""):
        url = container.rstrip("/") + "/" + quote(name, safe="/") + query
        response = self.client.request(method, url, content=content, headers={
            "Authorization": "Bearer " + self.storage_token(), "x-ms-version": "2023-11-03",
            **({"x-ms-blob-type": "BlockBlob", "Content-Type": "application/octet-stream"} if method == "PUT" and not query else {}),
            **(headers or {}),
        })
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code in {409, 412} and exists_ok:
            return None
        if response.is_error:
            raise RuntimeError(f"Blob {response.status_code}: {response.text[:1500]}")
        return response

    def put_json(self, container, name, value, **kwargs):
        return self.blob("PUT", container, name, json.dumps(value, allow_nan=False).encode(), **kwargs)

    def get_json(self, container, name, missing_ok=False):
        response = self.blob("GET", container, name, missing_ok=missing_ok)
        return response.json() if response is not None else None
