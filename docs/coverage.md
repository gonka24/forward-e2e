# Scenario Catalog and Coverage Reference

This document maps all 24 automated tasks in
[`forward_e2e/suite/catalog.py`](../forward_e2e/suite/catalog.py) — their legacy
scenario aliases, proof levels, drivers, evidence scopes, timeouts, catalog
descriptions, expected checkpoints, declared limitations and expected
artifacts — and then maps the contract obligations of the selected
`forward-contracts` revision onto those tasks, stating for each obligation
which of four coverage states applies.

Every table below is derived from `TaskPlan.to_dict()` of the catalog
(`CATALOG_SCHEMA_VERSION = "1.0.0"`); the catalog is hashed into every plan as
`lock.runner.catalog_hash` (`compute_catalog_hash`), so a change to any value
here must come with a new plan.

---

## 1. Execution Profiles

| Profile | Selected Tasks | Count |
|---|---|---:|
| `smoke` | `lock-exact-e` | 1 |
| `boundary` | All 5 tasks in `BOUNDARY_TASKS` (`GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`) | 5 |
| `native` | All 19 live Testermint tasks in `NATIVE_TASKS` (`NATIVE`) | 19 |
| `all` | `BOUNDARY_TASKS` (ordinals 1–5) followed by `NATIVE_TASKS` (ordinals 6–24) | 24 |

Profiles and single tasks are resolved by `resolve_e2e_selection`; a legacy
alias (`LEGACY_SCENARIO_ALIASES`, eight entries) is accepted anywhere a task id
is and is translated to the canonical id by `canonical_task_id` before it is
frozen into the lock. See [`migration.md`](migration.md) for the alias history.

---

## 2. Complete 24-Task Catalog

### 2.1 Boundary and Contract-Policy Tasks (`BOUNDARY_TASKS`)

All five run through `BoundaryTaskAdapter`
([`forward_e2e/suite/adapters.py`](../forward_e2e/suite/adapters.py)), which
dispatches on the canonical task id. None of them starts a chain, so none owes
a `live-context.json`.

| # | Canonical `task_id` | Legacy Alias | Proof Level | Driver (what the adapter executes) | Timeout | Coverage IDs (`coverage_ids`) |
|---:|---|---|---|---|---:|---|
| 1 | `go-query-error-classification` | `go-boundary` | `GO_BOUNDARY` | `python3 scripts/run_go_boundary.py <gonka_dir> <task evidence dir>`: a `docker build` of a Dockerfile generated from the prefix of Gonka's own `inference-chain/Dockerfile` (pinned `golang:1.24.2-alpine3.21` builder), running `go test -mod=mod -tags=muslc -count=1 -json ./query_faults -run 'TestToQuerierResultClassifiesVMSystemErrors\|TestStrictPlanValidation'` against [`harness/go_boundary/query_faults/`](../harness/go_boundary/query_faults/); the `-run` filter is built from `REQUIRED_TESTS`. There is no host-Go path. | 30m | `R4.4 InvalidResponse`, `R4.5 InvalidRequest / Unknown / NoSuchContract / NoSuchCode` |
| 2 | `wasm-abi-boundary` | — | `WASM_ABI` | `cargo build --locked --release --target wasm32-unknown-unknown -p a8-query-boundary` in the contracts snapshot (`CARGO_TARGET_DIR` outside the sources), copy of the artefact to `a8_query_boundary.wasm`, then `node scripts/test_wasm_query_boundary.mjs <wasm> <abi.json>` against a synthetic Node.js host. | 15m | `R4.4 InvalidResponse`, `R4.5 InvalidRequest / Unknown / NoSuchContract / NoSuchCode` |
| 3 | `contract-network-unconfirmed-policy` | `ct-network-unconfirmed` | `CONTRACT_TEST` | `cargo test --locked -p marketplace-deal network_unconfirmed_refund_rejects_early_and_unexpected_system_failures -- --nocapture` | 10m | `R4.6 encoding/overflow/accounting` |
| 4 | `contract-claim-expiry-policy` | `ct-claim-expiry` | `CONTRACT_TEST` | `cargo test --locked -p marketplace-deal claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow -- --nocapture` | 10m | `R4.6 encoding/overflow/accounting` |
| 5 | `contract-query-fault-policy` | `ct-package-c-policy` | `CONTRACT_TEST` | `cargo test --locked -p marketplace-deal -p marketplace-factory c_ -- --nocapture --test-threads=1` (18 exact tests in `PACKAGE_C_POLICY_TESTS`, 71 case markers in `PACKAGE_C_POLICY_CASES`) | 15m | `C2 / R3`, `E2 policy / R4.1-4.3`, `E3 policy / R5`, `G1 / R6.3`, `R4.4 UnsupportedRequest` |

The verifier side of each row
([`forward_e2e/suite/verifier.py`](../forward_e2e/suite/verifier.py)):

