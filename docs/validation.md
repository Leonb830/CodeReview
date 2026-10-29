# Implementation validation

## Version 0.2.1

Validated on 2026-10-29 with Python 3.12.

| Check | Result |
|---|---|
| `uv run pytest -q` | 162 passed |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check src tests` | Passed across 32 files |
| `uv run mypy` | Passed across 17 source modules |
| `uv lock --check` | Passed |

New regression cases exercise metadata-only online diagnostics, both keys and
models, invalid model metadata, proxy/network errors, wrapped DNS and TLS
failures, secret redaction and preservation of generation cost reservations.
A full review/report case reproduces a metadata connection failure and verifies
that it reports the endpoint without claiming a generation was billed.

HTTP responses and connection failures are simulated. The user's Ubuntu/VPN
connection and live OpenRouter credentials have not been tested here; run
`codereview doctor --online` on that machine to identify the remaining setup issue.
No source was sent to a live model and no paid model calls were made.

## Version 0.2.0

Validated on 2026-10-29 with Python 3.12.

| Check | Result |
|---|---|
| `uv run pytest -q` | 144 passed |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check src tests` | Passed across 32 files |
| `uv run mypy` | Passed across 17 source modules |
| `uv lock --check` | Passed |
| CLI help and `--version` | Passed; reports `0.2.0` |

New cases exercise TypeScript, TSX, module and declaration files across working,
staged, branch and audit scopes; relative imports and direct importers; local
JSONC configuration inheritance and references; exact TypeScript evidence,
rejected fabricated evidence, stored reports and offline CLI inspection.
The existing Python suite remains passing.

TypeScript model responses are scripted in tests. No paid model calls, target
TypeScript compiler run, or Node validation-image build was performed for this
version. The optional image in the TypeScript guide is an example to adapt and
validate for the target project. Source relationship discovery remains lexical
and bounded, not a compiler-resolved module graph.

## Version 0.1.0

Validated on 2026-10-23 with Python 3.12.

| Check | Result |
|---|---|
| `pytest -q` | 129 passed |
| `ruff check src tests` | Passed |
| `ruff format --check src tests` | Passed |
| `mypy src/codereview` | Passed across 16 source modules |
| `uv lock --check` | Passed |
| `uv build` | Source distribution and wheel built |
| CLI help and local `doctor` | Passed; models/credentials correctly reported as unconfigured |
| Real Docker validation smoke | Passed |

The automated suite uses real temporary Git repositories and simulated external HTTP responses. End-to-end cases exercise the actual snapshot, provider, orchestration, storage, reporting, and GitHub event interfaces. They cover source evidence, independent verification, incomplete coverage, deletion impact, dirty local baselines, dependency lockfiles, requested fix commits, cost reservations/retries, source revision changes, repository-writer authorization, trusted artifacts, and publication reconciliation.

Independent standards and specification reviews identified issues that were corrected and given regressions: host execution through Git filters, inconsistent credential-file exclusions, unsafe inline rendering, incomplete baseline validation, misclassified baseline failures, unavailable repair lockfile context, operation deadline resets, and GitHub base-tip versus merge-base identity. An additional real-Git test verifies text conversion is disabled when producing fix patches.

GitHub Actions REST metadata was checked against public workflow runs: `pull_request_target` run head fields describe the source branch/repository, while workflow execution comes from the trusted base. Artifact recovery tests reflect that distinction. Tests also reject a fork-label event if its exact head/base revision has changed before execution.

The real Docker smoke built a local `FROM scratch` image containing a static test executable, without pulling images or using network access. Through the actual `execution.validate()` function, it confirmed:

- Captured source was readable and its disposable copy was writable.
- `.git` and `.env` were excluded.
- Dummy OpenRouter, GitHub, and controller environment secrets were absent.
- The container root was read-only.
- Original source and private files were unchanged after the check.

The smoke image was removed afterward. This smoke verifies the container integration in the development environment; target repositories still need their own trusted validation images and checks.

No paid OpenRouter calls, live GitHub publication, or target-specific quality calibration were performed. Model/provider compatibility, useful-finding rates, missed defects, actual cost/latency, and real repair quality remain to be measured with configured credentials and representative repositories. Budget amounts are enforced admission policies and intended limits, not benchmark results.
