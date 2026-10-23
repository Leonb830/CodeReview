"""OpenRouter transport with explicit model policy and spend admission."""

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from jsonschema import Draft202012Validator, SchemaError, ValidationError

from codereview.budgets import Budget, BudgetExceeded, DeadlineExceeded
from codereview.config import Settings
from codereview.models import Usage

BASE_URL = "https://openrouter.ai/api/v1"


class ProviderConfigurationError(ValueError):
    """A credential or routing policy cannot enforce the configured contract."""


class ProviderError(RuntimeError):
    """A provider operation failed without exposing source or credentials."""


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        operation: Literal["review", "audit", "fix"] = "review",
        coordinator_key: str | None = None,
        worker_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.settings = settings
        self.coordinator_key = coordinator_key or os.environ.get("OPENROUTER_COORDINATOR_KEY", "")
        self.worker_key = worker_key or os.environ.get("OPENROUTER_WORKER_KEY", "")
        if not self.coordinator_key or not self.worker_key:
            raise ProviderConfigurationError(
                "Set separate OPENROUTER_COORDINATOR_KEY and OPENROUTER_WORKER_KEY credentials"
            )
        if self.coordinator_key == self.worker_key:
            raise ProviderConfigurationError("Coordinator and worker API keys must differ")
        limit = settings.limit(operation)
        self.budget = budget or Budget(
            limit.dollars, limit.seconds, settings.models.coordinator_monthly_dollars
        )
        self._catalog: dict[str, dict[str, Any]] = {}
        self._catalog_lock = asyncio.Lock()
        self.call_records: list[dict[str, Any]] = []
        policy = settings.models
        if not policy.allowed_providers or not policy.allowed_models:
            raise ProviderConfigurationError("Configure non-empty model and provider allowlists")
        if any(model not in policy.allowed_models for model in (policy.coordinator, policy.worker)):
            raise ProviderConfigurationError("Both configured models must appear in allowed_models")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(follow_redirects=False)

    async def _check_key(self) -> None:
        response = await self._request("GET", "/key", key=self.coordinator_key)
        data = _response_object(response).get("data")
        if not isinstance(data, dict):
            raise ProviderError("OpenRouter returned invalid key metadata")
        limit = data.get("limit")
        if (
            not isinstance(limit, (int, float))
            or isinstance(limit, bool)
            or not 0 < limit <= self.settings.models.coordinator_monthly_dollars
            or data.get("limit_reset") != "monthly"
            or data.get("include_byok_in_limit") is not True
            or data.get("is_management_key") is True
        ):
            raise ProviderConfigurationError(
                "Coordinator key requires a monthly cap at or below the configured allowance, including BYOK usage"
            )
        remaining = _money(data.get("limit_remaining"))
        await self.budget.set_coordinator_remaining(remaining)
        if remaining <= 0:
            raise BudgetExceeded("Monthly coordinator allowance is exhausted")

    async def _request(
        self, method: str, path: str, *, key: str, timeout: float = 30, **kwargs: Any
    ) -> httpx.Response:
        remaining = self.budget.remaining_seconds()
        if remaining <= 0:
            raise DeadlineExceeded("Review deadline exceeded")
        try:
            async with asyncio.timeout(min(timeout, remaining)):
                return await self._http.request(
                    method,
                    f"{BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=min(timeout, remaining),
                    follow_redirects=False,
                    **kwargs,
                )
        except (TimeoutError, httpx.TimeoutException):
            if self.budget.remaining_seconds() <= 0:
                raise DeadlineExceeded(
                    "Review deadline exceeded; outstanding usage remains reserved"
                ) from None
            raise ProviderError(
                "OpenRouter request timed out; outstanding usage remains reserved"
            ) from None
        except httpx.HTTPError:
            raise ProviderError(
                "OpenRouter transport failed; outstanding usage remains reserved"
            ) from None

    async def _model(self, model: str) -> dict[str, Any]:
        async with self._catalog_lock:
            if not self._catalog:
                response = await self._request("GET", "/models", key=self.coordinator_key)
                entries = _response_object(response).get("data")
                if not isinstance(entries, list) or not all(
                    isinstance(entry, dict) and isinstance(entry.get("id"), str)
                    for entry in entries
                ):
                    raise ProviderError("OpenRouter returned invalid model metadata")
                self._catalog = {entry["id"]: entry for entry in entries}
        entry = self._catalog.get(model)
        if entry is None:
            raise ProviderConfigurationError("Configured model is absent from OpenRouter's catalog")
        supported = entry.get("supported_parameters", [])
        if not isinstance(supported, list):
            raise ProviderError("OpenRouter returned invalid model capabilities")
        if not {"tools", "structured_outputs"}.issubset(str(value) for value in supported):
            raise ProviderConfigurationError(
                "Configured model must support tools and structured outputs"
            )
        return entry

    async def _cost(self, data: dict[str, Any], generation: str | None, key: str) -> Decimal | None:
        try:
            return _money(data.get("usage", {}).get("cost"))
        except (ProviderError, AttributeError):
            pass
        if generation and self.budget.remaining_seconds() > 0:
            try:
                response = await self._request(
                    "GET", "/generation", params={"id": generation}, key=key, timeout=10
                )
                if response.is_success:
                    return _money(response.json()["data"]["total_cost"])
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                ProviderError,
                DeadlineExceeded,
            ):
                pass
        return None

    async def complete(
        self,
        *,
        role: Literal["coordinator", "worker"],
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        if role not in {"coordinator", "worker"}:
            raise ValueError("Unknown model role")
        messages = deepcopy(messages)
        schema = _strict_schema(schema)
        await self._check_key()
        model = (
            self.settings.models.coordinator
            if role == "coordinator"
            else self.settings.models.worker
        )
        entry = await self._model(model)
        pricing = entry.get("pricing", {})
        if not isinstance(pricing, dict):
            raise ProviderError("OpenRouter returned invalid model prices")
        prompt_price = _money(pricing.get("prompt"))
        completion_price = _money(pricing.get("completion"))
        request_price = _money(pricing.get("request", 0))
        max_tokens = min(max_tokens, self.settings.models.max_output_tokens)
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "provider": {
                "only": list(self.settings.models.allowed_providers),
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {
                    "prompt": float(prompt_price * 1_000_000),
                    "completion": float(completion_price * 1_000_000),
                    "request": float(request_price),
                },
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "review_result",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        # A byte bound deliberately overestimates tokens; include schema and message framing.
        input_tokens = (
            len(
                json.dumps({"messages": messages, "schema": schema}, ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            + 1024
            + 128 * len(messages)
        )
        context_length = entry.get("context_length")
        if not isinstance(context_length, int) or input_tokens + max_tokens > context_length:
            raise ProviderConfigurationError(
                "Input byte bound exceeds configured model context window"
            )
        estimate = (
            Decimal(input_tokens) * prompt_price
            + Decimal(max_tokens) * completion_price
            + request_price
        )
        key = self.coordinator_key if role == "coordinator" else self.worker_key
        # Stable identities include exact prompt boundaries and the normalized schema,
        # but records never retain source text, prompts, or authorization headers.
        system_prompt_hash = _canonical_hash(
            [message for message in messages if message.get("role") == "system"]
        )
        request_hash = _canonical_hash(payload)
        data: dict[str, Any] = {}
        for attempt in range(2):
            reservation = await self.budget.reserve(role, estimate)
            record: dict[str, Any] = {
                "role": role,
                "model": model,
                "reservation": reservation,
                "status": "reserved",
                "estimated_cost": float(estimate),
                "attempt": attempt + 1,
                "system_prompt_sha256": system_prompt_hash,
                "request_sha256": request_hash,
            }
            self.call_records.append(record)
            try:
                response = await self._request(
                    "POST", "/chat/completions", key=key, json=payload, timeout=120
                )
                data = _response_object(response, require_success=False)
            except BaseException:
                record["status"] = "usage_unknown"
                raise
            generation = data.get("id") or response.headers.get("x-generation-id")
            cost = await self._cost(data, generation, key)
            await self.budget.settle(reservation, cost)
            record.update(
                status="settled" if cost is not None else "usage_unknown",
                cost=float(cost) if cost is not None else None,
                generation_id=generation,
            )
            if response.is_success:
                if cost is None:
                    raise ProviderError(
                        "OpenRouter usage is unavailable; cost reservation retained"
                    )
                break
            if response.status_code not in {429, 502, 503, 504} or attempt == 1:
                raise ProviderError(f"OpenRouter rejected generation (HTTP {response.status_code})")
            try:
                delay = min(2.0, max(0.0, float(response.headers.get("retry-after", "0.25"))))
            except ValueError:
                delay = 0.25
            if self.budget.remaining_seconds() <= delay:
                raise ProviderError("Review deadline leaves no time for retry")
            await asyncio.sleep(delay)
        if data.get("model") != model:
            raise ProviderError(
                "OpenRouter returned a different model than the approved pinned model"
            )
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ProviderError("Model output did not finish normally")
            output = json.loads(choice["message"]["content"])
            Draft202012Validator(schema).validate(output)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError, SchemaError):
            raise ProviderError("Model returned invalid structured output") from None
        if not isinstance(output, dict):
            raise ProviderError("Model returned a non-object result")
        return output

    def usage(self) -> Usage:
        return self.budget.usage()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderError("OpenRouter omitted valid pricing or usage") from None
    if not amount.is_finite() or amount < 0:
        raise ProviderError("OpenRouter reported invalid pricing or usage")
    return amount


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _response_object(response: httpx.Response, *, require_success: bool = True) -> dict[str, Any]:
    if require_success and not response.is_success:
        raise ProviderError(f"OpenRouter metadata request failed (HTTP {response.status_code})")
    try:
        value = response.json()
    except ValueError:
        raise ProviderError("OpenRouter returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ProviderError("OpenRouter returned an invalid response object")
    return value


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make Pydantic's default-bearing schemas compatible with strict providers.

    Require fields explicitly (nullable types remain nullable), forbid extra keys,
    and resolve only local references. Never mutate a caller's reusable schema.
    """
    result = deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        for reference in ("$ref", "$dynamicRef"):
            if reference in node and not str(node[reference]).startswith("#"):
                raise ProviderConfigurationError(
                    "Structured output schemas require local references"
                )
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise ProviderConfigurationError(
                    "Structured output object properties must be a mapping"
                )
            if isinstance(node.get("additionalProperties"), dict):
                raise ProviderConfigurationError(
                    "Strict structured output does not support dynamic object keys"
                )
            node["required"] = list(properties)
            node["additionalProperties"] = False
        for keyword in ("properties", "$defs", "definitions", "patternProperties"):
            for child in node.get(keyword, {}).values():
                visit(child)
        for keyword in ("items", "contains", "not", "if", "then", "else"):
            visit(node.get(keyword))
        for keyword in ("anyOf", "allOf", "oneOf", "prefixItems"):
            for child in node.get(keyword, []):
                visit(child)

    try:
        Draft202012Validator.check_schema(result)
        visit(result)
        Draft202012Validator.check_schema(result)
    except SchemaError:
        raise ProviderConfigurationError("Invalid structured output schema") from None
    return result
