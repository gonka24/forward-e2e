# Runner Harness Scripts (`scripts/`)

This directory contains the acceptance harness and boundary probe drivers that
run inside the runner container.

All four files are hashed into `lock.runner.harness_hash` (`HARNESS_FILES` in
[`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py)).

## Contents

| File | Role |
|---|---|
| [`acceptance_harness.py`](acceptance_harness.py) | Live acceptance harness (`run-live`, `build-external-harness`, `wasm-query-allowlist` / `p0-probe`, and Testermint callback subcommands). |
| [`external_harness.py`](external_harness.py) | Out-of-tree Gradle harness builder, upstream Testermint API verifier, network root copier (`prepare_network_root`), and B3 genesis delta verifier. |
| [`run_go_boundary.py`](run_go_boundary.py) | Go query error classification boundary probe driver (`go-query-error-classification` / legacy alias `go-boundary`). |
| [`test_wasm_query_boundary.mjs`](test_wasm_query_boundary.mjs) | Node.js synthetic-host `ExternalQuerier` ABI probe driver (`wasm-query-allowlist-abi` / legacy alias `wasm-abi-boundary`). |

## Constraints

- `scripts/acceptance_harness.py` is executed both by the Python suite adapter
  and as a subprocess re-entered by Kotlin Testermint scenarios. It must not
  import `forward_e2e.*` packages directly; it loads
  `forward_e2e/suite/source_snapshot.py` by file path.
- For architecture and evidence details, see
  [`docs/architecture.md`](../docs/architecture.md) and
  [`docs/evidence.md`](../docs/evidence.md).
