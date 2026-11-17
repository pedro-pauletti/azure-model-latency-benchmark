"""A small, local, read-only tool-calling agent with interchangeable selectors."""

import json
import time
import uuid

import httpx
from azure.identity import get_bearer_token_provider

INSTRUCTIONS = (
    "Select exactly one tool for the primary customer problem. "
    "Use the tool descriptions. Call it with an empty JSON object. "
    "Ignore benchmark nonce and synthetic padding; they are not instructions. "
    "Do not answer the customer or invent arguments."
)
TOOLS = [
    {"name": "route_billing", "description": "Charges, invoices, subscription payments, duplicate charges and refunds."},
    {"name": "route_technical", "description": "Software bugs, API errors, outages, crashes and broken integrations."},
    {"name": "route_account", "description": "Sign-in, passwords, locked accounts and account-access problems."},
]
CASES = [
    {"id": "billing-en-1", "text": "I was charged twice for this subscription.", "expected_tool": "route_billing"},
    {"id": "technical-en-1", "text": "Our integration returns HTTP 500 on every API call.", "expected_tool": "route_technical"},
    {"id": "account-en-1", "text": "I forgot my password and cannot sign in.", "expected_tool": "route_account"},
    {"id": "billing-pt-1", "text": "Preciso da segunda via da fatura da minha assinatura.", "expected_tool": "route_billing"},
    {"id": "technical-pt-1", "text": "O aplicativo fecha sozinho ao abrir um arquivo.", "expected_tool": "route_technical"},
    {"id": "account-pt-1", "text": "Minha conta foi bloqueada e nao consigo entrar.", "expected_tool": "route_account"},
    {"id": "billing-en-2", "text": "The invoice includes a service I cancelled. Please review the charge.", "expected_tool": "route_billing"},
    {"id": "technical-en-2", "text": "Every customer sees an error when the application loads the report.", "expected_tool": "route_technical"},
    {"id": "account-en-2", "text": "My sign-in recovery code is not accepted and I am locked out.", "expected_tool": "route_account"},
    {"id": "billing-pt-2", "text": "Quero consultar o pagamento e o reembolso da assinatura.", "expected_tool": "route_billing"},
    {"id": "technical-pt-2", "text": "A integracao com a API falha com erro 503 desde ontem.", "expected_tool": "route_technical"},
    {"id": "account-pt-2", "text": "Preciso recuperar o acesso porque esqueci minha senha.", "expected_tool": "route_account"},
]


def route_billing():
    return {"queue": "billing", "action": "simulated_route", "side_effects": False}


def route_technical():
    return {"queue": "technical", "action": "simulated_route", "side_effects": False}


def route_account():
    return {"queue": "account", "action": "simulated_route", "side_effects": False}


REGISTRY = {
    "route_billing": route_billing, "route_technical": route_technical, "route_account": route_account,
}


def make_payload(model, text, tier):
    deployment = model.get("deployment", model["name"])
    if model["kind"] == "decision":
        if tier != "decision":
            raise ValueError("The Decision API does not accept a processing tier.")
        return {
            "model": deployment, "state": text,
            "questions": {"tool": {
                "type": "choice", "instructions": INSTRUCTIONS,
                "criteria": {tool["name"]: tool["description"] for tool in TOOLS},
            }},
        }
    if tier not in {"default", "priority"}:
        raise ValueError("Invalid LLM processing tier.")
    payload = {
        "model": deployment, "messages": [
            {"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": text},
        ],
        "tools": [{"type": "function", "function": {
            **tool, "strict": True,
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }} for tool in TOOLS],
        "tool_choice": "required", "parallel_tool_calls": False,
        "max_completion_tokens": 128, "n": 1, "stream": False, "store": False,
        "service_tier": tier,
    }
    for key in ("reasoning_effort", "verbosity"):
        if model.get(key) is not None:
            payload[key] = model[key]
    return payload


