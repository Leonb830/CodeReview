# Reviewing TypeScript

CodeReview 0.2 reviews TypeScript with the same local CLI and GitHub Actions engine as Python. The reviewer itself needs Python 3.11+ and Git. A target repository can contain only TypeScript; it does not need Python files or a `pyproject.toml`.

## Quickstart

Install the CLI from the CodeReview checkout:

```sh
uv tool install /absolute/path/to/CodeReview
codereview --version
```

Initialize the target Git repository, supplying your chosen OpenRouter models and upstream provider:

```sh
codereview init --root /path/to/typescript-project \
  --coordinator-model YOUR_COORDINATOR_MODEL_ID \
  --worker-model YOUR_WORKER_MODEL_ID \
  --provider YOUR_ALLOWED_PROVIDER_SLUG
codereview doctor --root /path/to/typescript-project
```

Both models must support tools and structured outputs. For online reviews, supply `OPENROUTER_COORDINATOR_KEY` and `OPENROUTER_WORKER_KEY` through your shell or secret manager as described in the [README](../README.md#install-and-configure). Do not put keys in repository files. Online review sends bounded source context to OpenRouter and your allowed upstream providers.

The default doctor checks local presence only. `doctor --online` verifies keys and catalog metadata without sending source or making model calls. If it reports a transport failure, follow the [connection troubleshooting guide](../README.md#connection-and-configuration-troubleshooting). The TypeScript file exclusions and missing optional fix-validation checks do not cause this connection error.

After supplying the keys, verify the online setup:

```sh
codereview doctor --root /path/to/typescript-project --online
```

Inspect what will be captured without making model calls:

```sh
codereview review --root /path/to/typescript-project --offline
```

Offline mode deliberately returns exit code `2` and a partial report: it captures scope but does not review the code. It requires neither model credentials nor a Node.js installation.

Then choose a review scope:

```sh
# Staged, unstaged, and nonignored untracked changes.
codereview review --root /path/to/typescript-project

# Only the staged index.
codereview review --root /path/to/typescript-project --staged

# Committed branch changes from the merge-base of the supplied reference.
codereview review --root /path/to/typescript-project --base origin/main

# The whole captured repository, including unchanged source.
codereview audit --root /path/to/typescript-project
```

No language option is needed. `.ts`, `.tsx`, `.mts`, `.cts`, and declaration files such as `.d.ts` are already supported snapshot inputs. JavaScript, JSON/JSONC configuration, package manifests, and Prisma schemas can supply related evidence. Generated output and dependency directories are excluded; capture and context limits still apply.

## What the reviewer considers

TypeScript guidance covers narrowing and nullability, structural contracts, unsafe assertions, erased types at runtime, asynchronous control flow, and module/runtime boundaries. React/Next.js and Prisma claims must be grounded in captured versions and the relevant runtime or transaction context. The verifier checks exact source evidence and counterexamples before a finding appears.

Related context includes direct local relative imports, reexports and importers (one source-module hop), including common `.js`/`.mjs`/`.cjs` specifiers pointing at TypeScript source. Nearby project configuration includes JSONC, local `extends` chains and project references. This is bounded lexical discovery. It does not implement the TypeScript compiler's module resolution, resolve package exports or path aliases, install dependencies, or guarantee a complete dependency graph. Missing or truncated context must not be treated as proof that a program is correct.

Normal reviews do not execute `tsc`, lint, tests, npm scripts, or application code. Findings are source-backed reasoning; a supported finding does not mean a runtime bug was reproduced. Keep your normal type-check and test CI, and use isolated validation checks when requesting fixes.

## Optional type-check validation for fixes

Validation runs only for requested fixes, against both the original snapshot and proposed patch. It uses fresh disposable source copies mounted at `/workspace`, with no network or controller credentials and a read-only container root. A plain Node image has no project dependencies; `npm install` or `npx` downloads cannot work during validation.

For a single-package npm project, prepare a trusted image containing the project's pinned dependencies at `/opt/project/node_modules`. Its type-check wrapper links those dependencies into the disposable source tree so both `tsc` and TypeScript's module lookup can find them. For example, save this wrapper as `codereview-typecheck` in your trusted image build context:

```sh
#!/bin/sh
set -eu
cd /workspace
if [ -e node_modules ] || [ -L node_modules ]; then
    echo "Expected a fresh source copy without node_modules" >&2
    exit 1
fi
ln -s /opt/project/node_modules node_modules
exec node /opt/project/node_modules/typescript/bin/tsc --project tsconfig.json --noEmit
```

An example Dockerfile for that trusted build context:

```dockerfile
# Pin the approved Node image by digest in your real build.
FROM node:22-bookworm-slim
WORKDIR /opt/project
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY --chmod=755 codereview-typecheck /usr/local/bin/codereview-typecheck
```

The manifest must include the project's TypeScript version in its dependencies or devDependencies. Build and load this image through your normal trusted build process before running fixes, then use its immutable digest in the target's `.codereview.toml`:

```toml
[[checks]]
name = "typescript-types"
image = "your-registry/project-typescript-checks@sha256:REPLACE_WITH_DIGEST"
command = ["/usr/local/bin/codereview-typecheck"]
timeout_seconds = 120
```

The wrapper and image are an example, not a built-in tool or prebuilt image. Adapt them for monorepos, project references, other package managers, and application tests. If dependencies need trusted build steps or generated clients (for example Prisma), prepare them explicitly in the image build; `--ignore-scripts` does not run them. Check that required paths are readable by the validation container's non-root UID. Cache/output paths must use the writable source copy or `/tmp`.

Add project lint and test checks with their own trusted wrappers. Each check gets a separate source copy, so each wrapper must set up its own dependency link. A dependency change may require rebuilding the validation image. Missing images, unavailable tools, failed checks, or incomplete review leave the fix as a draft, never a validated result.

The [GitHub Actions setup](github.md) works for TypeScript targets as well; configure trusted policy and validation images in the controller's normal trusted setup, not from unreviewed pull-request code.
