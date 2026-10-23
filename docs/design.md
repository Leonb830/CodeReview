# Code Review Agent — accepted design

Status: accepted following 24 answered interview questions and the user's authorization to build. The implementation defaults described below are accepted starting choices. See README.md for implemented behavior, setup, and current limitations. No live OpenRouter model calls were made during implementation.

## Purpose and scope

Build a new Python engine and CLI for the user's repositories or one team, using selected OpenExecutive architecture patterns. Run the same engine locally and in GitHub Actions. No full OpenExecutive fork or always-on application service is required for v1.

Primary focus: maintainability, including overloaded modules, entangled/long functions, complexity, simpler alternatives, reusable existing/framework/library capabilities, and comments preserving non-obvious intent. Include smaller suggestions as well as correctness, security, and regression findings. Distinguish defects from suggestions. Size and complexity metrics guide inspection; they are not automatic findings unless a trusted repository rule establishes a threshold. Suggestions need honest concrete rationale, not necessarily substantial impact. Do not invent missing intent to write a comment.

V1 targets Python, TypeScript, React, Next.js, and Prisma. Java is deferred. Use target-repository versions and native tooling rather than assuming current framework behavior.

## Source findings and reuse boundary

Static source snapshot: SenteLabsAI/OpenExecutive a59b4420291945c839d6d5be9c724568772d8b7c. Its Executive model loop dispatches specialist calls concurrently and synthesizes results. Optional Committee invokes three critics and exactly one revision. Ordinary specialists return prose. Persistent workflows have checkpoints, atomic ownership claims and fencing; scheduler has a single-process assumption. The data-processing sandbox is not an arbitrary repository test runner. Current providers and UI versions differ from some architecture documentation.

Reuse patterns: bounded delegation, prepared context, independent review contexts, critique/consolidation, stable prompt caching, explicit tool policies, traceable runs and failure states. Build new snapshot, code-evidence, typed-finding, repair, repository-validation, budget and GitHub publication modules. Do not copy business personas, business knowledge, generic workflow UI, low-severity failure fallbacks or single-process scheduler assumptions.

Sources: [orchestration research](research/orchestration-research.md) and [boundary research](research/boundaries-research.md) carry pinned source citations; [maintainability research](research/maintainability-policy-research.md) maps user priorities and lb830 code-review/codebase-design sources; [OpenRouter budget research](research/openrouter-budget-research.md) contains official cost/key documentation. REA v45 explicitly routes complete source repositories to ordinary source analysis, so no REA binary engine was required or run. No runtime performance or review-quality claim is established by this research.

## Execution flow

GitHub event or CLI invocation -> immutable snapshot -> context inventory and trusted policy -> coordinator task plan -> bounded parallel workers -> evidence verification and deduplication -> coordinator consolidation -> findings, coverage and cost report -> GitHub/CLI publication.

Fix request -> authorize requester/select current findings -> isolated snapshot workspace -> selected repairs -> compare original/fixed check results -> separate fix commits -> local branch/worktree or GitHub draft fix PR.

## Models and agent responsibilities

OpenRouter exclusively for initial provider integration, using configurable model and upstream-provider allowlists. Relevant private source may be sent through this route. Own API keys, with separate coordinator and worker credentials.

Capable coordinator scopes the run, creates detailed worker tasks, follows progress and consolidates existing evidence. Each task specifies question, relevant paths/symbols, source revision, evidence required, permitted tools and bounded effort. Smaller-model workers investigate maintainability, correctness/security/regressions and framework-specific concerns. A verification pass challenges candidates and checks counterexamples. A requested-fix worker changes only selected findings. Separate worker contexts avoid early agreement contaminating independent analysis.

Proposed implementation defaults: configurable pinned model IDs; choose initial defaults from supported tool/structured-output models and evaluate against fixtures. No auto-routing to a disallowed model or provider. Deterministic coverage requirements prevent a coordinator omission from silently skipping mandatory review lenses. Task granularity and concurrency adapt to remaining cost and time. Coordinator consolidates supported records rather than inventing new unsupported findings.

## Budgets

| Operation | Worker model allowance | End-to-end deadline |
|---|---:|---:|
| PR/local change review | $2 | 10 minutes |
| Whole-repository audit | $10 | 30 minutes |
| Requested fix | $5 | 20 minutes, including validation |

Coordinator allowance: $100/month shared across GitHub, CLI, reviews, audits and fixes. Dedicated OpenRouter key with monthly reset is the provider backstop; OpenRouter resets monthly limits in UTC. Total model cost includes both buckets. Runner/compute charges are outside these model-spend allowances.

Role boundaries are fixed: coordinator planning/delegation/consolidation is monthly; specialist analysis, evidence verification, repair and model-assisted validation are workers. Retries and parallel calls count. Never relabel work to evade an exhausted bucket. Worker calls reserve conservatively estimated cost atomically before dispatch, bound input/output, and settle against reported cost; unresolved charges stay reserved. OpenRouter usage.cost is the account charge and upstream_inference_cost is not blindly added to it.

On exhausted budget/deadline, stop scheduling paid work, cancel supported operations, retain accounting for in-flight calls, and emit a deterministic partial report with uncovered scope. Provider key caps and cancellation do not justify promising zero in-flight overshoot or instant upstream billing termination. Reserve time for final reporting and fix validation.

