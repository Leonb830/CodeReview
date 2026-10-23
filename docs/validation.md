# Implementation validation

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
