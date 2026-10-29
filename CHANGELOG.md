# Changelog

## 0.2.0 — 2026-10-29

- Add TypeScript-specific review guidance while retaining source evidence verification and the same local/GitHub review workflow.
- Improve related context for local TypeScript/JavaScript imports, reexports, and project configuration.
- Add TypeScript regression coverage and a setup guide with an optional offline type-check validation image example.
- Add `codereview --version` and record the shared package version in new review and fix results.

TypeScript context discovery remains bounded and lexical. Reviews do not run the TypeScript compiler or install project dependencies.

## 0.1.0

- Initial budgeted review and requested-fix engine, local CLI, and GitHub Actions integration.
