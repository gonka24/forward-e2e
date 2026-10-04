# Forward E2E Documentation

Structured reference documentation for the `gonka24/forward-e2e` acceptance
runner.

## Contents

| Document | Topic |
|---|---|
| [`architecture.md`](architecture.md) | Architectural layers (`forward_e2e.suite` and `forward_e2e.execution`), one-way import rule, execution lifecycle, four-zone filesystem isolation (sources, build outputs, test code, network state), and the boundary mechanisms (out-of-tree Testermint compilation, upstream API verification, `B3` genesis provisioning, API container restarts by owned container ID). |
| [`operations.md`](operations.md) | Operator CLI reference (`list`, `plan`, `run`, `rerun`, `report`, `recover`), Bash/PowerShell host wrappers, source/selection/operational flags and which of them are refused with `--from`, where results land on the host, private repo credentials, Docker volumes, and cancellation semantics. |
| [`evidence.md`](evidence.md) | Durable stage vs exported run package layouts, document producer/consumer matrix, schemas, evidence models (`a8.evidence/e2e-immutable-source/2` vs the two historical models), proof levels (`NATIVE`, `GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`), the four whole-run verdicts (`PASSED`, `FAILED`, `INCOMPLETE`, `CANCELLED`) and the always-`NOT_REVIEWED` acceptance status. |
| [`coverage.md`](coverage.md) | Complete catalog of all 24 automated tasks (canonical IDs, legacy aliases, proof levels, test selectors, required checkpoints/scopes, profiles, timeouts, per-task limitations), the obligation mapping against the contracts' documented behaviour (runnable check, result for a specific SHA pair, historical evidence, not covered), the manual `wasm-query-allowlist` probe and its lack of a supported execution path, and known coverage boundaries including the `R6.1` naming determination. |
| [`validation.md`](validation.md) | Step-by-step runbook for validating a runner commit and target SHA pair (offline suites, runner image build, negative CLI checks, the optional developer-only `build-external-harness` shortcut, `smoke` run/replay, `boundary` and `all` runs, `report` and `recover`). |
| [`development.md`](development.md) | Codebase guide, running the three offline test suites (with the macOS `TMPDIR` and host-Git caveats), synthetic fixture contract (`tests/fixtures/evidence/`), files hashed into `run.lock.json`, and procedures for updating `harness/wasm_query_allowlist` and `vendor/contract_release/release.py`. |
| [`migration.md`](migration.md) | Migration matrix from the extracted repository layout to the current structure: file/directory paths, 8 renamed scenario IDs and aliases, `E2E_*` / `A8_*` environment variables, wire-format compatibility constants, and handling superseded locks. |
| [`licensing.md`](licensing.md) | BUSL-1.1 / Apache-2.0 licensing terms, Change Date publication procedure, and third-party notices. |