## Inputs, evidence and configuration

GitHub PR mode examines changed code and affected modules, including related callers/tests for context. Whole-repository audits report existing debt without requiring a diff. CLI default captures staged, unstaged and nonignored untracked code; explicit staged-only, branch-comparison and audit modes remain available.

Each run records repository, exact base/head or local snapshot digest, configuration/prompt/engine/model identities, run and attempt identity, task coverage and usage. Each finding includes stable ID, category, severity, location/revision, observation, rationale/impact, suggested alternative or repair, evidence references and uncertainty/verification state. Preserve distinction between reasoning support and actual reproduction. Evidence and anchors must match the reviewed snapshot.

Proposed default: GitHub uses review configuration and agent instructions from the trusted base revision; PR edits to policy are reviewed as content. CLI trusts the user-selected project configuration. Source text cannot authorize tools, alter budgets or grant privileges. Existing lint/type rules are consumed without duplicating identical comments. No generic framework assumption overrides observed project versions or conventions.

## Reports and persistence

One updated PR summary contains all findings, including grouped smaller suggestions, plus selected inline comments. Findings distinguish newly introduced issues from existing maintenance concerns in affected code. Local output offers readable report and machine-readable records. Partial/failed coverage remains explicit; no missing reviewer becomes an implied clean review.

Proposed default: local SQLite/file records and equivalent versioned GitHub Actions artifacts hold findings and run manifests. Artifacts bind repository, PR revision, configuration and checksums. Resolve fix IDs only through trusted completed runs, not arbitrary links. Stale, missing, ambiguous or expired finding sets require review refresh. Superseded runs cannot publish current-looking findings. Reconcile ambiguous publication outcomes instead of blindly duplicating comments or PRs.

## Requested fixes

Repository writers may issue `/review fix F12 F19` or `/review fix-all`; authorization is checked deterministically at execution time and IDs bind to the current reviewed revision. One isolated branch/worktree per request. Fix-all means applicable compatible findings; conflicting suggestions are reconciled or marked skipped with reasons.

Suitable new dependencies are permitted within a requested fix without another approval. Assess candidate API/version/semantics, integration and maintenance cost, runtime compatibility and license; include manifest/lockfile changes. Do not silently expand a selected fix into unrelated refactoring.

Run relevant lint, type checks and tests against baseline and fixed snapshots. Only completed passing required checks yield validated status. Failed/unavailable validation yields a draft with explicit reasons, including baseline failures versus newly introduced regressions. Validation describes recorded checks, not a proof of all behavior; validated fixes remain draft PRs until a human decides to merge.

Proposed default: GitHub fix PRs target the original same-repository PR's source branch. Merging the fix updates the original PR. Fork PR reviews require maintainer trigger; fork fixes are deferred in v1. This fork limit is a visible product-scope proposal to confirm.

Proposed default: isolated Docker validation receives the source snapshot and bounded commands, with model/GitHub credentials retained by the controller. Separate dependency acquisition from test execution where practical. Missing isolation or unavailable integration services creates a validation gap/draft, without silent host-execution fallback. Prisma tests use disposable test data, never production credentials or migrations against a production database.

Local original checkout and index remain unchanged. Captured user edits form an isolated baseline; agent fix commits and generated patch separate agent changes from the user's pre-existing edits. A synthetic baseline commit, if needed, is never automatically pushed.

## Python module boundaries (proposed defaults)

- `snapshots`, `context`, `policy`: reproducible inputs and grounded access.
- `agents`, `findings`: task contracts, review lenses, verification, deduplication, synthesis.
- `execution`: isolated patching and native validation.
- `budgets`, `runs`: usage reservations, deadlines, persistence, coverage and retries.
- `cli`, `github`: thin entry and publication adapters over the shared engine.

Python package managed by uv, typed/schema-validated data, async OpenRouter/HTTP calls, Git subprocess interfaces, and focused public module contracts. No LangGraph/CrewAI requirement, FastAPI service or web dashboard for v1. A provider protocol and run-store interface leave room for later changes without building speculative infrastructure.

## Build and validation plan

1. Snapshot/config/finding contracts and deterministic CLI report; fixtures establish staged/unstaged/untracked behavior and source anchors.
2. OpenRouter two-tier delegation, worker budget reservations, deadlines and recorded usage; verify concurrency, retry and incomplete-result behavior without paid calls.
3. Maintainability/defect/framework review, verification and consolidation; labeled Python and TS/React/Next/Prisma changed-code fixtures plus cohesive/clean counterexamples and whole-repo audit cases.
4. GitHub Actions publisher, current-head checks, idempotency and comment authorization; test stale/duplicate/unauthorized cases and persist trusted artifacts.
5. Selected fixes/fix-all, isolated validation, dependency changes and draft PR flow; test preservation of original local work and baseline-vs-regression reporting.
6. Calibrate real configured model pairs on a small labeled suite and human-rated suggestions; report measured false positives, useful suggestions, known defects missed, repair regressions, latency and cost. No quality claim from prose-judge scores alone. Paid calibration occurs only within configured authorized limits/credentials.

The user confirmed shared understanding and authorized implementation: “Nice looks good you can start bulding.” The interview and defaults above form the accepted starting specification; model calibration remains dependent on configured credentials and real repository examples.