- `verify_go_boundary_report` requires `report.json` with status `PASS`, Docker
  and `go test` exit codes of zero, both tests in `GO_BOUNDARY_REQUIRED_TESTS`
  passed in the `-json` stream, and a `test_output_sha256` that matches
  `raw/go-test.json`. The report's own `level` field reads
  "Go classification and JSON roundtrip, not FFI", which is also the claim.
- `verify_wasm_abi_report` requires status `PASS`, **exactly the nine cases**
  in `WASM_ABI_REQUIRED_CASES` (`malformed-envelope`,
  `invalid-envelope-shape`, `invalid_request`, `unknown`, `no_such_contract`,
  `no_such_code`, `unsupported_request`, `contract-error-control`,
  `success-control`; checkpoint `9_abi_cases_passed`) and a `wasm_sha256`
  equal to the hash of the collected `a8_query_boundary.wasm`. There is no
  "scopes" count in this report.
- The three `CONTRACT_TEST` rows are graded from the `cargo test` log named in
  `expected_artifacts` (exit code, the named test(s) passed, and for task 5
  the 18 tests and 71 case markers).

### 2.2 Live Chain Tasks (`NATIVE_TASKS`)

All `NATIVE` tasks run through `NativeTaskAdapter`, which executes
`python3 scripts/acceptance_harness.py run-live --marketplace-dir … --gonka-dir …
--evidence-dir <snapshot>/evidence --run-id <task run id> --scenario <task_id>
--timeout-minutes <gradle_timeout_minutes>` in the contracts snapshot. `run_live`
starts the Gonka Testermint network from the selected Gonka's unmodified
sources and executes exactly one JUnit method in
`MarketplaceContractAcceptanceTests`
([`harness/testermint/`](../harness/testermint/)), which re-enters the harness
for every chain action.

| # | Canonical `task_id` | Legacy Alias | Exact Kotlin Test Method | Evidence Scopes | Timeout (Gradle) | Coverage IDs (`coverage_ids`) |
|---:|---|---|---|---|---|---|
| 6 | `funded-claim` | — | `marketplace funded claim settles and releases on real Gonka` | `<top-level>` | 100m (60m) | `A1`, `A2`, `A3` |
| 7 | `network-unconfirmed` | — | `marketplace absent native summary refunds only at emergency deadline` | `network-unconfirmed` | 85m (45m) | `E1 missing summary` |
| 8 | `claim-expiry-positive` | — | `marketplace positive unclaimed summary refunds only at claim expiry` | `claim-expiry-positive` | 85m (45m) | `D1 positive` |
| 9 | `claim-expiry-zero` | — | `marketplace zero unclaimed summary refunds only at claim expiry` | `claim-expiry-zero` | 85m (45m) | `D1 zero` |
| 10 | `terminal-release-repeat` | — | `marketplace terminal release repeat is rejected without payout` | `<top-level>` | 85m (45m) | `G3` |
| 11 | `foreign-native-preservation` | `b3-foreign-native` | `marketplace successful release preserves foreign native denom` | `<top-level>` | 85m (45m) | `B3` |
| 12 | `late-donation-after-completed` | — | `marketplace late liquid donations after Completed use cumulative GNK rounding` | `<top-level>` | 85m (45m) | `B2 liquid` |
| 13 | `lock-exact-e` | — | `marketplace funded lock succeeds exactly at E` | `lock-exact-e` | 85m (45m) | `C1 exact E lower boundary` (`smoke` profile) |
| 14 | `lock-e-plus-4` | — | `marketplace funded lock succeeds exactly at E plus 4` | `lock-e-plus-4` | 85m (45m) | `C1 E+4 Lock` |
| 15 | `lock-e-plus-5` | — | `marketplace funded lock rejects exactly at E plus 5` | `lock-e-plus-5` | 85m (45m) | `C1 E+5 Lock/pruning` |
| 16 | `refund-boundary-and-vesting-addition` | `package-a-r1-r2` | `marketplace preserves refund boundary and releases a new vested gift` | `r1-refund-e-plus-5`, `r2-vested-gift` | 85m (45m) | `B2 vesting / R2` |
| 17 | `usdt-withdrawal-failure-recovery` | `package-b-r6-1` | `marketplace settlement commits once and each rejected USDT withdrawal rolls back atomically` | `<top-level>` | 85m (45m) | `G1 / R6.1` |
| 18 | `native-release-rollback-retry` | `package-b-r7-1` | `marketplace native release rejects selected second Bank send then retries once` | `<top-level>` | 85m (45m) | `G2 / R7.1` |
| 19 | `funded-routing-refunds` | — | `marketplace funded routing refunds are isolated and atomic` | `<top-level>`, `routing-mismatch`, `routing-missing` | 85m (45m) | `routing missing/mismatch`, `Factory isolation` |
| 20 | `unfunded-lock-boundaries` | — | `marketplace unfunded lock boundaries preserve buyer absence` | `lock-e-plus-4`, `lock-e-plus-5` | 85m (45m) | `unfunded Lock E+4/E+5` |
| 21 | `funded-gas-sweep` | — | `marketplace claimed refund gas sweep is isolated` | `gas-claimed` | 85m (45m) | `claimed refund gas sweep` |
| 22 | `no-buyer-claim-expiry` | — | `marketplace no buyer claim expiry preserves buyer absence` | `no-buyer-expired` | 85m (45m) | `no-Buyer claim expiry` |
| 23 | `no-sale-vesting-lifecycle` | — | `marketplace no sale vesting lifecycle preserves every asset` | `no-sale` | 100m (60m) | `no-sale donations / foreign CW20 / non-empty vesting addition` |
| 24 | `emergency-host-only-recovery` | — | `marketplace emergency refund host only release rolls back and retries` | `network-unconfirmed` | 100m (60m) | `emergency refund HostOnly Bank rollback/retry` |

