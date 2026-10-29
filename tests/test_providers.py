import asyncio
import hashlib
import json
import socket
import ssl

import httpx
import pytest

from codereview.budgets import Budget, DeadlineExceeded
from codereview.config import ModelPolicy, Settings
from codereview.models import Finding
from codereview.providers import OpenRouterClient, ProviderConfigurationError, ProviderError

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def configured() -> Settings:
    return Settings(
        models=ModelPolicy(
            coordinator="test/coordinator",
            worker="test/worker",
            allowed_models=["test/coordinator", "test/worker"],
            allowed_providers=["trusted-provider"],
        )
    )


def key_data(**overrides):
    data = {
        "limit": 100,
        "limit_reset": "monthly",
        "limit_remaining": 95,
        "include_byok_in_limit": True,
        "usage_monthly": 5,
        "is_management_key": False,
    }
    return {"data": data | overrides}


def catalog():
    return {
        "data": [
            {
                "id": model,
                "context_length": 100000,
                "supported_parameters": ["tools", "structured_outputs", "response_format"],
                "pricing": {"prompt": "0.000001", "completion": "0.000002", "request": "0.01"},
            }
            for model in ["test/coordinator", "test/worker"]
        ]
    }


async def test_monthly_key_must_enforce_approved_cap_before_any_generation():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data(limit=101))
        return httpx.Response(200, json=catalog())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderConfigurationError, match="monthly"):
            await client.complete(
                role="worker", messages=[{"role": "user", "content": "Hi"}], schema=SCHEMA
            )
    assert not any(path.endswith("/chat/completions") for path in calls)


