# A8: End-to-End Contract Gap Analysis

## Current Acceptance Status for Merge

The agreed scope of verification is complete; evidence is accepted. No
additional live runs are required to close these MRs. Current row-by-row matrix:
[coverage matrix](a8-final-coverage-matrix.md); final execution records:
[evidence](evidence/a8-remaining-checks-20260910.json),
[full R4.6 logs](evidence/a8-r4-6-full-logs-review.json).
Native C: 73 cases accepted via receipts; historical timeout/JUnit XML is not
rewritten. C1/E has an independent PASS JUnit. Go JSON, synthetic Wasm ABI, and
CT have PASS at their respective levels. Production Go↔libwasmvm/keeper
reachability is not claimed; this represents an evidence boundary, not a new
mandatory merge requirement. The unified sequential runner and future code
modifications are subsequent independent tasks. Merge does not constitute
production deployment approval and does not waive release gates.

## Acceptance History (Legacy TODOs Are Not the Current Plan)

Current status and remaining items: [verified matrix](a8-final-coverage-matrix.md).
Below is preserved the preparation/run history; PREPARED/NOT RUN in legacy sections
do not supersede the current matrix. B2 and R1–R7 native subcases are accepted based on evidence;
exact native C1/E attribution and VM/FFI remain explicitly highlighted gaps.

## Native Continuation Update (2026-09-09)

Runs `a8-negative-016`/`017` closed several open observations on production
runtime: actual E+3 gas failure/rejection, no-sale vesting and donation event
proof, terminal repeats, pruned Lock rejection, Factory isolation,
network-unconfirmed refund, and no-Buyer Expired outcome. This does not close
the E+2 fail-closed boundary, controlled native fault injection, or reproducible
`claimed=false` summaries. Production advisory CWA-2025-007 remains an open gate.

- Date: 2026-09-07.
- Base: `origin/main@45b17b761ab0f8b4c3185a81d3f67eeb1bbfb5f6` after PR #11 merge.
- Scope: public execute/query handlers, Factory-created Deal, contract-only
  tests, generated schemas, integrator handoff documentation, and release gates.
- Out of scope: native Gonka patch, keeper, deployment tooling A9, migration/admin,
  N-party/V2, and merge of this PR.

Addendum 2026-09-07: Following merge of PR #14, the contract-side matrix was
extended by ADR-0013 with emergency NetworkUnconfirmed starting at E+3. The
original A8 baseline is preserved for traceability; new evidence and constraints
are listed below and in [A10 review evidence](a10-network-unconfirmed-review-evidence.md).

## Notation

- **CT** — implemented and proven via unit/property/`cw-multi-test` suites.
- **E2E** — contract logic implemented, but native semantics require real Gonka chain execution.
- **GATED** — transition intentionally unavailable until native evidence gates are closed.

`cw-multi-test` uses real CW20 and bank ledgers, but substitutes custom Gonka
gRPC with a protobuf-aware adapter. Therefore, CT does not imply production
compatibility with `wasmd`, claim recipients, or streamvesting.

## Requirements and Evidence Matrix