"Timeout" is `timeout_minutes` (the task budget enforced by the suite runner,
also exported as `stage_timeout_seconds`); "(Gradle)" is
`gradle_timeout_minutes`, which `NativeTaskAdapter` passes to the harness as
`--timeout-minutes`. Boundary tasks carry the model default of 45 for that
field (`TaskPlan.gradle_timeout_minutes`) but nothing reads it for them. Both
values are frozen into the lock (`_limits_section` in
`forward_e2e/execution/planner.py`).

### 2.3 Catalog description, expected checkpoints and declared limitations

`description`, `expected_checkpoints` and `limitations` are catalog fields.
Checkpoints are the names the verifier must *observe* in the evidence for the
task to pass; `phase:<name>` entries are producer phases recorded by the
harness in `live-context.json`, the others are derived facts the verifier
credits only after its own cross-checks (see §5.2 for one worked example).
`limitations` is the catalog's own statement of what the task does **not**
prove; `TaskPlan.to_dict()` carries it unchanged into `suite-plan.json`, and
`OfflineReporter.build_coverage_json` into `coverage.json`.

| # | `task_id` | `description` | `expected_checkpoints` | `limitations` |
|---:|---|---|---|---|
| 1 | `go-query-error-classification` | Go classification and JSON roundtrip tests in pinned Docker container | `docker_build_completed`, `TestToQuerierResultClassifiesVMSystemErrors_passed`, `TestStrictPlanValidation_passed`, `go_exit_zero` | Proves classification/serialization only; not libwasmvm FFI or contract error handling |
| 2 | `wasm-abi-boundary` | 9-case CosmWasm ExternalQuerier ABI query_chain probe with synthetic Node host | `wasm_compiled`, `9_abi_cases_passed`, `status_pass` | Synthetic host; proves Rust query_chain ABI decoder, not production Go dispatcher/keeper reachability |
| 3 | `contract-network-unconfirmed-policy` | CT: network_unconfirmed_refund_rejects_early_and_unexpected_system_failures | `cargo_test_exit_zero`, `test_network_unconfirmed_refund_passed`, `non_zero_tests_executed` | Contract decision policy unit test, not live fault injection |
| 4 | `contract-claim-expiry-policy` | CT: claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow | `cargo_test_exit_zero`, `test_claim_expiry_checks_passed`, `non_zero_tests_executed` | Contract decision policy unit test, not live fault injection |
| 5 | `contract-query-fault-policy` | Contract-policy and cw-multi-test replacement for 71 query fault cases | `cargo_test_exit_zero`, `18_exact_tests_passed`, `71_case_markers_passed` | Synthetic query boundaries; not production Gonka query-fault reachability or native E2E. Modeled claim observations do not prove native claim/vesting ledger writes. R7.2 remains covered by emergency-host-only-recovery at native level |
| 6 | `funded-claim` | Funded claim settles and releases on real Gonka | `phase:lock`, `phase:claim_prepared`, `phase:usdt_fault_rollback`, `phase:claim_settle`, `phase:release`, `state_transition:Funded->Locked`, `recipient_locked=true`, `single_settlement_succeeds`, `funded_claim_path_verified`, `release_completed`, `funded_release_lifecycle_verified` | Single funded happy-path test; does not cover negative paths, no-sale, or gas sweep |
| 7 | `network-unconfirmed` | Absent native summary refunds only at emergency deadline | `phase:native_summary_absent`, `phase:refund_rejected`, `phase:refund_committed`, `emergency_deadline_reached`, `emergency_refund_rejected_at_e_plus_2`, `emergency_refund_committed` | Requires coincident Buyer/Host addresses for emergency sweep |
| 8 | `claim-expiry-positive` | Positive unclaimed summary refunds only at claim expiry | `phase:native_unclaimed_precondition`, `phase:refund_rejected`, `phase:refund_committed`, `positive_unclaimed_precondition`, `early_refund_rejected_at_e_plus_1`, `claim_expiry_refund_committed_at_e_plus_2` | Validates positive unclaimed state at claim expiry |
| 9 | `claim-expiry-zero` | Zero unclaimed summary refunds only at claim expiry | `phase:native_unclaimed_precondition`, `phase:refund_rejected`, `phase:refund_committed`, `zero_unclaimed_genesis`, `early_refund_rejected_at_e_plus_1`, `claim_expiry_refund_committed_at_e_plus_2` | Requires special genesis configuration; not a production-config reachability claim |
| 10 | `terminal-release-repeat` | Terminal release repeat is rejected without additional payout | `phase:release`, `phase:terminal_release_repeat`, `terminal_rejection_nothing_to_release`, `state_entitlements_unchanged`, `no_deal_bank_transfer` | Asserts an included NothingToRelease rejection and unchanged state and balances |
| 11 | `foreign-native-preservation` | Successful release preserves foreign native denom | `phase:b3_foreign_native_successful_release`, `foreign_native_funding_verified`, `foreign_native_balance_preserved`, `release_completed` | Preserves foreign native denom alongside GNK |
| 12 | `late-donation-after-completed` | Late liquid donations after Completed use cumulative GNK rounding | `phase:release`, `phase:late_completed_donation_release`, `two_liquid_donations_verified`, `cumulative_rounding_asserted` | Proves liquid donation rounding math on completed deals |
| 13 | `lock-exact-e` | Funded lock succeeds exactly at E (lower boundary) | `phase:lock_exact_e`, `state_transition:Funded->Locked`, `recipient_locked=true`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e`, `target_epoch_e_reached` | Waits for epoch E (up to 600s). Live network run, not a unit test. |
| 14 | `lock-e-plus-4` | Funded lock succeeds exactly at E plus 4 | `phase:lock_e_plus_4`, `state_transition:Funded->Locked`, `recipient_locked=true`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e_plus_4`, `target_epoch_e_plus_4_reached` | Upper valid lock window boundary |
| 15 | `lock-e-plus-5` | Funded lock rejects exactly at E plus 5 | `phase:lock_e_plus_5_rejected`, `lock_window_closed`, `deal_remains_funded`, `target_epoch_e_plus_5_reached` | First expired lock window block; asserts exact error |
| 16 | `refund-boundary-and-vesting-addition` | Preserves refund boundary and releases a new vested gift | `phase:r1_1_refund_e_plus_5_rejected`, `phase:vesting_addition`, `phase:r2_gift_checkpoint`, `r1_refund_boundary_asserted`, `vesting_addition_verified`, `r2_gift_fully_locked`, `r2_gift_first_unlocked`, `r2_gift_payout_receipts_verified`, `r2_gift_final_released` | Covers R1 refund boundary and R2 streamvesting gift mechanics |
| 17 | `usdt-withdrawal-failure-recovery` | Settlement commits once; each rejected USDT withdrawal rolls back atomically | `phase:claim_settle`, `cw20_three_send_rejections_asserted`, `settlement_atomic_rollback_verified`, `single_settlement_succeeds` | Injected CW20 transfer failures across Host/fee/Buyer send positions |
| 18 | `native-release-rollback-retry` | Native release rejects selected second Bank send then retries once | `phase:native_bank_release_fault_plan`, `phase:native_bank_release_rollback`, `phase:native_bank_release_retry`, `bank_send_rejection_asserted`, `bank_send_retry_succeeds`, `bank_rollback_atomic`, `r7_1_two_bank_sends_verified` | Bank send rollback and retry under native governance restriction |
| 19 | `funded-routing-refunds` | Funded routing mismatch and missing refunds are atomic and Factory-isolated | `phase:routing_mutation`, `phase:refund_committed`, `phase:factory_isolation`, `factory_isolation_verified`, `routing_refund_committed`, `routing_refund_fault_rollback`, `both_routing_refunds_verified` | Exact native routing rows on two funded Deals in one Factory |
| 20 | `unfunded-lock-boundaries` | Unfunded Deals preserve Buyer absence at exact Lock E+4/E+5 boundaries | `phase:lock`, `phase:lock_rejected`, `unfunded_lock_boundaries_verified` | Buyer-absent Open Deal boundary variant |
| 21 | `funded-gas-sweep` | Claimed positive reward refund gas sweep on a pristine Locked Deal | `phase:native_auto_claim`, `phase:lock`, `phase:claimed_refund_gas_sweep`, `claimed_refund_gas_sweep_verified` | Direct and caller/submessage gas behavior after authoritative claimed=true |
| 22 | `no-buyer-claim-expiry` | No-Buyer positive unclaimed summary expires without inventing a Buyer payout | `phase:native_unclaimed_precondition`, `phase:refund_rejected`, `phase:refund_committed`, `early_refund_rejected_at_e_plus_1`, `claim_expiry_refund_committed_at_e_plus_2` | Buyer is explicitly absent; positive summary remains claimed=false |
| 23 | `no-sale-vesting-lifecycle` | No-sale native reward preserves donations, foreign CW20 and old/new vesting | `phase:native_auto_claim`, `phase:lock`, `phase:liquid_donation`, `phase:foreign_cw20_contamination`, `phase:settle_claim`, `phase:early_release_rejected`, `phase:vesting_snapshot`, `vesting_addition_verified`, `release_completed`, `early_release_unavailable_verified`, `no_sale_vesting_lifecycle_verified` | Two Release attempts cover all tranches and the terminal no-op/rejection |
| 24 | `emergency-host-only-recovery` | Emergency refund terminal Deal releases a later donation to Host atomically | `phase:lock`, `phase:native_summary_absent`, `phase:refund_rejected`, `phase:refund_committed`, `phase:liquid_donation`, `phase:native_bank_release_rollback`, `phase:native_bank_release_retry`, `emergency_refund_rejected_at_e_plus_2`, `emergency_refund_committed`, `bank_send_rejection_asserted`, `bank_rollback_atomic`, `bank_send_retry_succeeds` | Single Host Bank send after terminal emergency refund |

