# A10: Review Evidence for Emergency NetworkUnconfirmed Refund

- Date: 2026-09-07.
- Base: `origin/main@5f4ed672b397f49cacd81c211e7e2417e1d0b987` after PR #14 merge.
- Branch: `feat/a10-emergency-unconfirmed-refund`.
- Scope: Deal/API/schema/tests/docs; Gonka production code and protobuf API were not changed.
- Deployment/merge: Out of scope.

## Answer-First Result

The existing `Refund {}` entrypoint is extended strictly for pristine Locked Deals.
Standard ClaimExpiry remains at E+2 with exact `claimed=false`. The new
NetworkUnconfirmed refund is permitted starting at E+3, strictly when the fixed
Host/E summary request terminated with one of the explicit response or
availability errors. `claimed=true` prohibits Refund across all epochs. Current
epoch query, request encoding, config/state/accounting, overflow, and unexpected
system errors remain fail-closed.

Economics: Buyer receives exact configured deposit; Host and fee recipient
receive 0 USDT; Buyer receives 0 GNK; all available and future GNK tokens are
released to the Host via `HostOnly`. In the absence of a Buyer, the deal
transitions to `Expired` without transfers. The outcome is terminal.

## Error Matrix Evidence

| Category | Decision | Contract Test |
|---|---|---|
| Native handler/NotFound (`ContractResult::Err`) | E+3 allow | `network_unconfirmed_refund_accepts_only_explicit_summary_failure_matrix_after_e_plus_three` |
| typed UnsupportedRequest | E+3 allow | same |
| typed InvalidResponse | E+3 allow | same |
| malformed, missing nested, oversized | E+3 allow | same |
| wrong Host/E, invalid address | E+3 allow | same |
| Any allowed error before E+3 | reject | `network_unconfirmed_refund_rejects_early_and_unexpected_system_failures` |
| InvalidRequest/Unknown/NoSuchContract/NoSuchCode | reject | same |
| request encoding / arithmetic classification | reject | same |
| GetCurrentEpoch failure concurrent with summary failure | reject | `refund_rechecks_current_summary_and_current_epoch_on_every_attempt` |
| Recovery to claimed=true / claimed=false | Settle-only / ClaimExpiry | same |

## Runtime Provenance

Local Gonka checkout `042758f4aa911606d34fc90fc1f3f257c09d55b8` depends on
`wasmd v0.54.2` and `wasmvm/v2 v2.2.4`; protobuf provenance is
`379bebced638aeb5e6077bfd51c986f898443832`.

Exact upstream tags verified at commits:

- wasmd `v0.54.2@e9bff8b543a68b2e33b01cc957de07dc237be05e`:
  `x/wasm/keeper/query_plugins.go`, `msg_dispatcher.go`;
- wasmvm `v2.2.4@45efbe55f3874020a1ba16effadf2f6737a69da1`:
  `types/queries.go`, `internal/api/callbacks.go`,
  `libwasmvm/src/querier.rs`, `libwasmvm/src/error/go.rs`;
- Gonka: `app/legacy.go`,
  `x/inference/keeper/query_epoch_performance_summary.go`,
  `epoch_performance_summary.go`.

The trace confirms: request JSON parse failure -> typed InvalidRequest;
allowlist/route failure -> typed UnsupportedRequest; ordinary
handler/NotFound/codec error -> redacted ContractResult error; invalid Go
response envelope -> typed InvalidResponse. Text parsing is absent.

Gas panics follow `recoverPanic -> GoError::OutOfGas -> BackendError::OutOfGas`
and abort the VM; other panics similarly become foreign backend panics. Such
aborts do not reach the Rust contract as `SystemResult/ContractResult` and cannot
permit Refund.

## Financial and State Evidence

`network_unconfirmed_refund_rolls_back_retries_and_keeps_terminal_host_only_economics`
verifies in a single public lifecycle:

- Native claim may be `claimed=true` while the summary route returns error;
- CW20 transfer failure rolls back state and balances;
- Retry returns exactly one deposit and leaves CW20 donations untouched;
- Host and fee recipient receive 0 USDT, other denoms remain untouched;
- Recovered claimed summary cannot revive SettleClaim or Refund;
- Pre-existing and late GNK tokens accrue entirely to the Host, Buyer receives 0 GNK;
- Factory Host/E index is preserved.

`network_unconfirmed_no_sale_expires_without_transfer` verifies Expired status
without a Buyer and the absence of USDT messages and events.

Local audit-reference snapshots were re-verified: DAO DAO audit base covers
specifically `cw-payroll-factory`/`cw-vesting`/`cw-wormhole`, and post-audit
`0178cf55…` is its descendant; OroSwap `9042989…` covers Factory/Vesting,
but remediation `2fa02f9…` affects only a separate `pool_initializer`; Astroport
commits indeed cover Maker/Vesting. Therefore, HAL-08 is not cited as an audit of
our refund path, and GPL sources serve strictly as security prompts.

## Local Validation

Passed:

- Reproducible protobuf generation: 43 vendored files, no extraneous diff;
- Reproducible Factory/Deal schema generation; new enum reflected in both Deal schemas;
- `cargo fmt --all --check`;
- `cargo clippy --all-targets --all-features -- -D warnings`;
- `cargo test --all`: 125 tests (4 proto golden, 20 common, 51 Deal unit,
  9 Factory unit, 41 Factory integration);
- Proto generator fmt/clippy/test;
- 21 A9 Python release-tooling tests;
- Release Wasm builds for both contracts;
- `cosmwasm-check`: both contracts pass;
- `git diff --check`.

## Native Evidence Limitations and Real-Chain Acceptance

Because a Go toolchain was unavailable in the local environment, a new focused
native/Wasm gas regression was not run. Existing upstream source and tests
corroborate abort semantics, but mocks and cw-multi-test do not substitute for
the actual Gonka binary.

Prior to acceptance on a real chain, the following remain:

1. Confirm exact deployed Gonka/wasmd/wasmvm binary and allowlist.
2. Execute healthy-summary Refund with insufficient gas directly and via
   contract/submessage; neither invocation may produce an emergency success.
3. Verify E+1/E+2/E+3/late and the full allowed/denied raw error matrix on chain.
4. Verify recovery to claimed=true/false prior to terminal outcome and
   immutability of terminal outcome after recovery.
5. Record CW20/bank/state/events receipts for Buyer/no Buyer, donations,
   rollback/retry, Factory index, and late HostOnly streamvesting unlock.
6. Re-evaluate runtime advisory and remediation reviews, including CWA-2025-007.

Chain halts, current-epoch query failures, VM aborts, and token transfer
failures can impede refunds. Keeper or automated invocation is not implemented;
Refund requires an explicit transaction submitted by any caller.
