# Forward E2E Documentation

Structured reference documentation for the `gonka24/forward-e2e` acceptance
runner.

## Contents

| Document | Topic |
|---|---|
| [`architecture.md`](architecture.md) | Architectural layers (`forward_e2e.suite` and `forward_e2e.execution`), one-way import rule, execution lifecycle, and four-zone filesystem isolation (sources, build outputs, test code, network state). |
| [`operations.md`](operations.md) | Operator CLI reference (`list`, `plan`, `run`, `rerun`, `report`, `recover`), Bash/PowerShell host wrappers, source/selection/operational flags, private repo credentials, Docker volumes, and cancellation semantics. |
| [`evidence.md`](evidence.md) | Run package and suite directory layouts, document producer/consumer matrix, schemas, proof levels (`NATIVE`, `GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`), `a8.evidence/e2e-immutable-source/2` vs historical models, and whole-run verdicts (`PASSED`, `FAILED`, `INCOMPLETE`, `CANCELLED`, `NOT_REVIEWED`). |
| [`coverage.md`](coverage.md) | Complete catalog of all 24 automated tasks (canonical IDs, legacy aliases, proof levels, test selectors, required checkpoints/scopes, profiles, timeouts), contract validation obligations, manual `wasm-query-allowlist` probe, and explicit coverage boundaries. |
| [`validation.md`](validation.md) | Step-by-step runbook for validating a runner commit and target SHA pair (offline suites, runner image build, negative CLI checks, `build-external-harness`, `smoke` run/replay, `all` run, `report` and `recover`). |
| [`development.md`](development.md) | Codebase guide, running the three offline test suites, synthetic fixture contract (`tests/fixtures/evidence/`), files hashed into `run.lock.json`, and procedures for updating the harness or `vendor/contract_release/release.py`. |
| [`migration.md`](migration.md) | Migration matrix from the extracted repository layout to the current structure: file/directory paths, 8 renamed scenario IDs and aliases, `E2E_*` / `A8_*` environment variables, wire-format compatibility constants, and handling superseded locks. |
| [`licensing.md`](licensing.md) | BUSL-1.1 / Apache-2.0 licensing terms, Change Date publication procedure, and third-party notices. |
