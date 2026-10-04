# Runner Harness Scripts (`scripts/`)

This directory contains the acceptance harness and boundary probe drivers that
run inside the runner container.

All four files are hashed into `lock.runner.harness_hash` (`HARNESS_FILES` in
[`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py)),
together with `vendor/contract_release/release.py`,
`forward_e2e/suite/source_snapshot.py` and `harness/container_control.py`.

## Contents

| File | Role |
|---|---|
| [`acceptance_harness.py`](acceptance_harness.py) | Live acceptance harness (`run-live`, `build-external-harness`, `wasm-query-allowlist` / `p0-probe`, and Testermint callback subcommands). |
| [`external_harness.py`](external_harness.py) | Out-of-tree Gradle harness builder, upstream Testermint API verifier, network root copier (`prepare_network_root`), and the genesis delta verifier for `foreign-native-preservation` (`verify_b3_genesis_delta`). |
| [`run_go_boundary.py`](run_go_boundary.py) | Go query error classification boundary probe driver (`go-query-error-classification` / legacy alias `go-boundary`): a `docker build` derived from Gonka's own `inference-chain/Dockerfile`, never host Go; see [`harness/go_boundary/README.md`](../harness/go_boundary/README.md). It also loads `forward_e2e/suite/source_snapshot.py` by file path (registered as `a8_source_snapshot`). |
| [`test_wasm_query_boundary.mjs`](test_wasm_query_boundary.mjs) | Node.js synthetic-host `ExternalQuerier` ABI probe driver for the `wasm-abi-boundary` catalog task (canonical id; it was never renamed and has no alias). It exercises the compiled `a8-query-boundary` probe's `query_chain` ABI against nine synthetic cases (`WASM_ABI_REQUIRED_CASES`); it does **not** talk to Gonka's gRPC query allowlist — that is the separate manual `wasm-query-allowlist` subcommand of `acceptance_harness.py`, see [`docs/coverage.md`](../docs/coverage.md) §4. (`wasm-query-allowlist-abi.json` is only the name of the committed test fixture for this driver's output.) |

## Constraints

- `scripts/acceptance_harness.py` is executed both by the Python suite adapter
  and as a subprocess re-entered by Kotlin Testermint scenarios. It must not
  import `forward_e2e.*` packages directly; it loads
  `forward_e2e/suite/source_snapshot.py` by file path.
- For architecture and evidence details, see
  [`docs/architecture.md`](../docs/architecture.md) and
  [`docs/evidence.md`](../docs/evidence.md).