| Scenario / Invariant | Implementation | Existing or A8 Evidence | Missing Evidence | Native Dependency |
|---|---|---|---|---|
| Factory creates unique Deal without admin and preserves two indexes | CT + E2E | `contracts_store_config_and_no_admin_blocks_code_replacement`; Factory unit reply tests | Chain `ContractInfo.admin=None`, upload/instantiate receipts | B5/A9 |
| Single address serves only one Host/E | CT + E2E | `multiple_host_epoch_lifecycles_keep_ledgers_recipients_and_counters_isolated` | Real streamvesting address resolution for contract recipients | B5 |
| Exact CW20 funding before E; failed hook rolls back preparatory Send | CT + E2E | `factory_to_deal_exact_cw20_send_funds_once_with_exact_balances`; `rejected_real_cw20_sends_roll_back_transfer_and_open_state` | Real availability of recipient queries from Wasm | B1/B3/B5 |
| `Open/Funded -> Locked` strictly in `E..E+4` with exact routing | CT + E2E | Deal unit routing/window matrix; `failed_lock_and_cancel_proofs_preserve_real_deal_state_and_balances` | Exact pruning timing, gas bounds, contract recipient support | B1/B3/B5/B6 |
| Late routing does not re-evaluate successful Lock | CT + E2E | `settlement_uses_locked_snapshot_and_deposit_accounting_not_live_rows_or_balance` | Real pruning after persisted lock proof | B5 |
| Under/exact/overproduction and `net + fee + refund = deposit` | CT + E2E | `settle_claim_full_flow_covers_production_shapes_and_exact_conservation`; math property tests | Native summary matches actual payout/vesting | B4/B5 |
| No-sale positive claim belongs to Host, USDT does not move | CT + E2E | `release_supports_buyer_only_no_sale_and_coincident_recipients_without_zero_sends` | Real claim and unlock to contract address | B5 |
| Confirmed zero settlement returns full deposit to Buyer and transitions immediately to Completed | CT + E2E | `settle_zero_claim_completes_directly_and_refunds_only_a_funded_buyer`; rollback/retry integration test | Whether pinned native lifecycle can yield authoritative `claimed=true,total=0` | Gonka core/B4/B5 |
| Permanent GNK shares, multiple releases, Completed, and late release | CT + E2E | `permanent_share_lifecycle_includes_pre_settlement_gifts_completion_and_late_alias`; math partition property | Real spendable unlocks and late schedules | B5 |
| `ReleaseUnlockedGnk` and Completed-only `ForwardExcessGnk` share identical shares/counters | CT + E2E | A8 extension of full lifecycle calls both entrypoints post-Completed | Native bank semantics | B5 |
| Pre-settlement donations and post-Completed donations follow frozen shares | CT + E2E | permanent-share lifecycle; ADR-0010 property tests | Liquid and vested donations on real chain | B5 |
| Remaining vesting does not constrain release | CT + E2E | `release_never_queries_vesting_even_when_every_vesting_response_shape_is_invalid` | Real independence of existing schedules from new schedules | B5 |
| `Open -> Cancelled` transfers no funds and preserves index | CT + E2E | `permissionless_cancel_keeps_factory_index_and_blocks_recreate_and_funding` | Real routing/pruning window | B3/B5 |
| Routing failure returns strictly deposit, not donation | CT + E2E | `routing_refund_returns_only_the_deposit_keeps_other_assets_and_enables_host_only_gnk` | Real allowlist/query/gas boundary | B1/B3/B5 |
| Claim-expiry / NetworkUnconfirmed `Locked -> Refunded` | CT + E2E | Unit raw error matrix; claim-expiry test; `network_unconfirmed_refund_rolls_back_retries_and_keeps_terminal_host_only_economics` | Exact production runtime, native gas + real-chain receipts | B4/B5 |
| No-sale `Locked -> Expired` | CT + E2E | claim-expiry lifecycle; `network_unconfirmed_no_sale_expires_without_transfer` | Exact production runtime + real-chain receipts | B4/B5 |
| Multiple Host/epoch pairs do not mix deposits, rewards, recipients, and counters | CT + E2E | New A8 multi-deal lifecycle: 3 Factory-created Deals, 2 Hosts, 2 epochs | Real independent streamvesting schedules | B5 |
| Factory index is preserved in terminal states | CT | Completed/Cancelled/Refunded existing tests; claim-expiry tests include Expired index | Real-chain query | B5 |
| Error on any CW20 transfer rolls back settlement/refund and allows one retry | CT | `every_nonzero_cw20_transfer_failure_rolls_back_state_and_all_balances_then_retries_once`; refund/zero tests | Real-chain atomicity smoke test | B5 |
| Error on any BankMsg rolls back counters/all sends and allows retry | CT | `bank_failures_roll_back_each_release_send_and_forwarding_then_allow_exact_retry` | Real bank keeper atomicity | B5 |
| Zero transfers are absent; coincident recipients are handled correctly | CT | dust/no-sale/coincident-recipient integration tests | Real event/balance receipts | B5 |
| Other denoms and CW20 tokens remain untouched | CT | Existing unrelated native denom checks; new A8 multi-deal test preserves unrelated CW20 balance | Real chain ledger smoke test | B5 |
| Events and queries reflect committed state | CT + E2E | Explicit event/query assertions in all lifecycle tests; generated schema reproduction | Real tx events/indexer mapping | B5/integrator |
| Repeats and invalid ordering do not generate payouts | CT | funding/lock/cancel/settle/refund/release repeat and wrong-state tests | Real-chain retry policy verification | B5/keeper |

## Complete Execute Transition Matrix

