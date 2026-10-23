# OpenRouter budget and routing research

Checked official OpenRouter sources on 2026-10-23. No account access, credential changes, billable calls or model selection was performed.

## Verified facts

**Accounting is reported after a generation.** OpenRouter automatically includes native prompt/completion token counts, reasoning/cache detail where applicable, and cost. For streaming this is in the final usage frame. The old `usage.include` and `stream_options.include_usage` flags are deprecated. `usage.cost` is the OpenRouter account charge; `cost_details.upstream_inference_cost` describes upstream inference. They are different accounting views, not two values to blindly add. [Usage Accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)

**An interrupted response can be reconciled by generation ID.** `GET /api/v1/generation?id=...` exposes metadata including `total_cost`, `upstream_inference_cost`, provider/model, native tokens and `cancelled`. The streaming reference documents `X-Generation-Id` on response headers. This provides a recovery route when the final usage frame was lost; it is not a pre-call cost quote. [Generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation), [Streaming](https://openrouter.ai/docs/api_reference/streaming)

**Keys can have distinct caps.** API-key creation accepts a USD `limit`, `limit_reset` of daily/weekly/monthly/null, expiration and `include_byok_in_limit`. Reset windows use UTC; null means no reset. Key creation needs a management key. Thus a dedicated coordinator key can carry `limit:100, limit_reset:"monthly"`; the API does not expose a “review_id budget” field here. [Create API key](https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key)

**Budget scope matters.** Official guidance calls the key credit limit a hard cap for that key. A workspace budget is a separate pooled limit; member guardrails attribute spend to a key's creator, not whoever uses it. Workspace-owned service keys do not inherit a member's guardrail. A workspace-wide pooled budget is documented as an Enterprise feature, so it should not be assumed available for this personal/team application. The documentation does not establish an atomic reservation guarantee covering all already-running requests at the exact moment a key cap is reached. [Capping shared agent keys](https://openrouter.ai/docs/guides/best-practices/capping-shared-agent-keys)

**Routing controls are not a total review budget.** `provider.max_price` filters acceptable prompt/completion unit prices (per million tokens) and, where applicable, per-request/image prices. It does not mean “spend no more than $2 across this workflow.” `require_parameters:true` excludes endpoints that cannot honor requested features; otherwise unsupported parameters can be ignored. `data_collection:"deny"` and `zdr:true` provide privacy routing filters; ZDR covers model inference endpoints, not arbitrary tool backends. [Provider Routing](https://openrouter.ai/docs/guides/routing/provider-selection)

**Structured output capability belongs to the endpoint.** Use JSON Schema via `response_format` and require supporting providers. Support may differ between providers for the same model and change over time. The current docs caution that even `strict:true` enforcement varies by provider; application validation remains necessary. [Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs)

**Cancellation is not a reliable financial stop.** OpenRouter's official cost guidance warns that some providers continue generation and billing after a client aborts a stream. The current streaming reference does not offer a universal cancellation guarantee or an aggregate in-flight dollar cap. A ten-minute application deadline must therefore not be represented as proof that all billing stops at ten minutes. [Official cost guidance, cancellation section](https://openrouter.ai/blog/tutorials/how-to-get-the-lowest-cost-llm-inference-on-openrouter/)

## Recommended design (inference, not an OpenRouter feature)

Use two explicit accounting buckets:

- **Coordinator:** all planning, delegation, clarification, final verification and synthesis calls through its dedicated key, with the agreed $100/month cap and an application ledger.
- **Workers:** all worker calls, retries and verifier work assigned to the $2 review allowance. Whether a verifier counts as coordinator or worker should be explicit in the job type, never chosen opportunistically to bypass the cap.

Put budget enforcement in a deterministic broker outside the LLM. Before dispatching each call, atomically reserve a conservative maximum charge based on bounded context, allowed endpoint price ceilings, maximum billable output/reasoning and any enabled extras. Authorize only when:

```
settled cost + outstanding reservations + new reservation <= bucket limit
```

All concurrent workers share that transactionally protected review ledger. Retries/fallbacks require budget and time remaining. Reconcile reservations using reported cost/generation metadata; do not release an unknown-cost reservation merely because a request timed out. Keep an explicit unresolved-accounting state until reconciled. Use decimal/integer monetary arithmetic.

The coordinator key supplies an external monthly backstop. For workers, an optional isolated short-lived key capped at $2 with no reset would add a per-run provider backstop, but requires management-key provisioning and lifecycle work. A long-lived worker key plus application per-review reservations is operationally simpler. Neither architecture should promise zero-cent overshoot from cancellation or undocumented in-flight behavior. The local admission bound is only as sound as the model/pricing/token and feature limits it covers.

Treat ten minutes as the overall review deadline: reserve time for final consolidation, stop admitting work near the deadline, cancel supported outstanding operations, and produce a partial report listing unreviewed areas when budget/time runs out. In-flight reservations remain accounted for after that report if billing is unresolved.

Keep model IDs configurable and select them using a small review evaluation corpus, measured cost and latency. Do not assume “smaller” implies adequate tool use, schema compliance or trustworthy findings. Approved fallback endpoints must satisfy the same capabilities, privacy and price constraints.

## Unresolved product details

The docs cannot decide whether the dollar budgets mean inference credits or total cash paid including credit-purchase fees/taxes, whether BYOK is in scope, what happens once the monthly coordinator allowance is exhausted, or whether per-run worker keys are worth the operational cost. These remain interview/design choices. No source reviewed guarantees a ten-minute successful review at the selected budgets.
