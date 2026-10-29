# Changelog

## 0.2.1 — 2026-10-29

- Add `codereview doctor --online` to verify OpenRouter credentials and model metadata without submitting source or making model calls.
- Distinguish local presence checks from verified setup, and report online check failures as structured JSON with a nonzero exit status.
- Report the failing request and safe transport-error category with troubleshooting guidance; do not imply a generation cost reservation for metadata requests.
- Document proxy/certificate troubleshooting, exact catalog model IDs and refreshing an installed CLI.

## 0.2.0 — 2026-10-29

- Add TypeScript-specific review guidance while retaining source evidence verification and the same local/GitHub review workflow.
- Improve related context for local TypeScript/JavaScript imports, reexports, and project configuration.
- Add TypeScript regression coverage and a setup guide with an optional offline type-check validation image example.
- Add `codereview --version` and record the shared package version in new review and fix results.

TypeScript context discovery remains bounded and lexical. Reviews do not run the TypeScript compiler or install project dependencies.

## 0.1.0

- Initial budgeted review and requested-fix engine, local CLI, and GitHub Actions integration.
