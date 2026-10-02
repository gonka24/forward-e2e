# A8 Coverage Map and Verification Traceability

authoritative matrix: `docs/reviews/a8-final-coverage-matrix.md`
Date: 2026-09-24
Schema Version: 1.0.0

> [!NOTE]
> **Proof Level Separation Notice**
> The four verification levels (Native Testermint, Go classification/JSON, compiled-Wasm synthetic host ABI, and Contract Tests)
> evaluate distinct boundary invariants. They do not substitute for one another, and synthetic or contract tests do NOT prove
> production Go dispatcher, keeper, or libwasmvm FFI reachability.

---

## 1. Traceability Table

The catalog currently contains 19 native tasks and 5 boundary tasks. Runnable coverage below does not award a new PASS or acceptance verdict.

For every item in `docs/reviews/a8-final-coverage-matrix.md`, this table records the runnable A8 selector, checkpoint, or explicit classification.

**Receipt scope:** A row marked PASS refers only to the exact checkpoint/evidence
named in that row; it does not imply the enclosing diagnostic run passed. For
example, `a8-negative-017-diagnostic.json` has top-level status `failed` after
later harness-oracle failures, even though its `completed_before_failure`
section records earlier routing-refund and other checkpoints. Likewise, the C
review supports 73 native cases across a 72-row automated snapshot plus a
separately recovered R7.2 retry; its JUnit/package finalization timed out. The
row-level evidence and the run-level outcome must remain distinct.

