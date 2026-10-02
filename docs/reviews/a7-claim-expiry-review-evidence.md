# A7 Claim-Expiry Refund/Expired — Review Evidence

> Historical evidence base for PR #14. The missing/erroneous summary policy was
> subsequently updated by ADR-0013: NetworkUnconfirmed is permitted starting at E+3.
> The table below preserves the original scope and does not represent the current active error policy.

- Date: 2026-09-07.
- Contract base: `origin/main@606c2a1`.
- Contract branch: `feat/a7-claim-expiry-refund`.
- Gonka checkout/head: `042758f4aa911606d34fc90fc1f3f257c09d55b8`.
- Gonka PR base / protobuf provenance: `379bebced638aeb5e6077bfd51c986f898443832`.
- Scope: contract/API, generated schema, unit/`cw-multi-test`, source review, and docs;
  production Gonka code/protobuf was not changed.

## Decision Matrix

| Invariant | Exact Source / Version | Applicable Reference | Decision | Test | Residual Limitation |
|---|---|---|---|---|---|
| Claim for E is impossible after E+1 | `msg_server_claim_rewards.go::validateRequest`, `042758f…`; native atomicity fixture E=100/current=101 | OroSwap HAL-01 as permissionless timing prompt | Refund strictly at `current >= checked(E+2)` | unit E+1/E+2/late + overflow; integration E+1 | Reconcile effective epoch/query with real-chain E2E |
| Only positive summary proves expiry | summary query/getter on `042758f…` | Fail-closed checklist | Require nested summary, exact Host/E, `claimed=false`; errors/absence/default reject | adapter + Deal unit matrix; integration query/malformed/oversized/wrong epoch | Genuine absence indistinguishable from read/decode error |
| `claimed=true` requires SettleClaim | payout/finishSettle shared CacheContext; SDK versions below | DAO DAO accounting-before-message | Refund forbidden on zero/positive claimed | unit/integration zero+positive; mutual exclusion | Reverify native claimed authenticity against binary SHA |
| Zero native amount does not become claim | `SettleAccounts` writes summary before zero settle skip; `validateRequest` rejects zero | Astroport zero-send finding | Unclaimed zero from E+2 uses expiry; compatibility `claimed=true,total=0` not removed | unit/integration Buyer/no Buyer, zero/positive | Native `claimed=true,total=0` not claimed reachable |
| Refund equals deposit, not balance | exact A3 funding/config | DAO DAO audit base `0b5cae57…`, finding exact funding | Transfer immutable budget to saved Buyer | integration CW20 donation + balances | Real production CW20 remains release input |
| State and payout are atomic | CosmWasm transaction model | DAO DAO state-before-message | Save Refunded/accounting/HostOnly before CW20 message | fault token rollback, successful retry, repeat reject | Real-chain smoke test B5 |
| No-sale creates no transfer | Locked without Buyer, unclaimed summary | Astroport zero-transfer finding | `Expired`, ClaimExpiry, HostOnly, event only | unit response messages empty; integration ledgers/events | At time of PR #14, summary-absent no-sale could remain Locked; ADR-0013 later added E+3 outcome |
| Other assets and index are preserved | Factory immutable Host/E index; asset namespaces | DAO DAO/OroSwap isolated accounting | Do not delete index, do not withdraw donations/denoms | integration CW20/ngonka/uatom/index | Real bank/streamvesting semantics B5 |
| Late GNK accrues to Host | ADR-0010 HostOnly 0/1 | DAO DAO/Astroport cumulative accounting | Refunded/Expired retain terminal status and lifetime counters | integration mint before and after both terminal outcomes, then release | Real unlock/schedule B5 |
| Routing refund does not regress | merged routing branch semantics | previous ADR-0011 review | Dedicated Funded branch preserved | full routing epoch/evidence/donation/rollback suite | Route gas/pruning E2E |

## Native Source Audit

### Creation and Amount Alignment

`SettleAccounts` retrieves active participant addresses, loads existing
participant rows, and builds `EpochPerformanceSummary` in a single iteration
from exact `amount.Settle.WorkCoins/RewardCoins`. Following this, a separate
iteration persists non-zero settle amounts from the same `amounts`. Both parts
reside within a single `sdkCtx.CacheContext()` and `writeFn()`. Under zero +
no-carry conditions, the summary is saved while the settle amount is skipped. An
error on any write prior to `writeFn` rolls back the entire current-epoch batch.

### Claim and Claimed

