# Scenario Catalog and Coverage Reference

This document maps all 24 automated tasks in [`forward_e2e/suite/catalog.py`](../forward_e2e/suite/catalog.py),
their legacy scenario aliases, proof levels, test selectors, evidence scopes,
timeouts, and explicit coverage boundaries.

---

## 1. Execution Profiles

| Profile | Selected Tasks | Count |
|---|---|---:|
| `smoke` | `lock-exact-e` | 1 |
| `boundary` | All 5 tasks in `BOUNDARY_TASKS` (`GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`) | 5 |
| `native` | All 19 live Testermint tasks in `NATIVE_TASKS` (`NATIVE`) | 19 |
| `all` | `BOUNDARY_TASKS` (ordinals 1–5) followed by `NATIVE_TASKS` (ordinals 6–24) | 24 |

---

## 2. Complete 24-Task Catalog

### 2.1 Boundary and Contract-Policy Tasks (`BOUNDARY_TASKS`)

| # | Canonical `task_id` | Legacy Alias | Proof Level | Driver / Selector | Timeout | Coverage Focus |
|---:|---|---|---|---|---:|---|
| 1 | `go-query-error-classification` | `go-boundary` | `GO_BOUNDARY` | `scripts/run_go_boundary.py` (`./query_faults`, `^TestA8GoBoundary`) | 30m | `R4.4` `InvalidResponse`, `R4.5` `InvalidRequest` / `Unknown` / `NoSuchContract` / `NoSuchCode` |
| 2 | `wasm-abi-boundary` | — | `WASM_ABI` | `scripts/test_wasm_query_boundary.mjs` (`tests/contracts/a8-query-boundary`) | 15m | Compiled-Wasm `ExternalQuerier` ABI (`6/6` scopes: `R4.4` `InvalidResponse`, `R4.5` error variants) |
| 3 | `contract-network-unconfirmed-policy` | `ct-network-unconfirmed` | `CONTRACT_TEST` | `cargo test --locked` (`network_unconfirmed_refund_rejects_early_and_unexpected_system_failures`) | 10m | `R4.6` encoding, overflow, and accounting policy |
| 4 | `contract-claim-expiry-policy` | `ct-claim-expiry` | `CONTRACT_TEST` | `cargo test --locked` (`claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow`) | 10m | `R4.6` pristine locked accounting and epoch overflow |
| 5 | `contract-query-fault-policy` | `ct-package-c-policy` | `CONTRACT_TEST` | `cargo test --locked` (18 exact tests, 71 case markers) | 15m | `C2` / `R3`, `E2` policy (`R4.1–R4.3`), `E3` policy (`R5`), `G1` (`R6.3`), `R4.4` `UnsupportedRequest` |

### 2.2 Live Chain Tasks (`NATIVE_TASKS`)

All `NATIVE` tasks run through `scripts/acceptance_harness.py run-live` and
execute a single JUnit method in `MarketplaceContractAcceptanceTests`
([`harness/testermint/`](../harness/testermint/)):

