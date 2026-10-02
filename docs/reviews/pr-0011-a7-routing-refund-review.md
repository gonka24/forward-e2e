# A7 Routing Refund — Implementation Review Evidence

- Date: 2026-09-07.
- Base: `origin/main@37869889849a758781e4584ae83f31e618d5eec1`.
- Scope: contract/API diff, unit and integration tests, generated schema, ADR-0011 and related README/SECURITY/spec/backlog/chain docs.
- Status at time of review: Draft. Subsequently PR #11 was merged as a self-contained routing-only slice; native claim-expiry gates and complete A7 remained open.

## Review Checklist

| Invariant | Pinned Gonka Constraint | Audit Pattern / Finding | Decision | Test |
|---|---|---|---|---|
| Empty routing does not equal query error | List query returns read error; successful empty list is separate response | OroSwap HAL-01: permissionless griefing | Only validated Missing/Mismatch; errors fail closed | unit evidence matrix; cw-multi-test balance matrix |
| Routing history provable only before pruning | Setter forbids E<=current; threshold 5, native 95/96 test | Astroport timing/gas findings as risk prompt | `E <= current < E+5`, checked add | E-1/E/E+4/E+5 and overflow |
| Refund does not depend on live balance | A3 already accepted exact configured deposit | DAO DAO finding 2 + `fadf2e4…` exact funding | Transfer immutable budget | direct CW20 donation remains in Deal |
| No partial state or payout | CosmWasm transaction rollback | DAO DAO accounting-before-message pattern | Refunded/reason/accounting/HostOnly before message | fault CW20 rollback + one retry |
| Permissionless caller does not select outcome | Host/E/Buyer/budget immutable | OroSwap griefing comparison | Empty execute; recipients/amount contract-owned | arbitrary callers, repeat rejected |
| Zero or extraneous assets are not withdrawn | Bank/CW20 asset namespaces separate | Astroport finding 8 zero send | One nonzero refund; no-sale blocked | ngonka/uatom/CW20 donation preservation |
| No-claim does not withdraw funds without evidence | Summary getter folds errors; claimed write error swallowed | State consistency check | Locked always rejected in this PR | claimed false/true, zero/positive, funded/no-sale |
| ADR-0010 after Refunded | HostOnly 0/1 fallback already reviewed in PR #10 | Cumulative accounting patterns | Frozen policy, status remains Refunded | late ngonka -> Host; alias rejected |

## Native Blockers Verdict

The four dependencies of full A7 are not closed on the exact Gonka SHA. Detailed source and test evidence is recorded in ADR-0011. This PR contains no new protobuf, no gRPC error text parsing, no default-response inference, and no modification of the pinned SHA. `Expired` is unreachable.

## A6 Review Note

The PR #10 review note was verified against current main: `deal_completed` is emitted upon confirmed zero SettleClaim and upon the first positive-claim transition from Releasing. The specification already describes both paths; code modifications were not required.

## Validation Record

On the final implementation head, the following passed locally:

- Reproducible protobuf generation without diff and schema with expected `refund_reason`/`RefundReason` changes;
- `cargo fmt --all --check` and separate fmt generator;
- Workspace and generator `clippy -D warnings`;
- 112 workspace tests: 4 protobuf golden, 20 common, 45 Deal unit, 9 Factory unit, and 34 Factory integration; generator tests also passed;
- Release Wasm build and `cosmwasm-check` for both contracts;
- `cargo audit` and `cargo deny` for both lockfiles with no vulnerabilities or prohibited dependencies; only previously resolved policy warnings were emitted.

Golden Gonka E2E was not performed and is not substituted by fixtures/mocks. PR CI is verified separately post-push.