| Matrix ID | Level | Authoritative Historical Status | Runnable A8 Selector / Task ID | Classification / Gap Explanation |
|---|---|---|---|---|
| **P0** | N | PASS (F12) | Bootstrap prerequisite; optional manual `scripts/a8_acceptance.py p0-probe` | **Not a catalog task**: The standalone command uploads the runner-owned Wasm fixture, checks the four allowed routes and rejects broad `/Params`. It must be run explicitly while the selected testnet is live; normal `run-live` does not invoke it. |
| **A1** | N | PASS (F12) | `funded-claim` | **Covered**: Bootstrap step inside native funded claim flow. |
| **A2** | N | PASS (F12) | `funded-claim` | **Covered**: Full lock → claim → settle → release on real Gonka testnet. |
| **A3** | N + CT | PASS N (F12) + CT | `funded-claim` + Contract Tests | **Partially Covered**: Native summary execution in `funded-claim`. Under/exact/overproduction conservation proven in CT `settle_claim_full_flow_covers_production_shapes_and_exact_conservation`. |
| **B1** | N | PASS (N17 positive no-sale) | `no-sale-vesting-lifecycle` | **Current runnable coverage**: No-sale claim/lock rejection, donations, foreign CW20, vesting and release. The N17 verdict remains historical. |
| **B2 liquid** | N | PASS (L: 1423/1423) | `late-donation-after-completed` | **Covered**: Native test proves cumulative GNK rounding on completed deal liquid donations. |
| **B2 vesting / R2** | N | PASS (A/R2 gift 10000000001) | `package-a-r1-r2` | **Covered**: Verifies R1 refund boundary and streamvesting gift release across tranches. |
| **B3** | N | PASS (B3: 12345ua8b3foreign) | `b3-foreign-native` | **Covered**: Verifies preservation of foreign native denom alongside GNK. |
| **C1 missing/mismatch** | N | PASS (N17 raw refund_committed) | `funded-routing-refunds` | **Current runnable coverage**: Missing/mismatched routing refunds and rollback checkpoints. The N17 diagnostic receipt remains historical. |
| **C1 exact E lower boundary** | N | PASS (a8-c1-exact-e-20260910) | `lock-exact-e` | **Covered** (also profile `smoke`): Proves lock succeeds at target epoch E=5 with bracket h133-h134. |
| **C1 E+4 Lock** | N | PASS (X4, epoch 9 for E=5) | `lock-e-plus-4` | **Covered**: Proves upper valid lock window boundary. |
| **C1 E+5 Lock/pruning** | N | PASS (X5, epoch 10 for E=5) | `lock-e-plus-5` | **Covered**: Proves exact rejection at first expired epoch block. |
| **C1 E+5 Refund / R1** | N | PASS reviewed (A/R1) | *None* | **historical-only**: Historical A/R1 manual review; original JUnit FAIL was preserved, not rewritten. |
| **C2 / R3** | CT | `ct-package-c-policy` | `ct-package-c-policy` | **Covered at contract-policy level**: 16 exact routing/epoch fault and healthy variants. No arbitrary native fault reachability claim. |
| **D1 positive** | N | PASS (D+: E+1 reject, E+2 refund) | `claim-expiry-positive` | **Covered**: Unclaimed summary positive balance refunds only at claim expiry. |
| **D1 zero** | N | PASS (D0, total=0) | `claim-expiry-zero` | **Covered**: Zero unclaimed summary refunds only at claim expiry under special genesis. |
| **D2 zero confirmed** | CT | CT PASS | *None (CT suite)* | **CT-only**: Contract unit test `settle_zero_claim_completes_directly_and_refunds_only_a_funded_buyer`. Native claimed=true,total=0 is not claimed proven. |
| **E1 missing summary** | N | PASS (E1 stable) | `network-unconfirmed` | **Covered**: Absent native summary refunds at emergency deadline. |
| **E2 native / R4.1–4.3** | CT (current); native fault injection (historical) | `ct-package-c-policy` | `ct-package-c-policy` | **Covered at contract-policy level only**: 40 variants across 8 fault kinds and 5 lifecycle checkpoints. The historical native handler/transport injection is retired and is not reproduced by this selector. |
| **E3 / R5** | cw-multi-test | `ct-package-c-policy` | `ct-package-c-policy` | **Covered at cw-multi-test level**: 14 recovery/cancellation cases with state and balance checks. Native claim/vesting ledger writes remain covered only by complementary native scenarios. |
| **F1/F2** | N | PASS (N17 gas sweep 14 attempts) | `funded-gas-sweep` | **Current runnable coverage**: Claimed-refund gas sweep. This does not assert reproduction of all 14 historical N17 attempts. |
| **G1 / R6.1** | N fault | PASS (B6) | `package-b-r6-1` | **Covered**: Rejection of CW20 sends across Host, fee, and Buyer positions followed by retry. |
| **G1 / R6.2** | N fault | PASS (N17 cw20_fault_rollback) | *None* | **historical-only**: Diagnostic CLI from N17; contract-level rollback/retry covered in R6.3 does not establish equivalent native reachability. |
| **G1 / R6.3** | cw-multi-test | `ct-package-c-policy` | `ct-package-c-policy` | **Covered**: real contract messages and CW20 execution prove rollback/retry with a failed summary boundary. |
| **G2 / R7.1** | N fault | PASS (B7) | `package-b-r7-1` | **Covered**: Native Bank restriction rejection, atomic rollback and retry. The send position in the evidence is the release-oracle expected position; DeliverTx raw log confirms the restriction class but does not identify a message index. |
| **G2 / R7.2** | N | `emergency-host-only-recovery` | `emergency-host-only-recovery` | **Covered natively through reachable controls**: real keeper restriction rollback, expiry retry and repeat protection after a native NotFound emergency refund. |
| **G3** | N | Historical suite receipt: FAIL; semantic review: no-payout invariant observed | `terminal-release-repeat` | **Runnable, not re-executed for this source**: Historical transaction returned `NothingToRelease` with state/balances unchanged and no payout; it was not a successful no-op. The current selector requires that rejection, but has no current-commit E2E result yet. |
| **R4.4 UnsupportedRequest** | CT + Go + Wasm ABI | `ct-package-c-policy` + boundary tasks | `ct-package-c-policy` + `go-boundary` + `wasm-abi-boundary` | Exact contract policy, Go classification and compiled-Wasm decoding are covered. This is not native reachability proof. |
| **R4.4 InvalidResponse** | Go + Wasm ABI | PASS (Go test + 9 ABI cases) | `go-boundary` + `wasm-abi-boundary` | **Covered**: Go classification test + 9-case compiled-Wasm synthetic host probe. |
| **R4.5 InvalidRequest/Unknown/NoSuch** | Go + Wasm ABI | PASS (Go test + 9 ABI cases) | `go-boundary` + `wasm-abi-boundary` | **Covered**: Same Go test + 9-case compiled-Wasm synthetic host probe. |
| **R4.6 encoding/overflow/accounting** | CT | PASS (2 CT tests) | `ct-network-unconfirmed` + `ct-claim-expiry` | **Covered**: Dedicated cargo test runs for both named decision-policy tests. |

