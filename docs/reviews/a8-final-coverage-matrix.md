# A8: Verified Evidence Coverage Matrix

> [!NOTE]
> **Historical Provenance Notice**
> Commit references and PR designations in this matrix originate from the private development archive.
> Historical PASS statuses validate specific test suites against the recorded source states and do
> not constitute automatic verification of newer builds.

The historical A8 acceptance scope was closed for the source revisions in the
register below: native (including C1/E),
Go classification/JSON, compiled-Wasm synthetic-host ABI, and contract policy tests.
That decision did not cover PR #27's new immutable-source runner or its current
product commit. Those changes require their own recorded validation before a
current live-verification claim. The proof-level limitations below remain in force.

Date: 2026-09-10. This is the authoritative summary superseding historical TODOs in
package handoffs and earlier gap analyses. Verification was performed directly without
delegation and offline. PASS applies strictly to the specified level and execution sources,
not to arbitrary future HEAD commits.
CT = contract unit/property/cw-multi-test; N = native; V = VM/FFI boundary.

## Source and Evidence Register

The [Machine-readable register](evidence/a8-matrix-source-register-20260910.json)
contains full source SHAs, hashes of compact files, and raw references. Short reference
codes below point to entries in that register, rather than replacing source SHAs.

| Ref | Evidence / Source |
|---|---|
| F12 | a8-funded-012-settlement-oracle.json: Marketplace e369b48bd2640c79161f9566cc507c51307e813c; Gonka harness 9f32e0274a3e22ddb567ea024820cc2522a250db |
| N17 | a8-negative-017-diagnostic.json + raw-review in register; Marketplace resolved-83076ee; Gonka d7cd261e3bbf293dccd398301fcb3fd9d297cd07 |
| A | a8-abc-reviewed-20260910.json / a8abc-a-20260909b: Marketplace a6bbc6f9c6736c4e81fd7c8409385843db19b620; Gonka b9c6ec54242109ef8a5ce7e4bdac1b992f78f2dc |
| B6 | same file / a8abc-b6-20260909; exact source in runs/source |
| B7 | a8-final-reruns-review-20260910.json / a8r7fix2-20260910: Marketplace 0a33f7d4b9b8d78db94611838ff2d2baaf5c528a; Gonka 6a09808e512f73e9809c845a93e7871f20e539b8 |
| C | a8-c-final-review-20260910.json: Marketplace 41489d5e599fb8e7a537312fb84ea2bedda0507a; Gonka 6a09808e512f73e9809c845a93e7871f20e539b8 |
| L | a8-b2-late-donation-completed-005.json; source in register |
| X4/X5 | c1-lock-e-plus-4-003.json / c1-lock-e-plus-5-002.json; provenance in register |
| D+/D0 | a8-claim-expiry-positive-043.json / a8-claim-expiry-zero-044.json; source in register |
| E1 | a8-network-unconfirmed-stable-e1-002.json; source in register |
| B3 | a8-b3-foreign-native-004.json; source in register |
| G3 | g3-terminal-release-repeat-002.json; source in register |

All Kotlin methods below reside in `MarketplaceContractAcceptanceTests.kt` in
Gonka Testermint. Python commands are in `scripts/a8_acceptance.py`. C case names are in
`scripts/a8_query_faults.py`. CT names are in `marketplace-deal` unit/integration tests.

## Main Matrix