### 2.4 Expected artifacts by proof level

`expected_artifacts` is identical for every task of a proof level except that
each `CONTRACT_TEST` log is named after its task. A missing expected artefact
is reported as missing, never inferred from a neighbour
(`locate_task_evidence` in `forward_e2e/execution/evidence.py`). Every path a
producer writes must also match `ALLOWED_ARTIFACT_PATTERNS` in
`forward_e2e/suite/collector.py` or it is never copied into the suite.

| Proof level | `expected_artifacts` | Where they land in the run package |
|---|---|---|
| `NATIVE` | `live-context.json`, `junit/TEST-MarketplaceContractAcceptanceTests.xml`, `testermint.log` | `suite/<run-id>/runs/<task-run-id>/evidence/<task-run-id>/…` — the harness is handed `<snapshot>/evidence` and appends the run id itself, so the id appears twice (`EvidenceRequirement.live_context_candidates`). `source-immutability.json`, `external-harness/`, `network/`, `container-control/` and `testermint-junit/` sit next to `live-context.json`. |
| `GO_BOUNDARY` | `build.log`, `raw/go-test.json`, `raw/exit-code`, `report.json` | `suite/<run-id>/runs/<task-run-id>/evidence/…` (written by `scripts/run_go_boundary.py` into the task evidence directory; `raw/` is the Docker `--output type=local` export) |
| `WASM_ABI` | `abi.json`, `a8_query_boundary.wasm` | same task evidence directory, plus `wasm-build.log` and `node-probe.log` |
| `CONTRACT_TEST` | `<task_id>.log` | same task evidence directory |

