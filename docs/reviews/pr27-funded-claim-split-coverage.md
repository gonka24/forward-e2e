# Funded-claim split coverage map

This map is the migration contract for splitting the historical monolithic
`funded-claim` Testermint method. A row may be removed from that method only
after its destination task produces and the verifier requires the stated
evidence. Each destination runs on its own network and context.

| Historical check | Required preconditions | Destination task | Mandatory evidence/checkpoints |
|---|---|---|---|
| Bootstrap, funded Lock, exact recipient | Buyer present; Funded; exact Host/E route | `funded-claim` | `lock`, `Funded->Locked`, `recipient_locked=true` |
| Manual native Claim with DAPI stopped, immediate restart, resume without a second Claim | positive Host/E summary; original balances retained | `funded-claim` | `claim_prepared`, `claim_settle`, matching authoritative summary and one included claim receipt; `funded_claim_path_verified` |
| SettleClaim with selected CW20 failure, rollback, retry and terminal repeat rejection | Locked funded Deal; Host/fee/Buyer obligations | `funded-claim` | `usdt_fault_rollback`, `settlement_delivery_verified`, `claim_settle`, recipient-bound fee fault, unchanged snapshots and pending obligations, three included payouts, terminal settlement rejection; `funded_claim_path_verified` |
| Original vesting tranches release with exact Buyer/Host amounts | settled Deal; two original tranches | `funded-claim` | first `release` plus a second included attempt: remaining payout or unchanged Completed Deal with `NothingToRelease`; `funded_release_lifecycle_verified` |
| Routing mismatch and routing missing refunds | funded Deals; wrong/empty exact Host/E recipient | `funded-routing-refunds` | routing mutations, CW20 rollback/retry, refund receipts, unchanged config/balances, factory isolation |
| Positive claimed gas sweep, direct and caller/submessage OOG | claimed positive summary; pristine Locked accounting | `funded-gas-sweep` | native auto-claim, lock, gas attempts, sufficient-gas semantic rejection, unchanged accounting except fees |
| No-Buyer claim-expiry path | Open/Locked Deal; unclaimed summary; Buyer absent | `no-buyer-claim-expiry` | unclaimed precondition, early rejection, expiry refund, exact Buyer absence |
| No-sale settlement plus liquid donations before/after settlement | no Buyer at Claim; positive auto-claim | `no-sale-vesting-lifecycle` | auto-claim, settlement, exact Host/fee payout, donation accounting |
| Foreign CW20 contamination remains untouched | contaminated no-sale Deal | `no-sale-vesting-lifecycle` | contamination phase and unchanged foreign balance through settlement/releases |
| Add vesting to a non-empty schedule and release every eligible tranche only on time | existing original vesting schedule; governance addition | `no-sale-vesting-lifecycle` | non-empty snapshot, funding/proposal receipts, unlock events, negative early-release proof, final payout |
| Missing summary E+2 rejection and E+3 emergency refund | exact native NotFound; funded Buyer | `network-unconfirmed` | exact E+2/E+3 brackets, rejection then committed refund |
| Donation after terminal emergency refund; HostOnly Bank rollback/retry | Refunded terminal Deal; Buyer already refunded | `emergency-host-only-recovery` | donation, Bank restriction rollback, atomic state, retry, terminal repeat rejection |
| Unfunded Lock E+4 success and E+5/pruning rejection | Buyer absent; Open Deal | `unfunded-lock-boundaries` | exact inclusion brackets, no distribution, E+5 rejection and pruned routing |
| Funded Lock E/E+4/E+5 | Buyer present; Funded Deal | existing `lock-exact-e`, `lock-e-plus-4`, `lock-e-plus-5` | existing task-specific exact-boundary checkpoints |
| Positive/zero funded claim-expiry | Buyer present; claimed=false positive/zero summary | existing `claim-expiry-positive`, `claim-expiry-zero` | existing unclaimed and exact refund-boundary checkpoints |
| Terminal release repeat | Completed funded Deal; zero remaining GNK | existing `terminal-release-repeat` | included `NothingToRelease`, unchanged entitlements/balances |
| Late liquid donation after Completed | Completed funded Deal | existing `late-donation-after-completed` | two donations and cumulative rounding |
| Factory isolation across multiple Deals | multiple independent Host/E indices in one Factory | `funded-routing-refunds` | `factory_isolation` phase bound to both routing scenarios |

Package A keeps its native scenario. The former Package C query-fault scenario
is retired: `ct-package-c-policy` covers 71 contract-policy/cw-multi-test cases,
and `emergency-host-only-recovery` covers the two reachable native R7.2 cases.
These replacements have different proof levels; see
[`pr27-c-replacement-coverage.md`](pr27-c-replacement-coverage.md).

For `funded-gas-sweep`, the catalog also requires
`claimed_refund_gas_sweep_verified`. The offline verifier derives that checkpoint
from the claimed native summary, exact recipient, Lock receipt, complete gas
attempt matrix, OOG observations, and before/after accounting; phase names alone
cannot satisfy this row.

For `no-sale-vesting-lifecycle`, the catalog also requires
`no_sale_vesting_lifecycle_verified`. The verifier derives it from the native
claim and Lock receipts, both donation transfers, the foreign CW20 transfer and
its unchanged balance through settlement/releases, the Buyer-absent settlement
oracle, an included zero-balance `NothingToRelease` probe before vesting unlock,
the non-empty vesting addition, and two ordered release receipts. The Rust
zero-balance unit test remains supporting contract coverage; it does not replace
the native probe recorded by the acceptance scenario.

For `late-donation-after-completed`, the catalog requires both
`two_liquid_donations_verified` and `cumulative_rounding_asserted` only after
the verifier reproduces the two included donor-to-Deal transfers and GNK
release receipts, confirms the Deal was already Completed with no original or
vesting obligations, and recomputes the cumulative proportional payouts and
rounding remainder. A phase name, success code, or producer-reported delta is
not sufficient evidence.