`→X` indicates successful transition; `same` — successful release preserves status,
except upon first reaching original total from Releasing; `—` — typed failure
prior to movement of funds. ClaimExpiry requires positive exact unclaimed
summary; starting at E+3, permitted response/availability errors yield
NetworkUnconfirmed.

| Execute | Open | Funded | Locked | Releasing | Completed | Refunded | Cancelled | Expired |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CW20 `Receive(Fund)` | `→Funded` | — | — | — | — | — | — | — |
| `Lock {}` | `→Locked` | `→Locked` | — | — | — | — | — | — |
| `SettleClaim {}` | — | — | `→Releasing/Completed` | — | — | — | — | — |
| `ReleaseUnlockedGnk {}` | — | — | — | `same/→Completed` | `same` | `same` | — | `same`* |
| `Refund {}` | — | `→Refunded` (routing only) | `→Refunded/Expired` (claim proof from E+2 or NetworkUnconfirmed from E+3) | — | — | — | — | — |
| `Cancel {}` | `→Cancelled` | — | — | — | — | — | — | — |
| `ForwardExcessGnk {}` | — | — | — | — | `same` | — | — | — |

`*` Expired is created via `Refund {}` without a Buyer and uses HostOnly; a
separate Expire execute handler does not exist.

Additional gates:

- all execute handlers are nonpayable;
- funding caller is strictly configured CW20, Buyer is extracted from verified hook sender;
- prior to E, Cancel is callable only by Host; in `E..E+4` Cancel is permissionless;
- Lock, Settle, Release, routing Refund, and alias are permissionless, but caller cannot set amounts, shares, or recipients;
- funding closes at `current >= E`; Lock and routing Refund close at `current >= E+5`; settlement after successful Lock has no contract-side deadline;
- `ForwardExcessGnk` is strictly a backward-compatible alias after Completed.

## Identified and Closed A8 Gaps

1. Added multi-deal lifecycle with real CW20/bank ledgers. It proves isolation of
   three Deals across distinct Host/epoch pairs, separate settlement terms,
   recipients, GNK balances, lifetime counters, and Factory indexes.
2. Full lifecycle extended with two sequential late paths post-Completed: first
   `ReleaseUnlockedGnk`, then `ForwardExcessGnk`. Duplicate completion events
   are suppressed.
3. Added proof that unrelated CW20 tokens, like unrelated native denoms, remain
   unaffected by settlement/release.
4. Removed deprecated manual storage write of `Completed` from multi-offer test;
   terminal index is now proven strictly via public state transitions.
5. No production contract bug found when comparing spec/API/code; economics,
   native boundaries, and public schemas were not modified.

## Reference and Security Decision Record

### Isolation of Multiple Deals

Decision: Dedicated contract instance per Host/E and verification of ledger isolation.
Our invariant: Neither storage, deposit, nor GNK payout of one deal affects another.
Gonka/source constraint: Streamvesting aggregates solely by recipient address and
does not store marketplace deal IDs.
DAO DAO comparison: Audited `cw-payroll-factory`/`cw-vesting` scope at base
`0b5cae57...` confirms the applicability of factory/reply for distinct instances.
OroSwap comparison: HAL-08 serves only as a general stale pending guard; its
pool-initializer scope does not cover our Factory.
Decision and test: Immutable per-deal config + Factory indexes;
`multiple_host_epoch_lifecycles_keep_ledgers_recipients_and_counters_isolated`.

### Permanent Shares and Two Release Entrypoints

Decision: ADR-0010 remains unchanged; alias receives no distinct economics.
Our invariant: Cumulative target depends on lifetime `U`, rather than inflow
segmentation or entrypoint; sum of deltas equals available `ngonka` balance.
Astroport comparison: Maker/Vesting audited commits are used solely for
zero-send/event review; legacy CosmWasm and GPL code are not ported.
Decision and test: Shared `execute_gnk_release`, late invocation of both
entrypoints in a single public lifecycle, without duplicate `deal_completed`.

### Advisories as of 2026-09-07