async def test_worker_is_routed_only_to_approved_models_and_providers_and_billed_once():
    dispatched = []
    instructions = [{"role": "system", "content": "Private review instructions"}]

    def respond(request):
        if request.url.path.endswith("/key"):
            assert request.headers["Authorization"] == "Bearer coord"
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        dispatched.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer worker"
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "test/worker",
                "choices": [
                    {"message": {"content": '{"answer":"supported"}'}, "finish_reason": "stop"}
                ],
                "usage": {"cost": 0.03, "cost_details": {"upstream_inference_cost": 0.03}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        result = await client.complete(
            role="worker",
            messages=instructions + [{"role": "user", "content": "Private source"}],
            schema=SCHEMA,
        )
    assert result == {"answer": "supported"}
    assert client.usage().worker_cost == 0.03
    assert client.usage().coordinator_cost == 0
    assert client.usage().worker_reserved == 0
    assert client.usage().calls == 1
    payload = dispatched[0]
    assert payload["model"] == "test/worker"
    assert payload["provider"]["only"] == ["trusted-provider"]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["provider"]["require_parameters"] is True
    assert payload["provider"]["max_price"] == {"prompt": 1, "completion": 2, "request": 0.01}
    assert payload["response_format"]["json_schema"]["strict"] is True
    record = client.call_records[0]
    assert (
        record["system_prompt_sha256"]
        == hashlib.sha256(
            json.dumps(
                instructions, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )
    assert (
        record["request_sha256"]
        == hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    assert record["attempt"] == 1
    assert "Private review instructions" not in json.dumps(client.call_records)
    assert "Private source" not in json.dumps(client.call_records)
    assert "Bearer" not in json.dumps(client.call_records)


@pytest.mark.parametrize("recoverable", [True, False])
async def test_missing_usage_is_reconciled_or_reservation_stays_held(recoverable):
    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        if request.url.path.endswith("/generation"):
            assert request.url.params["id"] == "gen-missing"
            return (
                httpx.Response(200, json={"data": {"total_cost": 0.02}})
                if recoverable
                else httpx.Response(404)
            )
        return httpx.Response(
            200,
            json={
                "id": "gen-missing",
                "model": "test/worker",
                "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        if recoverable:
            assert await client.complete(role="worker", messages=[], schema=SCHEMA) == {
                "answer": "ok"
            }
            assert client.usage().worker_cost == 0.02
            assert client.usage().worker_reserved == 0
        else:
            with pytest.raises(ProviderError, match="usage"):
                await client.complete(role="worker", messages=[], schema=SCHEMA)
            assert client.usage().worker_reserved > 0
            assert client.usage().worker_cost == 0
    assert client.usage().calls == 1


async def test_retries_have_separate_reservations_and_settle_reported_cost():
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "rate limited"}, "usage": {"cost": 0.005}},
                headers={"retry-after": "0"},
            )
        return httpx.Response(
            200,
            json={
                "id": "gen-2",
                "model": "test/worker",
                "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
                "usage": {"cost": 0.02},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        assert await client.complete(role="worker", messages=[], schema=SCHEMA) == {"answer": "ok"}
    assert attempts == 2
    assert client.usage().calls == 2
    assert client.usage().worker_cost == 0.025
    assert client.usage().worker_reserved == 0
    first, second = client.call_records
    assert [first["attempt"], second["attempt"]] == [1, 2]
    assert first["request_sha256"] == second["request_sha256"]
    assert first["system_prompt_sha256"] == second["system_prompt_sha256"]
    assert first["reservation"] != second["reservation"]


async def test_inflight_deadline_stops_waiting_but_does_not_release_the_charge():
    async def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        await asyncio.sleep(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(),
            coordinator_key="coord",
            worker_key="worker",
            http_client=http,
            budget=Budget(2, 0.02),
        )
        with pytest.raises(DeadlineExceeded):
            await client.complete(role="worker", messages=[], schema=SCHEMA)
        assert client.usage().worker_reserved > 0
        assert client.call_records[0]["status"] == "usage_unknown"


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": '{"answer":42}'}, "finish_reason": "stop"}]},
        {
            "model": "unapproved/model",
            "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
        },
        {"choices": [{"message": {"content": '{"answer":"partial"}'}, "finish_reason": "length"}]},
    ],
)
async def test_rejects_invalid_truncated_or_unexpected_model_output_but_records_its_cost(payload):
    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        return httpx.Response(200, json={"model": "test/worker", "usage": {"cost": 0.02}} | payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError):
            await client.complete(role="worker", messages=[], schema=SCHEMA)
        assert client.usage().worker_cost == 0.02
        assert client.usage().worker_reserved == 0


async def test_normalizes_nested_pydantic_defaults_for_strict_output_without_mutating_schema():
    schema = Finding.model_json_schema()
    original = json.dumps(schema, sort_keys=True)
    result = Finding(
        category="maintainability",
        title="Supported",
        observation="Observed",
        rationale="Cost",
        alternative="Alternative",
        evidence=[{"path": "a.py", "start_line": 1, "end_line": 1, "quote": "x"}],
    ).model_dump()
    sent = []

    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        sent.append(json.loads(request.content)["response_format"]["json_schema"]["schema"])
        return httpx.Response(
            200,
            json={
                "model": "test/worker",
                "usage": {"cost": 0.02},
                "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        assert await client.complete(role="worker", messages=[], schema=schema) == result
    assert json.dumps(schema, sort_keys=True) == original
    assert set(sent[0]["required"]) == set(sent[0]["properties"])
    assert "default" not in sent[0]["properties"]["severity"]
    assert sent[0]["$defs"]["Evidence"]["additionalProperties"] is False
    assert set(sent[0]["$defs"]["Evidence"]["required"]) == set(
        sent[0]["$defs"]["Evidence"]["properties"]
    )


@pytest.mark.parametrize(
    "response", [httpx.Response(401, text="private body"), httpx.Response(200, json={"data": []})]
)
async def test_invalid_preflight_is_sanitized(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.complete(role="worker", messages=[], schema=SCHEMA)
    assert "private body" not in str(error.value)
    assert client.usage().calls == 0


async def test_retry_with_unknown_usage_keeps_first_attempt_reserved():
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "0"}
            )
        return httpx.Response(
            200,
            json={
                "model": "test/worker",
                "usage": {"cost": 0.02},
                "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        assert await client.complete(role="worker", messages=[], schema=SCHEMA) == {"answer": "ok"}
    assert client.usage().calls == 2
    assert client.usage().worker_cost == 0.02
    assert client.usage().worker_reserved == client.call_records[0]["estimated_cost"]


@pytest.mark.parametrize("endpoint", ["/key", "/models"])
async def test_preflight_transport_failure_identifies_endpoint_without_claiming_a_charge(endpoint):
    dispatched = []

    def respond(request):
        dispatched.append(request.url.path)
        if request.url.path.endswith(endpoint):
            raise httpx.ConnectError(
                "private-proxy-password private-coordinator-key source contents", request=request
            )
        return httpx.Response(200, json=key_data())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.complete(role="coordinator", messages=[], schema=SCHEMA)
    message = str(error.value)
    assert f"GET {endpoint}" in message
    assert "ConnectError" in message
    assert "private-" not in message
    assert "source contents" not in message
    assert "reserved" not in message.lower()
    assert not any(path.endswith("/chat/completions") for path in dispatched)
    assert client.usage().calls == 0
    assert client.usage().coordinator_reserved == 0
    assert client.call_records == []


async def test_generation_transport_failure_keeps_charge_and_reports_safe_diagnostic():
    generations = 0

    def respond(request):
        nonlocal generations
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        generations += 1
        raise httpx.ReadError("sensitive-source-and-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.complete(role="coordinator", messages=[], schema=SCHEMA)
    message = str(error.value)
    assert "POST /chat/completions" in message
    assert "ReadError" in message
    assert "sensitive-source-and-key" not in message
    assert "reserved" in message.lower()
    assert generations == 1  # A broken connection must not replay an uncertain paid request.
    assert client.usage().calls == 1
    assert client.usage().coordinator_reserved > 0
    assert client.call_records[0]["status"] == "usage_unknown"


async def test_proxy_error_preserves_category_without_printing_proxy_credentials():
    def respond(request):
        raise httpx.ProxyError("http://proxy-user:proxy-secret@private.proxy:8080", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.preflight()
    message = str(error.value)
    assert "GET /key" in message
    assert "ProxyError" in message
    assert "proxy-secret" not in message
    assert "private.proxy" not in message
    assert client.usage().calls == 0


@pytest.mark.parametrize(
    ("cause_type", "expected_hint"),
    [
        (ssl.SSLCertVerificationError, "TLS certificate verification failed"),
        (socket.gaierror, "DNS resolution failed"),
    ],
    ids=["wrapped-certificate-verification", "wrapped-dns-resolution"],
)
async def test_nested_connection_causes_produce_safe_specific_hints(cause_type, expected_hint):
    def respond(request):
        try:
            raise cause_type(1, "private-root-cause http://user:password@internal.proxy")
        except OSError as original:
            try:
                raise OSError("private-wrapper-detail coordinator-secret") from original
            except OSError as wrapper:
                raise httpx.ConnectError("private-top-level-detail", request=request) from wrapper

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.preflight()
    message = str(error.value)
    assert "GET /key" in message
    assert "ConnectError" in message
    assert expected_hint in message
    for sensitive in ("private-", "password", "internal.proxy", "coordinator-secret"):
        assert sensitive not in message
    assert "reserved" not in message.lower()
    assert client.usage().calls == 0


async def test_online_preflight_verifies_both_keys_and_models_without_generation():
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path, request.headers["Authorization"]))
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json=catalog())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        result = await client.preflight()
    assert result["status"] == "ok"
    assert result["coordinator_key_policy"] == "passed"
    assert result["worker_key"] == "passed"
    assert result["models"] == {"coordinator": "test/coordinator", "worker": "test/worker"}
    assert result["note"]
    assert all(method == "GET" for method, _, _ in requests)
    assert {key for _, path, key in requests if path.endswith("/key")} == {
        "Bearer coord",
        "Bearer worker",
    }
    assert client.usage().calls == 0
    assert client.usage().worker_reserved == client.usage().coordinator_reserved == 0
    assert client.call_records == []


async def test_online_preflight_rejects_worker_authentication_failure():
    requests = []

    def respond(request):
        requests.append(request.method)
        if request.headers["Authorization"] == "Bearer worker":
            return httpx.Response(401, text="private-worker-key")
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        return httpx.Response(200, json=catalog())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderError) as error:
            await client.preflight()
    assert "401" in str(error.value)
    assert "private-worker-key" not in str(error.value)
    assert set(requests) == {"GET"}
    assert client.usage().calls == 0


@pytest.mark.parametrize("missing_model", ["test/coordinator", "test/worker"])
async def test_online_preflight_rejects_model_not_in_catalog_with_exact_id_hint(missing_model):
    def respond(request):
        assert request.method == "GET"
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        available = catalog()
        available["data"] = [item for item in available["data"] if item["id"] != missing_model]
        return httpx.Response(200, json=available)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises(ProviderConfigurationError) as error:
            await client.preflight()
    assert "catalog" in str(error.value).lower()
    assert "exact" in str(error.value).lower()
    assert client.usage().calls == 0


@pytest.mark.parametrize(
    "invalid_entry",
    [
        {"supported_parameters": ["tools"]},
        {"pricing": {"prompt": "invalid", "completion": "0.01"}},
        {"context_length": 0},
    ],
    ids=["structured-output-unavailable", "invalid-pricing", "invalid-context-window"],
)
async def test_online_preflight_validates_worker_capabilities_and_pricing(invalid_entry):
    def respond(request):
        assert request.method == "GET"
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=key_data())
        available = catalog()
        available["data"][1].update(invalid_entry)
        return httpx.Response(200, json=available)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            configured(), coordinator_key="coord", worker_key="worker", http_client=http
        )
        with pytest.raises((ProviderConfigurationError, ProviderError)):
            await client.preflight()
    assert client.usage().calls == 0