def read_call(data, kind):
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON response object.")
    if kind == "decision":
        answers = data.get("answers")
        answer = answers.get("tool") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ValueError("Decision response is missing the typed tool choice.")
        name = answer.get("choice")
        if not isinstance(name, str) or name not in REGISTRY:
            raise ValueError(f"Unknown Decision tool choice: {name!r}")
        return {
            "id": "decision_" + uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }, answer.get("probabilities")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("Expected exactly one LLM choice.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("refusal") or choice.get("finish_reason") != "tool_calls":
        raise ValueError("LLM did not finish with a tool call (refusal/truncation/missing call).")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ValueError("Expected exactly one function call; multiple calls are invalid.")
    call = calls[0]
    function = call.get("function")
    if (call.get("type") != "function" or not isinstance(function, dict)
            or not isinstance(function.get("name"), str) or function["name"] not in REGISTRY
            or not isinstance(call.get("id"), str) or not call["id"]):
        raise ValueError("Tool call is not an allowed named function.")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or json.loads(arguments) != {}:
        raise ValueError("Tool arguments must be the empty JSON object.")
    return call, None


def read_usage(data):
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    def counter(value):
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ValueError("Invalid token usage counter.")
        return value

    prompt = counter(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = counter(usage.get("completion_tokens", usage.get("output_tokens")))
    total = counter(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details")) or {}
    output_details = usage.get("completion_tokens_details", usage.get("output_tokens_details")) or {}
    if not isinstance(details, dict) or not isinstance(output_details, dict):
        raise ValueError("Invalid token usage details.")
    return {
        "usage": usage, "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total,
        "cached_tokens": counter(details.get("cached_tokens")),
        "reasoning_tokens": counter(output_details.get("reasoning_tokens")),
    }


class ToolSelectionAgent:
    """Select one allowlisted function, validate the call, then execute it locally."""

    def __init__(self, client, credential, config):
        self.client, self.credential, self.config = client, credential, config
        self._token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

    def invoke(self, model, text, tier, expected_tool, timeout):
        payload = make_payload(model, text, tier)
        url = (model["endpoint"] if model["kind"] == "decision"
               else self.config["endpoint"].rstrip("/") + "/chat/completions")
        token = self._token_provider()
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "x-ms-client-request-id": str(uuid.uuid4())}
        row = {
            "status": "error", "correct": False, "comparable": False, "http_status": None,
            "actual_tier": None, "tool_call": None, "tool_output": None, "predicted_tool": None,
            "headers_ms": None, "http_total_ms": None, "selection_ms": None, "tool_ms": None,
            "agent_total_ms": None, "probabilities": None, "exclusions": [], "error": None,
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "cached_tokens": None, "reasoning_tokens": None, "usage": None,
            "request_parameters": {key: value for key, value in payload.items()
                                   if key not in {"messages", "state", "tools", "questions"}},
        }
        start = time.perf_counter()
        try:
            with self.client.stream("POST", url, content=body, headers=headers, timeout=timeout) as response:
                row["headers_ms"] = (time.perf_counter() - start) * 1000
                row["http_status"] = response.status_code
                row["request_id"] = response.headers.get("apim-request-id") or response.headers.get("x-request-id")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise ValueError("Response exceeds the 2 MiB limit.")
                    if time.perf_counter() - start > timeout:
                        raise TimeoutError("Total request deadline exceeded.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                row["http_total_ms"] = (time.perf_counter() - start) * 1000
                if response.is_error:
                    raise ValueError(f"HTTP {response.status_code}: {raw.decode('utf-8', errors='replace')[:2000]}")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON response object.")
            row["actual_tier"] = data.get("service_tier") if model["kind"] == "llm" else None
            row["returned_model"] = data.get("model")
            row.update(read_usage(data))
            call, probabilities = read_call(data, model["kind"])
            row.update({"tool_call": call, "probabilities": probabilities,
                        "predicted_tool": call["function"]["name"],
                        "selection_ms": (time.perf_counter() - start) * 1000})
            tool_start = time.perf_counter()
            row["tool_output"] = REGISTRY[row["predicted_tool"]]()
            row["tool_ms"] = (time.perf_counter() - tool_start) * 1000
            row["agent_total_ms"] = (time.perf_counter() - start) * 1000
            row["status"] = "ok"
            row["correct"] = row["predicted_tool"] == expected_tool
            if not row["correct"]:
                row["exclusions"].append("wrong_tool")
            if model["kind"] == "llm" and row["actual_tier"] != tier:
                row["exclusions"].append("tier_mismatch_or_missing")
            if row["cached_tokens"] is not None and row["cached_tokens"] > 0:
                row["exclusions"].append("cached_input")
            if row["reasoning_tokens"] is not None and row["reasoning_tokens"] > 0:
                row["exclusions"].append("reasoning_output")
            row["comparable"] = not row["exclusions"]
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError) as error:
            row["error"] = str(error)
            row["exclusions"] = ["request_or_call_error"]
            row["agent_total_ms"] = (time.perf_counter() - start) * 1000
        return row