The task run id is `make_task_run_id(suite_id, ordinal, task_id)`
(`forward_e2e/suite/models.py`): the suite id truncated to 30 characters, the
two-digit ordinal and the task id truncated.

---

## 3. Obligation Mapping

### 3.1 The four coverage states

Every obligation below is in exactly one of these states. The distinction
matters because this repository contains a *runner*, and a runner that can
check something is not evidence that it was checked for the commit you care
about.

| State | Meaning | Where to look |
|---|---|---|
| **Runnable check** | A catalog task (and its verifier rule) exists in this runner and executes against whatever `gonka_sha` / `marketplace_commit_sha` pair a plan selects. It is a capability, not a result. | `forward_e2e/suite/catalog.py`, `forward_e2e/suite/verifier.py`, this section |
| **Result for a specific SHA pair** | A verdict exists only inside a run package written by `run` / `rerun`: `e2e-run-result.json` (schema `e2e/run-result/2`, `evaluate_run` in `forward_e2e/execution/outcome.py`) bound to the lock's two full SHAs. **This repository commits no run package and no live result**; the only committed evidence documents are the synthetic fixtures under `tests/fixtures/evidence/`, every one of which carries `test_fixture_only: true` and is refused by `verify_live_context`. For every row below this state is therefore available only from a run you (or your CI) executed. | the run package under your output directory (`docs/operations.md` §4) |
| **Historical evidence** | A run package whose documents declare `a8.evidence/e2e-prepared-build/1` or `a8.evidence/legacy-overlay/1`, or whose lock is not `e2e/run-lock/2`. `classify_provenance_model` grades it `historical-prepared-build`, which carries the blocking `HISTORICAL_PREPARED_BUILD` finding: readable with `report`, never immutable-source proof. None is committed to this repository either. | `forward_e2e/execution/outcome.py`, `docs/evidence.md` §2 |
| **Not covered** | No task, verifier rule or probe in this runner exercises the obligation. A `PASSED` run says nothing about it. | this section |

### 3.2 Lifecycle operations of `forward-contracts`

Obligations are taken from the lifecycle table in
`docs/contract-behavior.md` and from `docs/validation.md` of
`gonka24/forward-contracts` at
`58f41c2e6976002fe25b8ec1548d8599015c07e6` (the contracts revision this
document was checked against; select your own revision by full SHA when you
plan). "Exercised by" names the harness subcommand each NATIVE task re-enters
(`scripts/acceptance_harness.py`) and the catalog checkpoints that bind the
claim.

