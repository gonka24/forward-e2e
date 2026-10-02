# Review of A/B/C live results — 2026-09-10

Reviewed Marketplace `a6bbc6f9c6736c4e81fd7c8409385843db19b620` and Gonka
`b9c6ec54242109ef8a5ce7e4bdac1b992f78f2dc`. No source changes were made by
the operator; its report is an untracked Markdown file. This review did not
start a network, modify snapshots, or change existing acceptance evidence.

## Verified outcome

| Run | Result after inspecting evidence |
| --- | --- |
| a8abc-a-20260909b | JUnit failed due to the R1 error-string assertion. R1 raw receipt supports the intended rejection and unchanged financial state. R2 completed all four checkpoints and both new vesting releases. Do not discard R2 because the package's JUnit is red. |
| a8abc-b6-20260909 | PASS for R6.1: three distinct nonzero CW20 payout positions fail and roll back; clearing faults permits exact settlement; included repeat rejects. Stock Gonka with a deliberately faulting test CW20. |
| a8abc-b7-20260910 | PARTIAL: first Bank-send rejection/rollback is recorded. The second-send fixture fails its precondition before execute; second-send rollback and successful retry remain unproved. |
| a8abc-c-20260910 | PARTIAL: 29 recorded PASS subchecks, including 27 included failed execute receipts and two real native claim checks. Later recovery/E+3/terminal/Bank cases did not complete. |

All four A9 manifest hashes match the references in their live contexts.
All four runs retain JUnit plus `cleanup-evidence/ownership.json` and
`cleanup-evidence/completed.json` with status `cleaned`.
For C, independently checked all 27 transaction evidence file hashes, included
failure codes, epoch brackets, unchanged config/state/CW20/foreign CW20 and
unchanged native portfolios. Both plan file hashes match activation records.
This is evidence review, not a new execution or full acceptance promotion.

Raw root: `/home/runner/a8-runtime/<RunId>/`.
Each context is at `evidence/<RunId>/live-context.json`; JUnit is at
`junit/TEST-MarketplaceContractAcceptanceTests.xml`.

## Corrections needed in the operator report

1. R2 is supported by completed native evidence. New gift = 10,000,000,001 ngonka,
   split into native vesting tranches 5,000,000,001 and 5,000,000,000. Release
   heights 285 and 309 have code 0. Cumulative extra Buyer = 1,055,134 and
   Host = 9,998,944,867. Sum equals the gift and the independent fixed-share
   formula; entitlements and settlement CW20 are unchanged. Pre-gift, fully
   locked, first-unlocked and final checkpoints all exist. No new live run is
   necessary merely to recover this result from the package failure.
2. R7 first-send rollback is present: tx
   `E446627C7703AAF9D39A928731BAA344A2A71F1B8990A67A5C825B322DEE6EB4`,
   height 198, DeliverTx code 1108 / restrictions, exact native restriction
   error, before == after. The report incorrectly says neither send was proved.
3. C stopped after installing the recovery plan and starting nodes, during
   readiness observation. It did not fail the initial pre-recovery ownership
   or healthy-node gate. Suggesting only another check *before* recovery does
   not fix the failing wait path. The raw failing status response was not
   persisted, so the exact transient value cannot be reconstructed from the
   generic error alone. Later saved node logs show all three at height 439 with
   executed app hash `0AD2C734A82BF5562D70D1023F8F07BBB26FD811BEC0B4B781D0FD0FCF566D2C`.
   This supports recovery of the network, not completion of the missing cases.

## Harness findings before another run

### A: wrong expected error text

`scripts/a8_acceptance.py::assert_refund_window_closed` expects a substring
that does not match the production `refund routing-proof window is closed`.
R1 raw tx `3E412F12AFA44E51DC3D27A6BAB43E6DEA6F0597A82302764FD0F3FAD0AD945C`
was included at height 259 in effective epoch 10 for E=5, code 5 / wasm;
balances/state stayed unchanged and the exact correct routing-window rejection
is retained. Fix the expected string and regression test; keep the original
JUnit failure and annotate the retrospective evidence conclusion separately.

### B: stale amount in a time-dependent exemption fixture

The stored plan allowed Buyer 50,000,000,000 ngonka using the first liquid
tranche. The JUnit logs then record another 47,409,835,683,225 ngonka unlocking
on this exact Deal at height 205. The current Buyer payout consequently grows
to 100,000,000,000, while the prepared exemption still covers only half.
`assert_live_bank_second_send_exemption` correctly rejects that stale limit.
This is not absence of Gonka's exemption interface and not a contract failure.
Prepare the fixture after original vesting is fully unlocked, or otherwise
prove a sufficient exact-recipient allowance for the entire planned interval.
Keep the live second-send precondition; do not weaken it to obtain PASS.

### C: readiness poll aborts on its expected intermediate state

`scripts/a8_query_faults.py::activate` has a bounded readiness loop, but calls
`node_height`, which throws immediately when catching_up is not false. That
exception exits the whole loop instead of waiting within its deadline.
The first plan was confirmed at height 319; the second was scheduled for 427
and retained as INSTALLING. Fix the distinction between temporarily catching
up, invalid status, transport failure and deadline expiry, retaining per-node
observations. Preserve the before-E+3 assertion and immutable historical plans.

Also account for the observed time budget before rerunning: C consumed 38m49s
before late phases, against a 45-minute test limit. The final E+2 rejection was
at height 418 and recovery was scheduled at 427. Schedule R5.1 recovery with
explicit margin against the **effective** epoch boundary; do not infer this
boundary merely by dividing height by epoch length. Fixing readiness alone
does not establish sufficient epoch or overall execution time for late cases.

No new contract defect was established by these runs. Full A8 and production
readiness remain incomplete. Preserve verified subcases before planning further
network work; rerunning all four packages would waste the completed evidence.