`validateRequest` enforces `(currentEpochIndex - 1) == msg.EpochIndex`, exact
settle epoch, and non-zero total. `payoutClaim` initiates a CacheContext,
executes work and reward payments, then `finishSettle` deletes the settle amount
and updates the resolved summary to `Claimed=true`, after which `writeFn` is
executed.

Existing native tests reviewed from source:

- `TestClaimRewards_EscrowPaymentFails_SettleRecordPreserved`;
- `TestClaimRewards_RewardPaymentFails_SettleRecordPreserved`;
- `TestClaimRewards_HappyPath_SettleRecordConsumed`;
- `TestMsgServer_ClaimRewards_ZeroRewards`;
- `TestSettleAmount_PaymentGuard_DistinguishesWrapFromGenuineZero`;
- `TestEpochPerformanceSummaryQuerySingle`.

In the current environment, the Go toolchain was unavailable, so these tests
were not re-executed; this reflects review of the exact test source code.

### Exact Codec and Store Path

Gonka `go.mod` specifies `cosmossdk.io/collections v1.2.1` and fork SDK
`github.com/gonka-ai/cosmos-sdk v0.53.3-ps19-observability`.

- `collections.Map.Set` first encodes key/value and returns codec errors, then
  returns the result of KV `Set`.
- Fork SDK `codec.CollValue` invokes `BinaryCodec.Marshal`.
- Generated `EpochPerformanceSummary.MarshalToSizedBuffer` contains exclusively
  scalar writes and returns `(len - i, nil)`.
- Fork SDK `runtime.coreKVStore.Set` invokes underlying `KVStore.Set` and always
  returns `nil`; underlying API failures manifest as panics.

Conclusion: for an existing, correctly formatted summary, a silent
returned-error branch in `finishSettle` is not proved reachable by source
review. An abstract `error` return signature is not treated as a verified attack
vector.

### Retention and Missing Summary Scenario

`rg` across the production Go tree at head identified
`SetEpochPerformanceSummary` only in `SettleAccounts` and `finishSettle`;
`RemoveEpochPerformanceSummary` is called exclusively in native tests.
`pruning.go` prunes claim-recipient rows, not performance summaries.

However, a summary may fail to be created: Host absent from the active set;
active address lacking a participant row causing `GetParticipants` to skip it;
absent active set; or settlement returning an error. `onEndOfPoCValidationStage`
logs settlement errors and proceeds with the lifecycle. The getter then folds
not-found and decode/read errors into a gRPC error. Such a Locked Deal cannot be
closed safely by the contract under this earlier logic; funded deposits remain
locked.

Required external capability: a successful typed authoritative outcome that
distinguishes "summary legitimately absent and claim impossible" from
storage/decode failures. Text parsing, zero-defaults, and trusted manual refunds
are rejected.

## Protobuf Provenance

Vendored canonical schemas remain from `379beb…`; the PR head introduces no
schema changes. Comparison of `query.proto` and `epoch_performance_summary.proto`
against the fork head revealed no substantive differences (differences were
limited to line endings). Generated Rust was not regenerated and the source pin
was unchanged. The chain binary SHA for allowlist integration must be recorded
separately from protobuf source provenance.

## Verified Boundary

Contract tests verify decision, state, and ledger reactions using
protobuf-aware mocks. They do not prove that a production node is running the
PR head, that native responses reliably reflect payouts, that query gas is
acceptable, that contract recipients are supported, or that streamvesting
unlocks become spendable. Those remain handoffs for B1/B4/B5.

## Local Validation

On the local checkout, the following were executed and passed:

- `cargo fmt --all --check`;
- `cargo clippy --workspace --all-targets --all-features --locked -- -D warnings`;
- `cargo test --workspace --locked` — 120 tests;
- Dedicated rerun of `cargo test -p marketplace-factory --test integration` after
  adding post-terminal GNK — 39 tests;
- Release Wasm builds for Factory and Deal, plus `cosmwasm-check` for both artifacts;
- fmt/clippy/test for the separate `tools/proto-gen` manifest;
- Schema generator; protobuf generator with empty diff in `packages/gonka-proto`;
- `cargo audit` for workspace and proto-gen lockfiles: no vulnerabilities found;
  workspace reports only accepted unmaintained warnings for `derivative 2.2.0` and
  `paste 1.0.15`;
- `cargo deny check` for both manifests: advisories, bans, licenses, and sources
  `ok`; warnings limited to allowed duplicate transitive versions and unused
  `BSD-3-Clause` allowance in proto-gen policy.

Native Go tests were not run due to lack of a Go toolchain; the list above
reflects source code review rather than test execution output.