| Contract operation / obligation | State | Exercised by | What is **not** shown |
|---|---|---|---|
| CW20 `Send` with `fund` hook (`Open` → `Funded`) | Runnable check (`NATIVE`) | The funding step of every funded NATIVE task (the harness `fund` hook executes the CW20 `send`); `foreign-native-preservation` adds `foreign_native_funding_verified`. | No catalog checkpoint names a *rejected* funding attempt (wrong token, wrong budget, `current >= E`, repeated funding). |
| `Lock` (`E <= current < E+5`, exact routing) | Runnable check (`NATIVE`) | `lock-exact-e`, `lock-e-plus-4` (accepted; `lock_tx_included_in_epoch_e*`, `state_transition:Funded->Locked`), `lock-e-plus-5` (rejected; `lock_window_closed`, `deal_remains_funded`), `unfunded-lock-boundaries` (Buyer-absent variant). | — |
| `Cancel` (`Open`, no Buyer; Host before E, anyone in `E..E+4`) | **Not covered** | Nothing: no `cancel` execute message is sent anywhere in `scripts/` or `harness/`. | The whole operation, including the "exact routing still rejects Host cancellation before E" rule. |
| `SettleClaim` (`Locked` + stored proof + exact `claimed=true` summary) | Runnable check (`NATIVE`, plus `CONTRACT_TEST` policy) | `claim-settle` in `funded-claim`, `usdt-withdrawal-failure-recovery`, `native-release-rollback-retry`, `late-donation-after-completed`, `foreign-native-preservation`, `terminal-release-repeat`, `no-sale-vesting-lifecycle` (`single_settlement_succeeds`, `phase:settle_claim`); the repeat attempt after settlement is recorded as `settle_repeat`. Policy rejections: `contract-query-fault-policy` (`c_policy_*`). | — |
| Native reward claim as a separate chain action (not a Deal execute) | Runnable check (`NATIVE`) | `claim-settle` submits `inferenced tx inference claim-rewards` with the Host key, then reads `show-epoch-performance-summary-by-participant` (`phase:claim_prepared`); `funded-gas-sweep` and `no-sale-vesting-lifecycle` record `phase:native_auto_claim` with the recipient routing query, Deal bank balance and streamvesting total. | `contract-query-fault-policy`'s own limitation: its modeled claim observations do not prove native claim/vesting ledger writes. |
| `WithdrawUsdt` (pull payment; clears one debt, one CW20 transfer, status unchanged) | Runnable check (`NATIVE`) | `claim-settle` executes `withdraw_usdt {role}` per recipient (`recorded_withdrawals`, `verify_recorded_settlement`) and asserts the Deal's CW20 balance is zero and the per-role deltas match the oracle (`assert_claim_settlement_matches_oracle`). Fault injection per recipient: `usdt-withdrawal-failure-recovery` (positions 1,2,3) and `funded-claim` (position 2, `--fault-retry`); see §5.2. | Withdrawal from a state other than the settled one, and a withdrawal by a third-party caller, are not separately asserted. |
| `Refund` (routing: `Funded`, pristine, `E <= current < E+5`, routing proves missing/mismatch) | Runnable check (`NATIVE`, plus `CONTRACT_TEST`) | `funded-routing-refunds` (`routing_refund_committed`, `routing_refund_fault_rollback`, `factory_isolation_verified`, `both_routing_refunds_verified`); E+5 rejection in `refund-boundary-and-vesting-addition` (`r1_refund_boundary_asserted`). Routing fault policy: `contract-query-fault-policy` (`c_routing_*`). | — |
| `Refund` (claim expiry: `current >= E+2`, exact `claimed=false` summary; Buyer present → `Refunded`, absent → `Expired`) | Runnable check (`NATIVE`, plus `CONTRACT_TEST`) | `claim-expiry-positive`, `claim-expiry-zero` (`early_refund_rejected_at_e_plus_1`, `claim_expiry_refund_committed_at_e_plus_2`), `no-buyer-claim-expiry` (Buyer absent), `funded-gas-sweep` (refund attempt against an authoritative `claimed=true` summary). Policy: `contract-claim-expiry-policy`. | `claim-expiry-zero` needs a special genesis (catalog limitation), so it is not a production-configuration reachability claim. |
| `Refund` (network unconfirmed: `current >= E+3`, explicitly permitted summary error) | Runnable check (`NATIVE`, plus `CONTRACT_TEST`) | `network-unconfirmed`, `emergency-host-only-recovery` (`emergency_refund_rejected_at_e_plus_2`, `emergency_refund_committed`; the API container is stopped through `harness/container_control.py` to make the summary unavailable). Policy: `contract-network-unconfirmed-policy`, `contract-query-fault-policy`. | Which *other* errors Gonka can produce on an unmodified node is not enumerated by a live task (see §5.1). |
| `ReleaseUnlockedGnk` (terminal states; frozen policy/counters; full bank balance) | Runnable check (`NATIVE`) | `funded-claim` (`release_completed`, `funded_release_lifecycle_verified`), `terminal-release-repeat` (`terminal_rejection_nothing_to_release`, `state_entitlements_unchanged`), `foreign-native-preservation`, `late-donation-after-completed` (`cumulative_rounding_asserted`), `refund-boundary-and-vesting-addition` (R2 gift tranches), `no-sale-vesting-lifecycle` (`early_release_unavailable_verified`), `native-release-rollback-retry` and `emergency-host-only-recovery` (Bank send rollback and retry). | — |
| `ForwardExcessGnk` (`Completed` only; release alias with identical shares) | **Not covered** | Nothing: no `forward_excess_gnk` execute message is sent anywhere in `scripts/` or `harness/`. | The whole operation. |
| Streamvesting additions and vested gifts | Runnable check (`NATIVE`) | `refund-boundary-and-vesting-addition` (`vesting_addition_verified`, `r2_gift_*`), `no-sale-vesting-lifecycle` (`phase:vesting_snapshot`), `funded-claim` (release across both native tranches). | — |
| Donations after terminal states | Runnable check (`NATIVE`) | `late-donation-after-completed`, `no-sale-vesting-lifecycle`, `emergency-host-only-recovery` (`phase:liquid_donation`). | — |
| Factory isolation of Deals | Runnable check (`NATIVE`) | `funded-routing-refunds` (`factory_isolation_verified`). | — |

