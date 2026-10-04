# Repository Refactoring Reference

This repository has no backward compatibility requirement for earlier runner
commands, scenario names, environment aliases, or Docker state. Use the current
interface in [operations.md](operations.md); create a new plan with this runner.
The path map below records the refactor and does not promise old entrypoints.

## 1. Directory and File Path Mapping

| Old Path (at extraction / baseline) | Current Path |
|---|---|
| `ops/a8/__init__.py`, `adapters.py`, `catalog.py`, `collector.py`, `evidence_model.py`, `lock.py`, `models.py`, `orchestrator.py`, `reporter.py`, `runtime.py`, `source_snapshot.py`, `supervisor.py`, `verifier.py` | `forward_e2e/suite/` (same module names) |
| `ops/a8/e2e/*` | `forward_e2e/execution/*` (same module names) |
| `ops/a8/Dockerfile` | `ops/runner/Dockerfile` |
| `ops/a8/compose.yaml` | `ops/runner/compose.yaml` |
| `ops/a8/e2e-entrypoint.sh` | `ops/runner/entrypoint.sh` |
| `ops/a8/.env.example` | `ops/runner/.env.example` |
| `ops/a8/RUNNER_VERSION` | `ops/runner/RUNNER_VERSION` (content changed, see [§5](#5-handling-pre-refactoring-plans-and-run-packages)) |
| `ops/a8/harness/testermint/gradle/a8-out-of-tree.init.gradle.kts` | `harness/testermint/gradle/out-of-tree.init.gradle.kts` |
| `ops/a8/harness/testermint/src/test/kotlin/A8ApiContainerControl.kt` | `harness/testermint/src/test/kotlin/ApiContainerControl.kt` |
| `ops/a8/harness/testermint/src/test/kotlin/A8PackageBBankFaultPlan.kt`, `A8PackageBBankFaultPlanTests.kt` | `harness/testermint/src/test/kotlin/BankSendFaultPlan.kt`, `BankSendFaultPlanTests.kt` |
| `ops/a8/harness/testermint/src/test/kotlin/A8UpstreamTestSupport.kt` | `harness/testermint/src/test/kotlin/UpstreamTestSupport.kt` |
| `ops/a8/harness/testermint/` (`build.gradle.kts`, `settings.gradle.kts`, `required-upstream-api.json`, `MarketplaceContractAcceptanceTests.kt`, `MarketplaceHarnessProcess.kt`, `MarketplaceHarnessProcessTests.kt`) | `harness/testermint/` (file names unchanged) |
| `ops/a8/harness/network/a8-ownership.yml` | `harness/network/ownership.yml` |
| `ops/a8/harness/network/a8-nats.yml` | `harness/network/nats.yml` |
| `ops/a8/harness/network/a8-b3-genesis.yml` | `harness/network/foreign-native-genesis.yml` |
| `ops/a8/harness/network/genesis/a8-genesis-provision.sh` | `harness/network/genesis/foreign-native-genesis-provision.sh` |
| `ops/a8/harness/go-boundary/a8faults/` (`plan.go`, `plan_test.go`) | `harness/go_boundary/query_faults/` |
| `ops/a8/harness/wasm-probe/p0-probe/` | `harness/wasm_query_allowlist/` (the compiled artefact keeps its name, `artifacts/p0_probe.wasm`) |
| `ops/a8/harness/container_control.py` | `harness/container_control.py` |
| `scripts/a8_acceptance.py` | `scripts/acceptance_harness.py` |
| `scripts/a8_external_harness.py` | `scripts/external_harness.py` |
| `scripts/run_a8_go_boundary.py` | `scripts/run_go_boundary.py` |
| `scripts/test_wasm_query_boundary.mjs` | `scripts/test_wasm_query_boundary.mjs` (unchanged name; it was never prefixed) |
| `scripts/a9_release.py` | `vendor/contract_release/release.py` (bytes unchanged; provenance in [`EXTRACTION.json`](../EXTRACTION.json)) |
| `ops/a8/tests/` | `tests/unit/runner/` |
| `ops/a8/tests/fixtures/catalog-ee14240.json` | Removed: no test read it at the baseline or after. Superseded by `tests/fixtures/compatibility/catalog-baseline-8079c0c.json`, a frozen dump of the baseline catalog's budget fields that `tests/unit/runner/test_catalog_baseline_equivalence.py` compares the live catalog against. |
| `scripts/tests/` | `tests/unit/harness/` |
| `ops/a8/integration_tests/` | `tests/integration/local_sources/` |
| `docs/reviews/evidence/*` (recorded development receipts) | Deleted. Replaced by deterministic synthetic fixtures in `tests/fixtures/evidence/`, generated or checked by `tests/unit/runner/support/synthetic_evidence.py` and pinned by `tests/unit/runner/test_synthetic_evidence.py`. The per-fixture replacement map is in [fixture replacement map](#fixture-replacement-map). |
| `ops/a8/README.md`, `ops/a8/coverage-map.md`, `ops/e2e/RUNBOOK-immutable-sources.md` | Replaced by structured docs in `docs/` (`architecture.md`, `operations.md`, `evidence.md`, `coverage.md`, `validation.md`, `development.md`, `migration.md`) |

### Python CLI entrypoint

- **Old:** `python3 -m ops.a8.e2e.cli`
- **Current:** `python3 -m forward_e2e.execution.cli`

The subcommands are unchanged: `list`, `plan`, `run`, `rerun`, `report`,
`recover` (`SUBCOMMANDS` in
[`forward_e2e/execution/cli.py`](../forward_e2e/execution/cli.py)). The host
wrappers `ops/e2e/run-e2e.sh` and `ops/e2e/Run-E2E.ps1` keep their names and
forward the same subcommands.

## Current Interface

Only exact scenario IDs from `python3 -m forward_e2e.execution.cli list` are
accepted. Pre-refactoring selectors such as `go-boundary`, `package-a-r1-r2`,
and `package-b-r7-1` are unknown scenarios. Plans and reports do not translate
old task IDs. The manual probe command is `wasm-query-allowlist`; `p0-probe`
is no longer a command.

Only current `E2E_*` configuration names are supported. There is no `A8_*`
fallback, conflict resolver, dual export, or deprecation window. Semantic
`E2E_*`, `GONKA_*`, and `TESTERMINT_*` values are frozen into the run lock;
operational paths are recorded by name. The harness and Kotlin tests exchange
only the current environment names.

Current configuration:

| Variable | Purpose |
|---|---|
| `E2E_WORKSPACE_DIR` | Persistent runtime workspace; defaults to `/workspace`. |
| `E2E_OUTPUT_DIR` | Writable plans and exported run packages; defaults to `/out`. |
| `E2E_APP_ROOT` | Runner code root for the container entrypoint; defaults to `/app`. |
| `E2E_DOCKER_ROOT_VOLUME` | Host-selected private Docker daemon state volume. |
| `E2E_RUNNER_IMAGE` | Image locator selected by the host wrapper. |
| `E2E_RUNNER_IMAGE_ID`, `E2E_RUNNER_IMAGE_DIGEST` | Observed immutable image identity recorded in the plan. |
| `E2E_EXPECTED_GONKA_SHA` | Full selected Gonka commit expected by the harness. |
| `E2E_EXPECTED_PROTO_SHA` | Independently pinned ABI/format compatibility commit. |
| `E2E_EXPECTED_RUNTIME` | Expected runtime fields, supplied as `FIELD=VALUE` pairs. |
| `E2E_EVIDENCE_MODEL` | Current immutable-source evidence model. |

The runner supplies Kotlin with `E2E_PYTHON`, `E2E_HARNESS`,
`E2E_MARKETPLACE_DIR`, `E2E_GONKA_DIR`, `E2E_CONTEXT`, `E2E_RUN_ID`,
`E2E_DEAL_WASM`, `E2E_FACTORY_WASM`, `E2E_CW20_WASM`, `E2E_CALLER_WASM`,
`E2E_CONTAINER_CONTROL`, `E2E_CONTAINER_CONTROL_STATE_DIR`, and
`E2E_OWNERSHIP_LABEL`. These name the selected sources, run-scoped inputs,
runner-owned executable paths, and container ownership contract.

The Compose project is `forward-e2e`, the default image is
`forward-e2e-runner:local`, and persistent volumes are
`forward-e2e-workspace`, `forward-e2e-docker-root`,
`forward-e2e-cargo-registry-cache`, `forward-e2e-cargo-git-cache`,
`forward-e2e-gradle-cache`, and `forward-e2e-go-cache`.
`E2E_DOCKER_ROOT_VOLUME` or `--docker-root-volume` selects a different daemon
state volume. The runtime fallback is `~/forward-e2e-runtime`.

Linux is the execution platform. Bash and PowerShell wrappers both launch the
Linux runner; PowerShell writes unformatted errors to stderr with the same
exit codes on Linux and Windows.

## Evidence and Source Integrity

A changed catalog, harness, verifier, or asset path changes the hashes pinned
in a lock. Existing plans must be recreated; checks are never relaxed to replay
them. Older overlay/prepared-source formats remain invalid as proof of immutable
sources. Recognising them for rejection does not enable an old command or alias.

Current document schema identifiers, contract event fields, external Cargo
package names, and the ownership label still name the producer/consumer
protocol they implement. They are not alternative spellings of a current input.
The vendored release helper remains byte-identical to the extracted source;
its provenance is recorded in [EXTRACTION.json](../EXTRACTION.json).

The catalog baseline fixture is an offline regression oracle for unchanged
budgets and proof requirements, rather than support for older user inputs.
Historical live-run receipts are absent from this checkout. Fixtures are
synthetic and passing them does not establish live-chain provenance.

## Fixture Replacement Map

The recorded development receipts under `docs/reviews/evidence/` were
deleted, not relocated. Before deletion every consumer was traced and each
verified property was carried over to a self-contained synthetic fixture
whose chain identities are derivations of a labelled SHA-256 seed
(`tests/unit/runner/support/synthetic_evidence.py`; contract in
`tests/fixtures/evidence/README.md`). Every JSON document carries
`"test_fixture_only": true`, which `verify_live_context`,
`verify_go_boundary_report` and `verify_wasm_abi_report` reject outright, so
none of them can be mistaken for a live artefact. The four live-context
documents are identity-scrubbed derivations of the removed receipts (every
identity replaced, the recorded arithmetic kept); the two boundary fixtures
are generated from code and never held a recorded value.

| Removed recorded fixture | Verified property that had to survive | Synthetic replacement |
|---|---|---|
| `a8-c1-exact-e-20260910.json` (full live context of a lock-exact-E run) | Legacy pre-model `source` block is rejected as a downgrade; adapted in memory to `a8.evidence/e2e-immutable-source/2` it proves the `lock-exact-e` checkpoints (`Funded→Locked`, `recipient_locked`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e`, `target_epoch_e_reached`); the `LEGACY_FIXTURE_SOURCE` digest pin in `test_e2e_evidence_model.py` | `tests/fixtures/evidence/lock-exact-epoch-legacy-context.json` — identity-scrubbed derivation of the receipt (addresses, hashes, validator key, timestamps, run id and store paths replaced by seed derivations; balances, heights and epochs kept) |
| `a8-abc-reviewed-20260910.json`, `claim_settle` slice | CW20 withdrawal fault rollbacks, pending-obligation preservation, per-role `withdrawal_txs`, rejected `settle_repeat` | `tests/fixtures/evidence/claim-settlement-fault-phase.json` — identity-scrubbed derivation (same rule) |
| `a8-abc-reviewed-20260910.json`, R1.1 / R2 slices (R1.1 was also read directly by `scripts/tests/test_a8_acceptance.py`) | `Refund` rejected at `E+5` after settlement; four-stage `r2_gift_checkpoint`; `vesting_addition` accounting; proportional gift `release` payouts proven by their own receipts; the harness gate `assert_refund_window_closed` on the recorded `attempt` | `tests/fixtures/evidence/settled-deal-late-refund-and-vested-gift.json` — identity-scrubbed derivation; `tests/unit/harness/test_acceptance_harness.py` reads its `refund_e_plus_5_phase` |
| `g3-terminal-release-repeat-002.json` | Terminal `ReleaseUnlockedGnk` repeat rejected with `NothingToRelease` (`code=5`, `codespace="wasm"`), unchanged Deal/Bank/CW20 snapshots | `tests/fixtures/evidence/terminal-release-repeat.json` — identity-scrubbed derivation |
| `a8-wasm-abi-probe-rerun-20260910.json` (the earlier `a8-wasm-abi-probe-20260910.json` had no test consumer) | 9-case `query_chain` envelope decoding report; `wasm_sha256` binding checked by `verify_wasm_abi_report` | `tests/fixtures/evidence/wasm-query-allowlist-abi.json` — generated from code by `synthetic_evidence.py`; never held a recorded value |
| `a8-go-boundary-20260910/` (`report.json`, `raw/go-test.json`, `build.log`, `raw/exit-code`) | `verify_go_boundary_report`: exit codes, `test_output_sha256` binding to the raw `go test -json` stream, pass events for `GO_BOUNDARY_REQUIRED_TESTS` | `tests/fixtures/evidence/go-query-error-classification/` — generated; its shape is attributed to the post-relocation `scripts/run_go_boundary.py` (`./query_faults`, `go-boundary/` layout), not to the baseline commit, whose `run_a8_go_boundary.py` wrote different paths |
| `a8-claim-expiry-positive-043.json` (D1 claim-expiry refund, target epoch 5) | Refund rejected at `E+1` with the contract's window error, committed at `E+2`; rejected bracket leaves balances unchanged, commit moves exactly the budget from Deal to Buyer with fee and bank unchanged | No committed document. The in-memory D1 contexts in `real_fixtures.py` (`real_claim_expiry_*`) use seed-derived identities, and `test_real_fixtures.py` derives every height, epoch and error text from the context's own `terms` and brackets (`assert_refund_phases_follow_the_declared_window`) instead of comparing to literals copied from the receipt |
| `a8-network-unconfirmed-stable-e1-002.json` (E1 network-unconfirmed refund; was loaded by `test_real_fixtures.py` on the baseline) | Refund rejected at `E+2`, committed at `E+3`; Buyer and Host coincide (catalog limitation of the emergency sweep); `NotFound` transport facts for the absent confirmation | No committed document. In-memory E1 contexts (`real_network_unconfirmed_*`) with seed-derived identities; the same derived-expectation checks as D1 plus `cw20.host == cw20.buyer` in every snapshot and the absence epoch computed from `terms` |
| `a8-b3-foreign-native-004.json` (named only in a `real_fixtures.py` docstring on the baseline; never loaded) | B3 foreign-native preservation: the Deal keeps the foreign denom through release | In-memory context `real_b3_context()`; the B3 genesis fixture account `gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny` is the one product literal kept because it is the wire value shared with the provisioner and the Kotlin test |
| Recorded identities (D1/E1/B3/R2 addresses, transaction hashes, timestamps, run ids) embedded in the baseline `real_fixtures.py` | Claim-expiry, network-unconfirmed, B3, R2 and late-donation in-memory contexts | Derivations: `synthetic_address(doc, role)`, `fixture_tx_hash(doc, label)`, `synthetic_timestamp(n)`, `synthetic_run_id(doc)`; `test_synthetic_evidence.py` applies the same identity checks to the in-memory contexts as to the committed documents |

**Lost on purpose.** The suites no longer regress against *recorded* producer
output. Concretely, three kinds of coverage went away with the receipts and
were not re-created:

- **Producer field-name drift.** A change in a live producer's field names is
  now caught only when the harness tests are updated together with the
  producer (`docs/evidence.md`), not by committed receipts drifting out of
  date. The four scrubbed documents still carry the producer's shape, but
  their shape is asserted by the same tests that would be edited alongside
  the producer.
- **Recorded chain arithmetic as an independent oracle.** The D1 and E1 refund
  heights, epochs and balances used to be literals copied from
  `a8-claim-expiry-positive-043.json` and
  `a8-network-unconfirmed-stable-e1-002.json`; they are now derived from the
  synthetic context's own `terms`. That proves the verifier applies the rule
  consistently, not that a real chain once produced those numbers. The
  scrubbed documents keep their recorded arithmetic, which is the only
  remaining link to a real execution, and it is not identity-bearing.
- **The dated review reports and matrices** (`docs/reviews/*.md`,
  `*-review-*.json`, `a8-matrix-source-register-*.json`) that cited run ids.
  No test depended on them; their claims about a given commit are not
  reproducible from this repository and are not restated anywhere.

No test depends on an old run id, review report or review matrix;
`NoDevelopmentArtifactDependencyTests` in
`tests/unit/runner/test_synthetic_evidence.py` scans `tests/` for the removed
path on every run. Nothing re-archives the deleted receipts: the only record
of them is Git history before the deletion commit.
