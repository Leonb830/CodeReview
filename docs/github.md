# Install on a GitHub repository

The repository containing the reviewed code is the **target**. The repository containing this Python package is the **engine**. They can be the same repository. Workflows and configuration must be installed on a trusted default/base branch before they can review PRs.

1. Copy `.github/workflows/review.yml` and `.github/workflows/fix.yml` into the target repository. Keep those filenames: artifact provenance checks bind to `review.yml`. `ci.yml` tests this engine and is not required in target repositories.
2. Create the target's `.codereview.toml` using `codereview init`, select model/provider allowlists, and configure validation checks. Commit it to the trusted branch.
3. If target and engine differ, set target **Actions variables**:

   | Variable | Value |
   |---|---|
   | `CODE_REVIEW_ENGINE_REPOSITORY` | `owner/CodeReview` |
   | `CODE_REVIEW_ENGINE_REF` | Full lowercase 40-character trusted engine commit SHA |

   Set both variables or neither. With neither, the workflows use engine code from the target's trusted base/default revision. They never install the engine from the PR head. For a private external engine, provide `CODE_REVIEW_ENGINE_TOKEN` as a secret with read-only access to its contents. Public engines do not need it.

4. Add target **Actions secrets** `OPENROUTER_COORDINATOR_KEY` and `OPENROUTER_WORKER_KEY`. Use the same dedicated coordinator key for all target repositories and local runs; configure its OpenRouter monthly cap to $100 or less with BYOK included. The worker key must differ. See the README for model capabilities and budget details.
5. In repository Actions settings, allow the workflow to create pull requests. The workflows request their declared GitHub permissions. If you need normal CI triggers on agent-created commits/PRs, supply `CODE_REVIEW_APP_TOKEN` with a suitable installation token; it requires contents and pull-request write access. The default `GITHUB_TOKEN` can create the fix PR where permitted, but GitHub suppresses recursive events. Installation tokens expire: integrate your normal token-minting step if supplying a token dynamically rather than keeping a short-lived token as a static secret.
6. Prepare trusted Docker check images before the fix command runs. Add a trusted workflow step to pull/build your pinned image without executing PR code or giving repository tests controller credentials. The validation runner does not use Docker registry credentials and does not install PR-supplied dependencies on the host. Missing dependencies/images produce a draft with validation gaps.

The review workflow reacts to same-repository PR opening, synchronization, reopening, and readiness. A fork PR is reviewed only when a repository writer applies the `codereview` label. That permission covers the exact reviewed revision: remove and reapply the label after a new fork commit to request another review. The workflow rechecks the sender's repository permission through GitHub's API; label presence alone does not authorize future commits.

Once a review finishes, request selected fixes in the original PR:

```text
/review fix F1234567890 F0987654321
```

Use the actual IDs in the latest report. To select all findings, comment exactly:

```text
/review fix-all
```

Only repository writers may request fixes. The adapter reads the current comment, checks permission and current PR revision, loads a trusted review artifact, and rejects stale or mismatched findings. One request creates a separate branch and draft PR against the original PR's source branch. Fork fixes are intentionally unavailable in v1. Human merging of the draft fix PR updates the original PR.

The review posts one updated summary plus selected inline comments. It also retains a full report, structured findings/source snapshot, and checksum manifest as an Actions artifact for 30 days. Fix runs retain the patch, validation results, and publication metadata. Artifact checksums detect changes; authenticated workflow/run provenance supplies trust. Expired artifacts require a fresh review. These artifacts contain reviewed source, so repository access and artifact-retention policies also govern the captured code.

Concurrent reviews of the same PR cancel superseded workflow runs; publication checks the current head before each write. The adapter reconciles ambiguous comment/PR writes using stable bot markers and request IDs. A model call already in flight may still incur provider charges after workflow cancellation.

## First-run checks

- Ensure the engine ref is a full commit SHA reachable in the configured engine repository.
- Verify the target's trusted configuration includes both model IDs and a nonempty allowed-provider list.
- Check `doctor` locally and inspect the Actions artifact for incomplete coverage or unknown charges.
- Try one small PR before relying on larger reviews. Provider compatibility and review quality need validation with your selected models.
- Request a small fix and inspect its baseline/fixed diagnostics. Missing Docker checks should visibly produce a draft, not a validated claim.

No GitHub installation, secret configuration, live OpenRouter calls, or remote publication is performed by installing the source package or running its test suite.
