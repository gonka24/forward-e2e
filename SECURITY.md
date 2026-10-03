> These inherited product invariants describe the contracts under test.
> Production implementation and its current security policy live in
> [forward-contracts](https://github.com/gonka24/forward-contracts/blob/main/SECURITY.md).
> This repository is the acceptance runner that executes those contracts on a
> real Gonka network. Runner integrity, immutable sources, credentials and
> ownership cleanup are specified in [AGENTS.md](AGENTS.md) §4 and in
> [`docs/`](docs/README.md) — chiefly [`docs/architecture.md`](docs/architecture.md),
> [`docs/operations.md`](docs/operations.md) and [`docs/evidence.md`](docs/evidence.md);
> what each catalog task does and does not exercise is in
> [`docs/coverage.md`](docs/coverage.md).

# Security Policy and Initial Invariants

This document outlines the security boundaries of the project. Detailed invariants are refined alongside the public API and state machine of the contracts.

## Architecture Invariants

1. Each deal receives an isolated `marketplace-deal` address; global escrow is prohibited.
2. The canonical Factory and every Deal it creates must have chain-level `ContractInfo.admin = None`. The absence of a `migrate` entry point in the current code does not replace this check: an admin could migrate an instance to new code containing its own migrate handler.
3. `marketplace-api` contains only serializable public types and contains no storage or business logic.
4. `marketplace-common` contains only stateless calculations and validated native queries. Storage, authorization, state transitions, transfers, and transaction construction belong to the contracts; the package is not deployed separately.
5. Gonka Protobuf types are generated from pinned source. Manual reproduction of wire layout is prohibited.
6. A transport/decode error or semantic response mismatch from Gonka causes the operation to fail closed.
7. Any asset movement must conserve total assets accounting for explicitly documented rounding rules.
8. Repeated invocation of a permissionless operation must not result in duplicate payouts.
9. Factory/reply flow accepts only the compile-time reply ID `1`, requires exactly one `MsgInstantiateContractResponse`, verifies the type URL, and validates the Deal address. The single pending context cannot be overwritten; after successful indexing it is removed, and any error rolls back the entire transaction.
10. Until a payable entry point is explicitly designed, `instantiate` on both contracts rejects any native funds.
11. A Deal may only access four compile-time Gonka gRPC routes; paths from public input are prohibited.
12. Gonka responses are verified for size, participant, epoch, address, denom, and amount before being consumed by business logic.
13. Node-side gRPC error text is never parsed. Only the summary adapter preserves typed `SystemResult/ContractResult`: explicit response/availability categories may allow NetworkUnconfirmed after E+3; request and unexpected system errors remain fail-closed. Other query callers are unaffected.
14. `ListClaimRecipients` is bounded to 32 KiB encoded response and 64 entries; exceeding these limits blocks the operation.
15. Funding accepts only the configured CW20 via `Send`, only from `Open`, only before target epoch, and only with unambiguous exact `(Host, E) -> Deal` routing. The actual amount must exactly equal the immutable budget.
16. Buyer identity is taken from `Cw20ReceiveMsg.sender` only after verifying the immediate `info.sender` as the configured token contract.
17. Funding produces no outgoing payouts. An error in the Deal hook atomically rolls back the preliminary CW20 transfer; repeated funding cannot alter the first Buyer or the already deposited funds.
18. Lock is permitted only from Open/Funded in `E <= current < E+5` and requires unambiguous exact `(Host, E) -> Deal` routing. Success stores explicit `recipient_locked` proof; subsequent settlement does not depend on a pruned row.
19. Cancel is permitted only from Open without a Buyer. Before E only the Host can invoke it, and in `E <= current < E+5` — any caller upon proven missing/mismatch. Query/decode/validation failures and exact routing block Cancel.
20. The routing window uses checked `E+5`. When `current >= E+5`, absence of a row does not constitute historical proof; Lock/Cancel are rejected without mutation.
21. Lock/Cancel do not move assets and do not alter Buyer, configured terms, or financial accounting. Cancel does not release the permanent Factory Host/E index.
22. SettleClaim is permitted only from Locked with stored recipient proof and uses exact immutable Host/E performance summary. `claimed=false`, missing/malformed/oversized/mismatched responses, and query failures block the transition without mutation or payouts.
23. Settlement accounting uses `calculate_claim_settlement` and the previously accepted exact A3 budget, not the live CW20 balance. Donations do not increase original capacity/entitlements or USDT payouts. GNK donations are distributed according to the permanent shares of ADR-0010 and increase lifetime GNK payouts.
24. A positive claim transitions the Deal to Releasing with zero release counters without GNK transfers. A no-sale awards the entire GNK entitlement to the Host and produces no USDT transfers.
25. A confirmed zero claim transitions the Deal directly to Completed. The funded Buyer receives a full budget refund; gross/fee/Host net and all GNK entitlements/counters are zero. A no-sale completes without a transfer.
26. Settlement state is persisted before non-zero Host net, fee, and Buyer refund messages. Failure of any CW20 transfer rolls back state and all preceding transfers; repeated settlement is rejected.
27. Settlement does not repeat recipient queries and has no E+2/E+5 deadline: stored Lock proof remains the authoritative basis after pruning. The caller provides no evidence, amounts, or recipients.
28. The agreed policy is [ADR-0010](https://github.com/gonka24/forward-contracts/blob/d637eea5432506d60c90c1d8436c67b93802d829/docs/decisions/0010-permanent-gnk-shares.md).
29. Release distributes all available ngonka according to frozen exact shares; lifetime counters may exceed initial entitlements. Remaining vesting does not limit distributions.
30. Counters are canonical and monotonic; the sum of deltas equals available balance. Zero transfers are prohibited; wide checked arithmetic is mandatory.
31. State-before-message and full rollback semantics are preserved. Completed at U>=T does not close further release; shares/counters are not reset.
32. The legacy ForwardExcessGnk solely to Host is unacceptable when a positive Buyer share exists. Zero settlement and legitimate Refunded/Expired use Host-only fallback. CW20 and other denoms are untouched.
33. Implemented routing-failure Refund is permitted only from Funded in `E <= current < E+5` and only upon a successful validated recipient response proving missing or mismatch. Exact routing, query/decode/identity errors, and E+5+ block the transition.
34. Refund returns the immutable configured budget to the recorded Buyer regardless of live CW20 balance. The caller does not select amount/recipient; fee and Host payouts are absent; donations and other assets remain in the Deal.
35. Before CW20 transfer, Refunded, exact RefundReason, refund accounting, and HostOnly are persisted. Claim/entitlement/lifetime counters must remain initial zeroes; inconsistent Funded state is rejected. Transfer failure rolls back state and ledger; repeated/settlement calls after refund cannot pay out twice.
36. After Refunded/Expired, ReleaseUnlockedGnk distributes all available ngonka to the Host per ADR-0010. ForwardExcessGnk remains Completed-only; the Factory Host/E index is not released.
37. Concurrent Deals for different Host/epoch pairs have independent config, storage, CW20 deposits, native balances, recipients, and lifetime counters. Shared Factory/token modules do not allow one Deal to inspect or deduct balances of another.
38. Settlement, Refund, and GNK release do not withdraw unrelated CW20 or other native denoms. Directly sent unsupported assets may remain locked; this does not create arbitrary withdrawal privileges.
39. Claim-expiry Refund is permitted only from Locked with stored recipient proof, `current >= checked(E+2)`, and successful exact Host/E summary with `claimed=false`. From `checked(E+3)`, narrowly classified summary unavailability/invalidity permits a separate NetworkUnconfirmed. `claimed=true` always blocks Refund. With a Buyer, the outcome is Refunded with exact configured deposit; without a Buyer — Expired without transfer. Both persist typed reason and HostOnly before external messages.
40. `claimed=true` at any amount requires SettleClaim and blocks Refund always. Before `checked(E+3)`, any summary error blocks Refund without mutation. Starting from `checked(E+3)`, only response/availability errors enumerated in ADR-0013 permit NetworkUnconfirmed; request, config, accounting, and unexpected system errors remain fail-closed.

## Current State and Pending Implementation

Factory creates one isolated Deal per `(Host, target_epoch)` and indexes it via safe reply. The Deal accepts exact CW20 budget, stores recipient Lock proof, implements Cancel, permissionless SettleClaim, routing and positive-summary claim expiry, and E+3 NetworkUnconfirmed Refund/Expired with atomic CW20 payouts, permanent-share GNK release, late distributions from Completed/Refunded/Expired, and a compatible `ForwardExcessGnk` alias. At the extraction commit, the Gonka query boundary and financial transitions had been tested on pinned protobuf fixtures/mocks and `cw-multi-test` with real CW20/bank ledgers and test-only fault injection, and real-chain golden E2E had not yet been executed. The `forward-contracts` acceptance milestone `A8` additionally validated three Factory-created Deals across two Hosts and two epochs, segregated deposits/rewards/recipients/counters, preservation of the Factory index after Completed, and both release entry points after Completed; its evidence tracing is in [`docs/reviews/a8-contract-gap-analysis.md`](https://github.com/gonka24/forward-contracts/blob/d637eea5432506d60c90c1d8436c67b93802d829/docs/reviews/a8-contract-gap-analysis.md).

This repository is the runner for that real-chain golden E2E. Three facts about it bear on the statements above:

- No live run result is tracked in this repository; a result exists only as a run package produced by an actual execution ([`docs/evidence.md`](docs/evidence.md)).
- The catalog does not exercise `Cancel` or `ForwardExcessGnk` at all (no such execute message is sent anywhere in `scripts/` or `harness/`), and the Gonka query allowlist is not asserted by any catalog task; see the obligation mapping in [`docs/coverage.md`](docs/coverage.md) §3. A `PASSED` run says nothing about those.
- Every task is written with `acceptance_status: NOT_REVIEWED` (`forward_e2e/suite/orchestrator.py`, `forward_e2e/suite/reporter.py`); no automated path in this runner awards acceptance.

## No-Admin Deployment Gate

- Factory deployment must not assign an admin; following the transaction, deployment tooling must query chain `ContractInfo` and halt the process if `admin` is present.
- `CreateOffer` dispatches `WasmMsg::Instantiate` with `admin: None` and no funds. `cw-multi-test` verifies the actually instantiated `ContractInfo`, rejection of migration to new code with a migrate handler, and preservation of the original `code_id`. Real-chain E2E remains a mandatory release gate.
- On pinned `wasmd v0.54.2`, self-query from `instantiate` is not an adequate safeguard: keeper stores `ContractInfo` only after successful return of the Wasm entry point.
- The current integration test uses new code with a migrate handler and proves that for Factory and Deal with `admin=None`, migration is rejected and the original `code_id` is retained. This is a model of the chain, not a replacement for a real-chain query.

## A9 Release/Deployment Evidence Gate

- Build manifest, deployment config, and deployment receipt are separate versioned documents. Config cannot contain code IDs/addresses in advance, and manifest cannot substitute pinned source SHA as proof of an actively running binary.
- Release build permits only clean committed contract/build/tooling inputs, two independent pinned optimizer outputs, identical Wasm SHA-256 hashes, and successful `cosmwasm-check 2.2.2`. Missing/`not_run` evidence is not treated as success.
- Read-only preflight reconciles manifest/artifacts, chain ID, optional Gonka checkout SHA, and settlement CW20 decimals=6 prior to any broadcast. Fee bps must equal 150.
- `sync` broadcast is accepted only as a tx hash. Success requires a separate confirmed `query tx` with `code=0`; timeout/ambiguity prohibits automatic resending.
- Durable receipt blocks repeated deployment. An independent verifier re-reads code checksum, ContractInfo code/admin, Factory config, and token info. An existing Deal is verified without automatic offer creation.
- Fixtures verify fail-closed orchestration, but not real forks, keyrings, RPC, or event encoding. Details: [`docs/deployment-tooling.md`](https://github.com/gonka24/forward-contracts/blob/d637eea5432506d60c90c1d8436c67b93802d829/docs/deployment-tooling.md) and [ADR-0012](https://github.com/gonka24/forward-contracts/blob/d637eea5432506d60c90c1d8436c67b93802d829/docs/decisions/0012-a9-release-evidence-and-deployment.md).

## Gonka Query Boundary Limitations

- Pinned protobuf source does not contain the Marketplace allowlist patch; verified Gonka PR head `042758f…` adds four routes without schema changes. Release requires exact production binary SHA and real-chain verification.
- wasmd `v0.54.2` does not retain typed gRPC status accessible to the contract for regular handler errors: node-side `NotFound` is indistinguishable from other `ContractResult::Err`. Typed `UnsupportedRequest`, `InvalidRequest`, and `InvalidResponse` are preserved via raw query envelope.
- Claim-expiry `Refund/Expired` uses exact `claimed=false` summary from E+2. From E+3, summary response/availability failures from the explicit ADR-0013 matrix yield NetworkUnconfirmed. This is deliberate risk allocation, not proof of claim absence. Routing-failure refund does not use this adapter.
- Exact source `wasmvm v2.2.4` converts gas exhaustion/panic query callbacks into backend error/VM abort, rather than standard raw query results. A focused native Wasm gas regression is **not** part of this runner's catalog — the Go boundary task `go-query-error-classification` runs only `TestToQuerierResultClassifiesVMSystemErrors` and `TestStrictPlanValidation` against the runner's own fixtures (`REQUIRED_TESTS` in `scripts/run_go_boundary.py`) — and it remains mandatory before production alongside contract/submessage scenarios.
- Gonka `ListClaimRecipients` is not paginated. Contract-side byte/entry limits safeguard decode and subsequent processing, but not keeper construction of the full response. Lookahead `40` does not bound historical entries if pruning lags; chain-side gas/size bounds must still be proven in the Gonka PR.
- Funding uses successful `ListClaimRecipients` solely as positive proof of exact routing. Query/decode failures, missing, mismatched, invalid, or duplicate target entries block funding. The test native adapter in `cw-multi-test` does not prove actual Gonka runtime behavior.
- Lock uses the same positive exact proof and persists it in state. Cancel uses only successful validated `Missing` or valid mismatch; general query/decode errors are never classified as absence.
- SettleClaim uses the stored proof and does not repeat recipient queries following potential pruning. Performance summary must be positively located, identity-checked, and `claimed=true`; errors are not treated as zero claim or refund grounds.
- Release does not repeat performance/recipient/vesting queries. After checking frozen policy and canonical counters, it reads only standard `BankQuery::Balance` for `ngonka`. `TotalVestingAmount` remains diagnostic in `NativeStatus` and its error does not block payout of available bank balance.
- In pinned Gonka, locked streamvesting funds remain on the module account, and `ProcessEpochUnlocks` transfers only the unlocked tranche to the Deal. Therefore, Deal balance is spendable for A6 in the source model; real-chain spendability and `BankMsg::Send` still require the real-chain golden E2E. In this runner those are the `NATIVE` catalog tasks that execute a release on a live chain — `funded-claim`, `terminal-release-repeat`, `foreign-native-preservation`, `late-donation-after-completed`, `native-release-rollback-retry`, `no-sale-vesting-lifecycle` and `emergency-host-only-recovery` ([`docs/coverage.md`](docs/coverage.md) §2.3) — and no result of theirs is tracked in this repository.
- Pinned setter prohibits altering E starting from E, and pruner may first delete E at current=E+5. Thus the permissionless proof window terminates strictly before E+5; potential pruner lag does not expand the safe window.

## Chain Runtime Release Gate

- Pinned Gonka source uses `wasmd v0.54.2`, which falls within the affected range of [CWA-2025-007](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2025-007.md). The patched version of this branch is `v0.54.3`; the patch changes consensus behavior and requires a coordinated network upgrade.
- Prior to production deployment, the Gonka core team must provide verifiable confirmation of the patch/backport and actual binary: source revision, build metadata, and hash/attestation of the artifact run by validators.
- Local `cargo-audit` verifies Rust dependencies of the contract and cannot inspect the Go dependency `wasmd`. The absence of RustSec findings does not clear this blocker.
- As of 2026-09-07, the official advisory index also contains CWA-2026-001 and placeholders CWA-2026-002…006. CWA-2026-001 lists affected `wasmd v0.54.5`/`wasmvm v2.2.5`, rather than pinned `v0.54.2`/`v2.2.4`; this does not imply the legacy runtime is safe, since CWA-2025-007 applies independently. Placeholder entries lack scope assessment data; this verification must be repeated once details are published. Nothing in this runner automates it: a run records the selected Gonka commit and the `wasmd`/`wasmvm`/`go`/`cosmos_sdk` versions reported by the running `inferenced` (`parse_runtime_identity` in `scripts/acceptance_harness.py`); it does not consult an advisory index.

## Checks

- Unit tests verify local functions and error branches.
- Property tests verify financial invariants across a large space of inputs.
- `cw-multi-test` validates contract interactions within a simulated chain.
- Golden E2E on real Gonka validates protobuf, custom gRPC, and actual network module behavior. This repository is that layer; its result is a run package graded by `evaluate_run` (`forward_e2e/execution/outcome.py`), and its acceptance status is always `NOT_REVIEWED`.

None of these layers individually substitutes for a comprehensive security review prior to production deployment.

## Reporting a Vulnerability

The project team takes smart contract and protocol security seriously. If you discover a security vulnerability, please do not disclose it publicly or via public GitHub issues.

Please report suspected vulnerabilities privately to `support@gonka24.com` with the subject `Security: Gonka24 Marketplace`. Include the affected version or commit, reproduction steps, affected contracts, and potential impact.

When **GitHub Private Vulnerability Reporting** is enabled for this repository, you may also use **Security > Advisories > Report a vulnerability**.