| # | Canonical `task_id` | Legacy Alias | Exact Kotlin Test Method | Evidence Scopes | Timeout (Gradle) | Coverage Focus |
|---:|---|---|---|---|---|---|
| 6 | `funded-claim` | — | `marketplace funded claim settles and releases on real Gonka` | `<top-level>` | 100m (60m) | `A1`, `A2`, `A3` funded claim settlement and release |
| 7 | `network-unconfirmed` | — | `marketplace absent native summary refunds only at emergency deadline` | `network-unconfirmed` | 85m (45m) | `E1` missing native summary emergency refund |
| 8 | `claim-expiry-positive` | — | `marketplace positive unclaimed summary refunds only at claim expiry` | `claim-expiry-positive` | 85m (45m) | `D1` positive unclaimed summary refund at expiry |
| 9 | `claim-expiry-zero` | — | `marketplace zero unclaimed summary refunds only at claim expiry` | `claim-expiry-zero` | 85m (45m) | `D1` zero unclaimed summary refund at expiry |
| 10 | `terminal-release-repeat` | — | `marketplace terminal release repeat is rejected without payout` | `<top-level>` | 85m (45m) | `G3` terminal release idempotency / repeat rejection |
| 11 | `foreign-native-preservation` | `b3-foreign-native` | `marketplace successful release preserves foreign native denom` | `<top-level>` | 85m (45m) | `B3` preservation of foreign native denom (`12345ua8b3foreign`) |
| 12 | `late-donation-after-completed` | — | `marketplace late liquid donations after Completed use cumulative GNK rounding` | `<top-level>` | 85m (45m) | `B2` liquid donations after `Completed` |
| 13 | `lock-exact-e` | — | `marketplace funded lock succeeds exactly at E` | `lock-exact-e` | 85m (45m) | `C1` exact epoch `E` lower lock boundary (`smoke` profile) |
| 14 | `lock-e-plus-4` | — | `marketplace funded lock succeeds exactly at E plus 4` | `lock-e-plus-4` | 85m (45m) | `C1` `E+4` upper inclusive lock boundary |
| 15 | `lock-e-plus-5` | — | `marketplace funded lock rejects exactly at E plus 5` | `lock-e-plus-5` | 85m (45m) | `C1` `E+5` lock rejection / pruning boundary |
| 16 | `refund-boundary-and-vesting-addition` | `package-a-r1-r2` | `marketplace preserves refund boundary and releases a new vested gift` | `r1-refund-e-plus-5`, `r2-vested-gift` | 85m (45m) | `B2` vesting / `R1` refund boundary + `R2` new vested gift |
| 17 | `usdt-withdrawal-failure-recovery` | `package-b-r6-1` | `marketplace settlement commits once and each rejected USDT withdrawal rolls back atomically` | `<top-level>` | 85m (45m) | `G1` / `R6.1` single settlement, then atomic rollback of each CW20-faulted USDT withdrawal |
| 18 | `native-release-rollback-retry` | `package-b-r7-1` | `marketplace native release rejects selected second Bank send then retries once` | `<top-level>` | 85m (45m) | `G2` / `R7.1` native Bank release rollback and retry |
| 19 | `funded-routing-refunds` | — | `marketplace funded routing refunds are isolated and atomic` | `<top-level>`, `routing-mismatch`, `routing-missing` | 85m (45m) | Routing missing/mismatch refunds and Factory isolation |
| 20 | `unfunded-lock-boundaries` | — | `marketplace unfunded lock boundaries preserve buyer absence` | `lock-e-plus-4`, `lock-e-plus-5` | 85m (45m) | Unfunded `Lock` at `E+4` and `E+5` without Buyer |
| 21 | `funded-gas-sweep` | — | `marketplace claimed refund gas sweep is isolated` | `gas-claimed` | 85m (45m) | Claimed refund gas sweep isolation |
| 22 | `no-buyer-claim-expiry` | — | `marketplace no buyer claim expiry preserves buyer absence` | `no-buyer-expired` | 85m (45m) | Claim expiry when no Buyer funded the Deal |
| 23 | `no-sale-vesting-lifecycle` | — | `marketplace no sale vesting lifecycle preserves every asset` | `no-sale` | 100m (60m) | No-sale donations, foreign CW20, and non-empty vesting addition |
| 24 | `emergency-host-only-recovery` | — | `marketplace emergency refund host only release rolls back and retries` | `network-unconfirmed` | 100m (60m) | `R7.2` emergency refund `HostOnly` Bank rollback and retry |

---

## 3. Manual Operator Probe (`wasm-query-allowlist`)

[`harness/wasm_query_allowlist/`](../harness/wasm_query_allowlist/) provides an
unaudited CosmWasm probe (`artifacts/p0_probe.wasm`) and CLI command
(`python3 scripts/acceptance_harness.py wasm-query-allowlist`, legacy alias
`p0-probe`) for live regression testing of Gonka's `AcceptListGrpcQuerier`.

Why it is manual rather than one of the 24 automated catalog tasks:

- `wasm-abi-boundary` (task #2) exercises compiled-Wasm protobuf encoding and
  `ExternalQuerier` responses against a synthetic host in Node.js, not Gonka's
  live `AcceptListGrpcQuerier` router.
- The manual `wasm-query-allowlist` command deploys `p0_probe.wasm` to a live
  chain, verifies the four allowed marketplace query paths
  (`GetCurrentEpoch`, `ListClaimRecipients`,
  `EpochPerformanceSummaryByParticipant`, `TotalVestingAmount`), and verifies
  that `/inference.inference.Query/Params` is rejected with Gonka's exact
  `path is not allowed from the contract` message.
- See [`harness/wasm_query_allowlist/README.md`](../harness/wasm_query_allowlist/README.md)
  for build, checksum verification (`./harness/wasm_query_allowlist/build.sh verify`),
  and invocation instructions.

---

## 4. Known Coverage Boundaries and Verifier Notes

1. **Synthetic Query Faults vs Live Chain Consensus (`contract-query-fault-policy`):**
   Malformed protobuf payloads or arbitrary gRPC system errors from Gonka's own
   querier cannot be triggered on an unmodified Gonka node without patching chain
   code. To preserve the immutable-source invariant, those 71 fault-policy cases
   are verified at `CONTRACT_TEST` level (`contract-query-fault-policy`) and
   boundary levels (`go-query-error-classification`, `wasm-abi-boundary`), while
   reachable bank/CW20 rollback paths (`usdt-withdrawal-failure-recovery`,
   `native-release-rollback-retry`, `emergency-host-only-recovery`) run as full
   `NATIVE` chain scenarios.
2. **Evidence Key Alignment in `usdt-withdrawal-failure-recovery` (`package-b-r6-1`):**
   In [`scripts/acceptance_harness.py`](../scripts/acceptance_harness.py), the
   live `package-b-r6-1` handler records its scenario payload under
   `context["package_b"]["r6_1"]`, whereas `_verify_package_b_r6_1` in
   [`forward_e2e/suite/verifier.py`](../forward_e2e/suite/verifier.py) inspects
   `data.get("package_b_r6_1_usdt_fault")`. Keep this existing producer/verifier
   distinction in mind when inspecting `usdt-withdrawal-failure-recovery`
   evidence or planning coordinated producer/verifier updates.