| ID | Level | Exact Test / Command / Case | Status and Evidence |
|---|---|---|---|
| P0 | N | p0-probe / bootstrap probe | PASS, previously accepted route probe; F12 and original acceptance report. Not a new allowlist audit |
| A1 | N | bootstrap, store/instantiate provenance | PASS, F12; immutable admin/config |
| A2 | N | marketplace funded claim settles and releases on real Gonka | PASS, F12; lock → claim → settle → release |
| A3 | N + CT | claim-settle-scenario; settle_claim_full_flow_covers_production_shapes_and_exact_conservation | PASS N for real summary F12; under/exact/overproduction verified in CT, not three separate native forms |
| B1 | N | scenario-settle/release no-sale | PASS N17 positive no-sale and Host release. Old release_repeat_noop is NOT execution proof; use G3/C for real repeats |
| B2 liquid | N | marketplace late liquid donations after Completed use cumulative GNK rounding | PASS L: donations 1423/1423, Buyer 1/2, Host 1422/1421 |
| B2 vesting / R2 | N | marketplace package A preserves R1 refund boundary and releases a new vested gift; r2-gift-checkpoint | PASS A/R2: pre_gift, fully_locked, first_unlocked, final; original tranches and new gift 10000000001 fully accounted for. Extra Buyer 1055134 + Host 9998944867 = gift |
| B3 | N | marketplace successful release preserves foreign native denom | PASS B3: 12345ua8b3foreign preserved; foreign CW20 verified in N17 and C snapshots |
| C1 missing/mismatch | N | refund-scenario routing-missing/routing-mismatch | PASS N17: raw refund_committed, Buyer +10000000, Deal CW20=0, terminal/competing reject |
| C1 exact E lower boundary | N | `lock-exact-e` / `marketplace funded lock succeeds exactly at E` | PASS: [`a8-c1-exact-e-20260910.json`](evidence/a8-c1-exact-e-20260910.json), tx `07DA…ABFA`, target/inclusion E=5, bracket h133–h134; Funded→Locked, routing proof, immutable accounting/balances and no Deal Bank transfer asserted. JUnit SHA pinned in `a8-remaining-checks-20260910.json` |
| C1 E+4 Lock | N | marketplace funded lock succeeds exactly at E plus 4 | PASS X4, bracket 233–234, epoch 9 for E=5 |
| C1 E+5 Lock/pruning | N | marketplace funded lock rejects exactly at E plus 5 | PASS X5, bracket 258–259, epoch 10 for E=5; row absent + exact window error |
| C1 E+5 Refund / R1 | N reviewed | refund-window-closed-scenario | PASS A/R1 historical receipt with corrected oracle; original JUnit FAIL was not rewritten |
| C2 / R3 | N fault | C r3 routing/epoch lock/refund + healthy counterparts | PASS C, including healthy Lock after recovery |
| D1 positive | N | marketplace positive unclaimed summary refunds only at claim expiry | PASS D+: E+1 reject, E+2 full refund |
| D1 zero | N special genesis | marketplace zero unclaimed summary refunds only at claim expiry | PASS D0, claimed=false,total=0. Not a production-config reachability claim |
| D2 zero confirmed | CT | settle_zero_claim_completes_directly_and_refunds_only_a_funded_buyer | CT PASS under previous acceptance; native claimed=true,total=0 is not claimed proven |
| E1 missing summary | N | marketplace absent native summary refunds only at emergency deadline | PASS E1; constraint of coincident Buyer/Host addresses is preserved |
| E2 native / R4.1–4.3 | N fault | C r4-{handler_error,malformed_protobuf,oversized_response,missing_nested_summary,wrong_host,wrong_epoch,invalid_participant_address}-{probe,e2,e3,terminal-*} | PASS C; errors injected on allowed native test instrumentation |
| E3 / R5 | N fault | C r5-recover / r5-cancel | PASS C: real claim, recovery prior to deadline or emergency cancellation, terminal and HostOnly |
| F1/F2 | N | claimed-refund-gas-sweep, direct and caller/submessage | PASS N17: 14 attempts, OOG + sufficient gas rejection. Raw gas evidence preserved in register |
| G1 / R6.1 | N fault | marketplace R6 dot 1 rejects all three selected CW20 sends then settles once | PASS B6, Host/fee/Buyer send positions |
| G1 / R6.2 | N fault | refund-scenario --fault-cw20 routing-mismatch | PASS N17 raw cw20_fault_rollback before=after; then full refund and terminal repeat |
| G1 / R6.3 | N fault | C r6.3 → r5-cancel-e3 | PASS C, CW20 rollback with active summary fault, then refund |
| G2 / R7.1 | N fault | marketplace R7 dot 1 rejects the selected second payout position under a native Bank restriction, then retries once | PASS B7, restriction rejection, atomic rollback, retry and repeat. The chain receipt does not expose the failing message index; that position is bound to the release oracle. |
| G2 / R7.2 | N reviewed | C bank-fault/bank-retry; native_bank_release_retry | PASS C via raw context: h716 release, h717 repeat. Summary row omitted due to timeout |
| G3 | N | marketplace terminal release repeat rejects without payout | Historical suite receipt is FAIL (`g3-terminal-release-repeat-002.json`); semantic review of the included DeliverTx found `NothingToRelease`, unchanged state/balances and no repeat payout. This is not a successful no-op or a current-source E2E PASS. |

## Accepted Additional Level Checks and Their Boundaries

| Case | Level / Exact CT Test | Status |
|---|---|---|
| R4.4 UnsupportedRequest | N decorator unsupported_request; CT network_unconfirmed_refund_accepts_only_explicit_summary_failure_matrix_after_e_plus_three | Native C variant PASS; exact classification of typed SystemError must follow pinned boundary, not standard keeper error |
| R4.4 InvalidResponse | Go classification/JSON; compiled-Wasm ABI synthetic host | PASS on both corresponding levels: Go `TestToQuerierResultClassifiesVMSystemErrors` in [`a8-go-boundary-20260910`](evidence/a8-go-boundary-20260910), and 9-case ABI probe. Not proof of libwasmvm FFI, keeper, or contract decision-policy |
| R4.5 InvalidRequest / Unknown / NoSuchContract / NoSuchCode | Go classification/JSON; compiled-Wasm ABI synthetic host | PASS: same Go test and ABI cases. This is serialization/decoder evidence, not production reachability |
| R4.6 encoding/overflow/accounting | CT; network_unconfirmed_refund_rejects_early_and_unexpected_system_failures; claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow | PASS: both exact contract tests re-executed: each 1 passed / 0 failed; [full logs and source hashes](evidence/a8-r4-6-full-logs-review.json). Native storage corruption remains unnecessary |

## Conclusion and Boundaries

Prepared packages R1–R7 have native evidence (excluding explicitly decoupled VM/CT branches).
B2/vesting is no longer PARTIAL. C1 is decomposed into proven subcases: accuracy of the
native lower E boundary check is now closed by a focused probe with explicit receipt/bracket.
This does not alter R4 proof levels: Go classification and synthetic Wasm ABI do not confirm
production Go dispatcher, libwasmvm FFI, or keeper reachability. CT covers contract decision
policy, but does not constitute live fault injection. New A/B/C runs for these results are not required.

Agreed checks are closed at the indicated levels. Production VM/FFI/keeper reachability is
not proved by these probes and is not claimed. This is a recognized limitation, not an
additional mandatory run. Native C 73/73 reviewed does not imply a green historical JUnit
or production release approval. C1/E has an independent green JUnit; R4.6 is verified by
complete logs of a real run of the two named tests.