### 3.3 Query-fault and boundary obligations

`forward-contracts/docs/validation.md` keeps 73 query-fault checks alive: 71
synthetic contract/ledger-policy cases and two native HostOnly recovery cases,
and states that Go/VM probes "establish only the boundary they actually
execute".

| Obligation | State | Exercised by | What is **not** shown |
|---|---|---|---|
| 71 synthetic policy cases (`c_policy_*`, `c_routing_*`, two ledger tests) | Runnable check (`CONTRACT_TEST`) | `contract-query-fault-policy`: `PACKAGE_C_POLICY_TESTS` (18) and `PACKAGE_C_POLICY_CASES` (71 = `PACKAGE_C_REQUIRED_CASES` minus the two `r7.2` markers). | Reachability of any of those faults on production Gonka; Cosmos SDK / wasmd / FFI behaviour (catalog limitation). |
| The two native HostOnly recovery cases (`R7.2`) | Runnable check (`NATIVE`) | `emergency-host-only-recovery` (`bank_send_rejection_asserted`, `bank_rollback_atomic`, `bank_send_retry_succeeds`). | — |
| Go querier error classification (`R4.4`, `R4.5`) | Runnable check (`GO_BOUNDARY`) | `go-query-error-classification`. | libwasmvm FFI and contract-side error handling (the report's own `level` field says so). |
| Compiled-Wasm `query_chain` ABI decoding (`R4.4`, `R4.5`) | Runnable check (`WASM_ABI`) | `wasm-abi-boundary` (nine cases). | Gonka's production dispatcher and keeper reachability; the probe host is synthetic Node.js. |
| Gonka `AcceptListGrpcQuerier` allowlist: the four marketplace query paths are allowed and `/inference.inference.Query/Params` is denied | **Not covered by any catalog task** | Only the manual probe in §4, which has no supported execution environment in this runner. | Whether the selected Gonka's allowlist still matches the contracts' queries. A `PASSED` run does not assert this. |

---

## 4. Manual Operator Probe (`wasm-query-allowlist`)

[`harness/wasm_query_allowlist/`](../harness/wasm_query_allowlist/) provides an
unaudited CosmWasm probe (`artifacts/p0_probe.wasm`) and a harness subcommand
(`python3 scripts/acceptance_harness.py wasm-query-allowlist`; `p0-probe` is a
deprecated alias that prints a notice) for regression testing of Gonka's
`AcceptListGrpcQuerier`. `wasm_query_allowlist` stores the probe, instantiates
it as `a8-p0-probe-<run_id>`, checks the four allowed paths (`GetCurrentEpoch`,
`ListClaimRecipients`, `EpochPerformanceSummaryByParticipant`,
`TotalVestingAmount`), requires that `/inference.inference.Query/Params` is
rejected with Gonka's exact `'<path>' path is not allowed from the contract`
text, and appends the phase `p0_wasm_grpc_allowlist` to the context it was
given.

Why it is manual rather than one of the 24 automated catalog tasks:

- `wasm-abi-boundary` (task #2) exercises compiled-Wasm protobuf encoding and
  `ExternalQuerier` responses against a synthetic host in Node.js, not Gonka's
  live `AcceptListGrpcQuerier` router. The two must not be conflated: the
  catalog task says nothing about the allowlist.
- The probe is not in the E2E CLI's `SUBCOMMANDS` and no catalog task runs it,
  so no plan, lock, verifier rule or report ever includes its result.

Where it can run, honestly stated:

- It needs a bootstrapped `--context` (a live context with `chain.chain_id`,
  `run_id`, `accounts.host`, `contracts.deal` and `terms.target_epoch`) and
  `--wasm`, and it talks to the chain with `docker exec -i genesis-node
  inferenced …` (`DockerGonka`, `DEFAULT_NODE = "genesis-node"`).
- That `genesis-node` container exists only inside the runner's private
  Docker daemon while a NATIVE task is executing, and is torn down by the
  ownership cleanup at the end of the task (`perform_runtime_cleanup`). It is
  never reachable from the host, and the runner entrypoint always executes the
  E2E CLI, which has no subcommand for the probe.
- Consequently there is **no supported path** to run the probe today: the only
  conceivable environment is a shell inside the runner container during a
  NATIVE task, which nothing automates or documents. Treat the probe as a
  maintained source artefact and the allowlist obligation as *not covered*
  (§3.3) until a catalog task exists for it.
- Build and checksum verification of the probe artefact itself
  (`./harness/wasm_query_allowlist/build.sh verify`) does run offline; see
  [`harness/wasm_query_allowlist/README.md`](../harness/wasm_query_allowlist/README.md).

---

## 5. Known Coverage Boundaries and Verifier Notes

### 5.1 Synthetic query faults vs live chain consensus

Malformed protobuf payloads or arbitrary gRPC system errors from Gonka's own
querier cannot be triggered on an unmodified Gonka node without patching chain
code. To preserve the immutable-source invariant, those 71 fault-policy cases
are verified at `CONTRACT_TEST` level (`contract-query-fault-policy`) and at
the two boundary levels (`go-query-error-classification`,
`wasm-abi-boundary`), while the reachable bank/CW20 rollback paths
(`usdt-withdrawal-failure-recovery`, `native-release-rollback-retry`,
`emergency-host-only-recovery`) run as full `NATIVE` chain scenarios. The live
fault the NATIVE tasks *can* inject is the one the harness controls: a CW20
transfer failure configured in the test token
(`configure_cw20_transfer_failure`), a Bank send restriction for the native
release, and an unavailable API container for the network-unconfirmed path.

### 5.2 `usdt-withdrawal-failure-recovery` (`package-b-r6-1`): producer, verifier and vocabulary

The complete R6.1 trace, Kotlin → harness → evidence → verifier:

1. `marketplace settlement commits once and each rejected USDT withdrawal rolls
   back atomically` (`harness/testermint/src/test/kotlin/MarketplaceContractAcceptanceTests.kt`)
   locks the Deal, stops the API container, waits for `CLAIM_REWARDS` and
   re-enters the harness with `claim-settle … --cw20-fault-positions 1,2,3`.
2. `claim_settle` (`scripts/acceptance_harness.py`) settles **once**
   (`settle_claim`), reads the pending obligations (`usdt_payments` smart
   query) and then, for each target from `cw20_settlement_fault_targets`
   (1 = Host, 2 = fee recipient, 3 = Buyer), configures a CW20 transfer failure
   for that recipient (`configure_cw20_transfer_failure`), executes
   **`withdraw_usdt {role}`** — a single pull-payment withdrawal — asserts that
   the attempt failed with the injected error (`assert_injected_cw20_failure`)
   and that both the balance snapshot **and** the pending `usdt_payments` are
   unchanged, clears the fault and records the phase `usdt_fault_rollback`.
   Afterwards the withdrawals are completed for real (`recorded_withdrawals`,
   `verify_recorded_settlement`), the Deal's CW20 balance must be zero, the
   per-role deltas must match the oracle
   (`assert_claim_settlement_matches_oracle`), a repeat settlement attempt is
   recorded as `settle_repeat`, and the phase `claim_settle` is written with
   `settlement_payments`, `withdrawal_txs` and `cw20_fault_rollbacks`.
3. `verify_live_context` (`forward_e2e/suite/verifier.py`) handles the
   `claim_settle` phase by requiring an included `settle_tx` and
   `_validate_cw20_fault_rollbacks(cw20_faults, phase, scope,
   required_positions)` with `required_positions = (1, 2, 3)` for this task
   (`(2,)` for `funded-claim`, whose Kotlin test passes `--fault-retry`
   instead). Only then does it credit `single_settlement_succeeds`, and for the
   three-position case `cw20_three_send_rejections_asserted` and
   `settlement_atomic_rollback_verified` — the catalog's
   `expected_checkpoints` for task 17.

**Determination: a name-only discrepancy, with no functional divergence.** The
producer and the verifier already test the contracts' pull-payment model —
independent, recipient-selected `WithdrawUsdt` calls, each rolling back on its
own — and nothing in either still assumes a settlement that pushes three
transfers in one transaction. What is historical is only vocabulary: the
checkpoint names `cw20_three_send_rejections_asserted` /
`settlement_atomic_rollback_verified` (the catalog keeps wire identifiers in
their historical spelling on purpose, so existing locks and evidence stay
comparable), the harness error text "did not roll back atomically for send #",
and the alias `package-b-r6-1`. Do not "fix" these names in isolation: the
checkpoint strings are in the catalog hash and in every verifier rule, so
renaming them is a coordinated catalog, verifier, fixture and documentation
change. Earlier revisions of this document described a
`context["package_b"]["r6_1"]` payload and a `_verify_package_b_r6_1`
function; neither has ever existed in this repository.