---

## 2. Analysis of the Funded Claim (`funded-claim`) Scenario

A critical verification was performed regarding the contents of the `funded-claim` test (legacy harness alias `full`, not a public runner selector; method `MarketplaceContractAcceptanceTests.marketplace funded claim settles and releases on real Gonka`):

- **What it executes**:
  1. Bootstraps accounts and instantiates Deal and Factory contracts.
  2. Funds CW20 and native tokens into the Deal.
  3. Waits for epoch E and executes `Lock` (Funded → Locked).
  4. Waits for reward and submits `Claim`.
  5. Submits `SettleClaim` verifying proportional distribution.
  6. Submits `Release` and verifies final token distribution.
- **What it DOES NOT execute**:
  - It does **not** perform negative claim expiry probes.
  - It does **not** execute no-sale release paths (historical B1).
  - It does **not** execute the 14-attempt gas sweep (historical F1/F2).
  - It does **not** inject CW20 or Bank send faults (those belong to B6, B7, and C).
- **Conclusion**: The selector `funded-claim` is strictly a single funded happy-path test. The catalog accurately reflects this limitation and does not claim full matrix coverage for this selector.

---

## 3. Explicit Proof Gaps and Boundaries

1. **Go Classification & Serialization**:
   - `go-boundary` runs a runner-owned Go module in the selected Gonka version's pinned build environment, using Gonka's dependency graph and read-only source as inputs.
   - It proves Go JSON roundtrips and typed-error classification; it does not run Gonka's `app` package or install the decorator into a node.
   - Does **not** prove libwasmvm FFI reachability or contract decision policy.

2. **Compiled-Wasm Query Chain ABI**:
   - `wasm-abi-boundary` tests the actual CosmWasm 2.2.2 `ExternalQuerier` decoder compiled to `wasm32-unknown-unknown`.
   - Uses a synthetic Node host injecting envelopes.
   - Does **not** prove that a live Gonka blockchain node can emit every error variant.
   - Native Marketplace scenarios exercise the four required query routes on the selected Gonka source. They do **not** currently execute denied or broad gRPC paths against Gonka's production Wasm query router.
   - The standalone `scripts/a8_acceptance.py p0-probe` command can check the four routes and broad `/Params` denial against a live node, but it is not part of the catalog or normal `run-live` flow and must be explicitly run and retained as evidence.
   - The retired `wasm_grpc_query_allowlist_test.go` tested denials inside the app; the synthetic ABI task is not its replacement. The immutable-source adapter restricts runs to reviewed commits and checks for the four required capabilities, but that source gate is not runtime denial evidence.

3. **Contract Decision Policy (CT)**:
   - `ct-network-unconfirmed` and `ct-claim-expiry` verify Rust contract branching logic in isolation.
   - Does **not** corrupt blockchain storage or inject live network faults.

4. **Historical-Only Probes**:
   - Original receipts remain in `docs/reviews/evidence/`; current selectors do not rewrite their verdicts.
   - B1, C1 missing/mismatch and F1/F2 now have runnable selectors listed above, with their specific proof limits.
   - The historical C1 E+5 successful refund review is distinct from the current `package-a-r1-r2` rejection probe; its original JUnit FAIL remains preserved.
   - G1/R6.2's original diagnostic probe remains historical; contract-policy rollback/retry in G1/R6.3 is not equivalent native evidence.