The official [CosmWasm advisory index](https://github.com/CosmWasm/advisories/tree/main/CWAs)
contains CWA-2026-001 and placeholders CWA-2026-002…006.
Pinned Gonka `wasmd v0.54.2` is explicitly affected by CWA-2025-007; patched branches
begin with `v0.54.3` ([CWA-2025-007](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2025-007.md)).
[CWA-2026-001](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2026-001.md)
specifies `wasmd v0.54.5`/`wasmvm v2.2.5`, rather than pinned `v0.54.2`/`v2.2.4`;
this does not waive the CWA-2025-007 gate. Placeholder advisories do not provide
sufficient data to deduce applicability. Production B4 must verify the exact
running binary and all published details prior to deployment; contract-only A8
cannot patch the node runtime.

### Versions and Boundaries of Audited References

| Reference | Verified Scope / Commit | Stack | Applicability to A8 |
|---|---|---|---|
| DAO DAO / Oak | `cw-payroll-factory`, `cw-vesting`, `cw-wormhole`; `0b5cae57…` | CosmWasm 1.5.4 | separate instance, exact funding, accounting-before-message; not our economics |
| OroSwap / Halborn | Factory/Vesting in `9042989…`; HAL-08 separately `pool_initializer@59f095b…` | CosmWasm 1.5 | permissionless/gas and pending-state prompts; no GPL code copied |
| Astroport / Oak | Maker `1f50cab…`, Vesting `042b076…` | CosmWasm 1.1 / cw-plus 0.15 | fixed recipients, cumulative accounting, zero/event checks; too legacy for API proof |
| Marketplace | current branch on CosmWasm 2.2.2 / cw-plus 2.0 | CosmWasm 2.2.2 | requires own tests and real Gonka; external audits do not cover it |

In OroSwap, the official report lists both assessed commits and includes Factory
in the 162-file scope of main `9042989…`; HAL-08 cannot be represented as a finding
on Factory/Vesting. No external audit is cited as an assertion that Marketplace is audited.

## Validation Record

Locally on the A8 branch, the following passed:

- One-command Testermint lifecycle `a8-funded-009` on production Gonka runtime
  SHA `29a58fc…`: store/deploy/fund/lock/native claim/settle, two vesting releases,
  Completed, and late donation; compact evidence —
  [a8-funded-009.json](evidence/a8-funded-009.json);
- Exact A9 manifest/HEAD and runtime identity are verified fail-closed; Testermint
  preflight covers containers, Postgres volumes, `chain-public`, and ignored
  `prod-local`; pre-existing local data is rejected prior to destructive reboot;
- Independent settlement oracle constructs expected state strictly from native summary
  and offer terms, then validates Deal state and recipient CW20 deltas. Regressions
  with zero fee / all budget to Host and invalid initial GNK shares are rejected;
- Clean clone of remote branch passed full settlement-oracle run
  [a8-funded-012-settlement-oracle.json](evidence/a8-funded-012-settlement-oracle.json);
- Independent payout oracle verified individual Buyer/Host deltas and lifetime
  counters for both tranches and late donation; conserving wrong-split regression
  is independently rejected;
- Mandatory Kotlin lifecycle published in
  [Gonka PR #2](https://github.com/gonka-ai/gonka/pull/2); clean clone of remote
  branch `test/a8-marketplace-harness` passed full run `a8-funded-010`;
- P0 real-Wasm probe: four exact native queries PASS, broad `Query/Params` denied;

- Reproducible protobuf generation: 43 vendored files, tracked diff clean;
- Reproducible Factory/Deal schema generation, tracked diff clean;
- Workspace and proto-generator `cargo fmt --check`;
- Workspace and proto-generator Clippy with all targets/features and `-D warnings`;
- 125 workspace tests: 4 protobuf golden, 20 common, 51 Deal unit,
  9 Factory unit, 41 Factory integration; generator tests also passed;
- Release Wasm builds and `cosmwasm-check 2.2.2` for both contracts;
- `cargo audit` for both lockfiles: no vulnerabilities; workspace reports only
  accepted unmaintained warnings for `derivative`/`paste`;
- `cargo deny` for both lockfiles: advisories/bans/licenses/sources `ok`, only
  policy warnings for duplicate transitive versions/unmatched allowance;
- `git diff --check`.

GitHub CI is recorded post-push / PR creation and is not substituted by this local record.

## Scope Summary

The contract-only A8 slice is complete, and the positive funded lifecycle, real
contract recipient support, streamvesting/bank semantics, and P0 allowlist were
additionally proven via live execution. Full A8 and MVP remain `NO-GO` for
production: native fault/gas/refund/pruning/multi-Deal matrix has not been
executed on main, and running `wasmd v0.54.2` is subject to CWA-2025-007.
Typed absence API B2 may enhance diagnostics, but does not waive these release gates.
