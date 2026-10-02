# A8: Live Acceptance Report for Prepared Packages A/B/C

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

## Current Summary: C Evidence Verified, Runner Finalization Pending

Independent review `a8cfix3-20260910` confirmed all **73 native cases**:
72 PASS rows in results.json and the final R7.2 retry in live-context.json.
Verified SHA256 of 67 receipt files (including nested R6.3), epoch brackets,
state and portfolio immutability under failure, Bank rollback/retry, and counters.

Contrary to the initial report, R7.2 retry is NOT unexecuted (NOT RUN):
- Release: height 716, code 0, tx
  `1B7B82C2452C2155EAF590E069FDEE279CE6B4F2D79560739E9087A0BA90B420`.
- Host received `94,864,721,408,887 ngonka`, Buyer `0`; counters match.
- Repeat: height 717, code 5/wasm, NothingToRelease; state/balances unchanged.
- Restriction already deactivated at height 715 (end 714).

Corrected healthy Lock is also PASS. One discarded snapshot was preserved;
there is no need to repeat blockchain scenarios merely for a missing table row.
**The automated run terminated with a 3900s timeout; final JUnit XML is absent.**
We do not alter raw results and do not report a green JUnit. What remains is an engineering
task to finalize the runner and persist reports within the deadline.

[Preserved independent review](evidence/a8-c-final-review-20260910.json).
B/R7, B/R6, and accepted A cases retain their results. This evidence
closes native cases for package C, but does not close distinct FFI/VM-boundary gates or
full production readiness. Cleanup of the latest run was confirmed.
Below is the historical progression: its NOT_RUN entries and suggestions to rerun C are superseded by this conclusion.

## Authoritative Status Following Independent Review of Recent Runs

- **B/R7.1 PASS** (`a8r7fix2-20260910`): Bank send #1 and #2 rejected with
  full rollback; after lifting the restriction, exactly one payout was executed;
  repeated Release did not alter state or balances. JUnit PASS.
- **C 72/73 PASS** (`a8cfix2-20260910`): confirmed R4 E+3 refund,
  R5 recovery/settlement, terminal checks, R6.3 CW20 rollback, and R7.2 Bank
  rollback/retry. Sole FAIL was `r3-epoch-lock-healthy`: snapshot
  crossed an epoch boundary. This was a failure of coherent evidence collection, not an
  identified contract defect. JUnit remains FAIL.
- This healthy Lock already possesses a successful receipt in `a8cfix-20260910`;
  it is preserved separately as historical evidence rather than modifying the
  outcome of the new run. Merging runs is not represented as a unified green JUnit.
- Independently reconciled 65 primary C receipts (SHA256/epoch brackets),
  rollback invariants, nested CW20 rollback, Bank retry amounts, and counters.
  Cleanup of both runs was confirmed.
- A/R1, A/R2, and B/R6 retain previously accepted results. Overall A8 status
  remains PARTIAL; this report does not close separate FFI/VM-level gates
  and does not declare production release ready.

[Independent review evidence](evidence/a8-final-reruns-review-20260910.json).
Next engineering work: stabilize coherent snapshot for healthy Lock and
verify it without losing previously accepted 72 cases. Rerunning B/R7 is not required.
Below is preserved history of earlier attempts; their TODOs are not the current plan.

Date: 2026-09-10. Marketplace `a6bbc6f9c6736c4e81fd7c8409385843db19b620`;
Gonka `b9c6ec54242109ef8a5ce7e4bdac1b992f78f2dc`. Each run utilized an isolated
immutable Linux snapshot; standard scoped cleanup was executed after preserving evidence.

| Selector | RunId | Status | JUnit | Outcome |
| --- | --- | --- | --- | --- |
| `package-a-r1-r2` | `a8abc-a-20260909b` | R1 behavior confirmed by review; R2 PASS | 1 / 1 failure | E+5 Refund correctly rejected; corrected oracle accepts historical receipt. R2 executed all checkpoints and both release tranches of gift. Original JUnit FAIL preserved. |
| `package-b-r6-1` | `a8abc-b6-20260909` | **PASS** | 1 / 0 failures | Documented CW20 send #1/#2/#3 failures, rollback, fault removal, single settlement, and terminal repeat. |
| `package-b-r7-1` | `a8abc-b7-20260910` | PARTIAL | 1 / 1 failure | First Bank rollback proven. Approved amount for second step became stale after additional vesting unlock; second rollback and retry were not executed. |
| `package-c-query-faults` | `a8abc-c-20260910` | PARTIAL | 1 / 1 failure | First activation and early R3/R4/R5 proven. After restart at `recover-before`, readiness polling immediately aborted; late cases not completed. Later, all nodes reached height 439 with identical app hash. |
| `package-b-r7-1` | `a8r7fix-20260910` | **PARTIAL / FAIL (harness)** | 1 / 1 failure | Full unlock and first Bank rollback proven; verification of `exemption_usage_tracking` stopped second send prior to execute. |
| `package-c-query-faults` | `a8cfix-20260910` | **PARTIAL / FAIL** | 1 / 1 failure | All three activation/recovery stages installed, but preliminary check of late phase observed epoch 6 (E+2), so refund was not dispatched. Therefore, E+3, terminal, and dependent recovery/retry checks were not proven in that run. |

## Evidence

Raw evidence and JUnit reside in WSL snapshots:

- `/home/runner/a8-runtime/a8abc-a-20260909b/{evidence,junit,cleanup-evidence}`
- `/home/runner/a8-runtime/a8abc-b6-20260909/{evidence,junit,cleanup-evidence}`
- `/home/runner/a8-runtime/a8abc-b7-20260910/{evidence,junit,cleanup-evidence}`
- `/home/runner/a8-runtime/a8abc-c-20260910/{evidence,junit,cleanup-evidence}`
- `/home/runner/a8-runtime/a8r7fix-20260910/{evidence,junit,cleanup-evidence}`
- `/home/runner/a8-runtime/a8cfix-20260910/{evidence,junit,cleanup-evidence}`

For C, key files are: `c-runtime-build.json`, `package-c/results.json`,
`live-context.json`, and per-transaction `package-c/tx-*.json`. First plan
installed at height 319: binary SHA-256
`adcc39ecd620196f649f6540a5652efb58932543ce54497fbbff64db94916d0c`, plan
SHA-256 `50bbd0ef284c9be78eda28ec23066871a2338363efef74bbfb63902317443627`.
Recovery plan at height 427 remained `INSTALLING`, not `INSTALLED`.

All six cleanups were confirmed by their corresponding
`cleanup-evidence/completed.json`. Docker resources without verified ownership
were not removed.

## Continuation Following Harness Fixes (2026-09-10)

Snapshot source: Marketplace `3a4f1f4d22c19191f44c35e33f9fdd0786a263e6`,
Gonka `6a09808e512f73e9809c845a93e7871f20e539b8`. Both working trees were clean
prior to execution. In this iteration, exactly one B/R7 and one C run were executed;
A and B/R6 were not rerun.

- **B/R7:** In 22m20s, full unlock and first Bank rollback were proven.
  Tx `26437ABCCAFB49582787666EA546DA4A6EDB1A5C4BB9C4D9D6AA52530032F814`,
  height 244, code 1108/restrictions; before/after state and tracked balances were identical.
  Second send halted prior to execute: `restrictions params lack exemption_usage_tracking`.
  The field exists in pinned protobuf; harness error message alone does not prove
  absence of the field in native schema. Representation of empty repeated fields
  needed normalization while retaining rejection of malformed values.
- **C:** In 51m42s, 73 entries were recorded: 37 PASS, 11 FAIL preconditions, 25 NOT_RUN.
  All three activation/recovery stages were INSTALLED. Independently reconciled SHA256 and
  epoch brackets across 34 receipts, and for failures verified immutability of config/state/CW20 and
  native portfolios. The remaining three PASS were native claim/ledger observations.
  With target E=4, the late phase at heights 525–530 observed effective epoch 6 (E+2),
  while 7 was required. Errors arose BEFORE sending refund; this was not a contract failure.
  Kotlin waits for latestEpoch.index, Python checks contract-effective epoch:
  synchronization on the identical epoch source was needed prior to a new run.
  Successful runtime transitions did not close dependent R5 settlement,
  terminal, R6.3, or R7.2 checks.
- Cleanup of both new runs confirmed with `status: cleaned`; SHA-256: B/R7
  `1eaee128b6a7e6aa780f8c9ee2bc291dda7e8f8a8b3498f42c9f783265f98ed9`, C
  `ca5cdd09ddbd24c9996fe331c29f3466f4570e3bb4aa72321e274552d4ba3498`.

New live acceptance was not closed: fixing B fixture schema and C epoch
scheduler/waiting logic was required, followed by scheduling fresh single-run acceptance attempts.

## What Has Been Proven

- **STOCK-NATIVE:** B/R6 successfully verified real contract-to-CW20 outgoing
  transfer failures across three positions, rollback, and retry.
- **NATIVE-FAULT (partial):** C successfully recorded native query-fault R3
  routing/current-epoch and R4 E+2 variants; `package-c/results.json` contains
  PASS for executed early cases and real DeliverTx/evidence files.
- **Additionally Confirmed by Review:** A/R1 against historical receipt with corrected
  oracle; A/R2 across full lifecycle of new gift; B/R7 first Bank rollback.
- **Not Proven:** Green JUnit for package A, B/R7 second rollback and retry, late C R3/R4 E+3, R5 recovery,
  R6.3, and R7.2. FFI/VM-boundary cases remain a separate tier and are not claimed as
  native PASS.

## Causes and Next Steps

1. A oracle is fixed; historical receipt was reverified offline. R2 is preserved
   as PASS. A new run of A merely to transfer these results is unnecessary.
2. B: Full unlock is already confirmed. Fix handling of empty usage tracking with
   offline regression against real response format; then execute new B/R7 run.
3. C readiness now waits for temporal sync within deadline, preserving per-node
   diagnostics. R5 moved to start of E+2, followed by recovery-before and R4.
   Specifically for C: 75-block epochs and 65-minute timeout per test. A new C snapshot
   is needed strictly AFTER fixing effective epoch waiting; the old snapshot
   must not be modified or resumed.

[Saved results](evidence/a8-abc-reviewed-20260910.json) and
[independent review](a8-abc-live-results-review.md) contain refined conclusions.

Overall A8 acceptance and production readiness **are not upgraded**: unresolved
mandatory live rows remain.

## Offline Verification of Fixes (Prior to the Two Reruns)

- Python: 123 tests, 122 PASS, 1 platform skip.
- Local launcher: 8 tests PASS.
- Testermint `compileTestKotlin --offline`: BUILD SUCCESSFUL.
- Gonka provenance: PASS for `6a09808e512f73e9809c845a93e7871f20e539b8`.
- `git diff --check`: PASS in both repositories.
- No new live runs were launched. These checks confirm harness fixes,
  but do not close remaining B/R7 and C live scenarios.

## Fixes Following Repeated B/R7 and C Runs

Harness commit: `05e2f37`. Empty repeated `exemption_usage_tracking` is
normalized from null/missing field into a list; invalid types and exhausted
usage are rejected. Before early-r5/early-r4/late, C waits for the exact effective epoch
via `inference get-current-epoch` up to 600 seconds. Observations are recorded in
`epoch_waits`; skipped epochs and timeouts halt the phase before execute.
Kotlin latestEpoch remains preliminary waiting; transaction epoch
brackets are preserved. Offline: 125 tests, 124 PASS, 1 platform skip.
The network was not run, and historical FAILs are not rewritten. Next step: new
B/R7 snapshot/run, cleanup, then new C snapshot/run.

## Live Rerun Outcomes Following `05e2f37`

New immutable snapshots were used: Marketplace `0a33f7d4b9b8d78db94611838ff2d2baaf5c528a`,
Gonka `6a09808e512f73e9809c845a93e7871f20e539b8`.

| Selector | RunId | Status | JUnit | Outcome |
| --- | --- | --- | --- | --- |
| `package-b-r7-1` | `a8r7fix2-20260910` | **PASS** | 1 / 0 failures / 0 errors | Empty-usage normalization enabled execution of Bank rollback and one retry; JUnit time 1492.877s. |
| `package-c-query-faults` | `a8cfix2-20260910` | **PARTIAL / FAIL** | 1 / 1 failure / 0 errors | 72 of 73 case records PASS; sole `r3-epoch-lock-healthy` failed closed: snapshot crossed vesting epoch, preventing coherent balance proof. |

B: JUnit SHA-256 `84e8b8cfbd19f6c882f46a11e7fbeeb1e5ca90fdf759395af58ac9d9040b0583`, live-context SHA-256 `7955ec0565a4e184cf7063d59b88082df2e248f8286fc30d4f4c7ebc99550d86`.
C: `results.json` SHA-256 `a0a52bde88faaeccff71572db4248ce6a95cdd164f5f22e336e66159917bc353`, JUnit SHA-256 `4e8420694f9486736eef155937ff5a416d489acd695de0acee046f8d7caf7773`.
Raw evidence: `/home/runner/a8-runtime/a8r7fix2-20260910/{evidence,junit,cleanup-evidence}` and `/home/runner/a8-runtime/a8cfix2-20260910/{evidence,junit,cleanup-evidence}`.

Scoped cleanup confirmed for both RunIds (`status: cleaned`): B SHA-256 `6950e2f95db3dea840bdd1440e4503e53b80655924bb3b5709af7834e9dfeded`, C SHA-256 `512de54460f95597cce7690e309f2e43c6613215b7791c984f7ed47c5a3a744c`. Third-party Docker resources were not removed.

Next mandatory work: make `r3-epoch-lock-healthy` resilient against vesting boundaries without weakening balance/state proofs, and execute a fresh standalone C run. Overall A8 acceptance remains PARTIAL; B/R7 is now closed, C is not.

## Coherent Snapshot Fix for Final C Case

The controller retries strictly the snapshot read if its start and end fall into
different epochs: up to three full reads. Discarded candidates are recorded in
results.json/discarded_snapshots; blending data across attempts is prohibited.
Other query errors are not retried. Execute transactions are not retried.
State/balance checks and inclusion epoch brackets are preserved.
Offline: 127 tests, 126 PASS, 1 platform skip. No new live result.
Next run: strictly selector `package-c-query-faults`, new Run ID, and
immutable snapshot via Start-A8.ps1. Repeating A and B is not required.

## C Live Rerun with Coherent Snapshot (`41489d5`)

RunID `a8cfix3-20260910` used Marketplace `41489d5e599fb8e7a537312fb84ea2bedda0507a` and
Gonka `6a09808e512f73e9809c845a93e7871f20e539b8`.
All three activation/recovery stages were installed. Of 73 mandatory C cases, evidence contains **72 PASS**, **0 FAIL**, **1 NOT RUN**: `r7.2-retry` was not recorded before the hard launcher timeout of 3900 seconds. This is not a contract failure and not an acceptance PASS.

The new coherent snapshot logic functioned as designed: one candidate was recorded in `discarded_snapshots`, after which the controller resumed reading; execute transactions were not retried. However, the final terminal/retry portion exceeded the overall time limit.

Raw evidence: `/home/runner/a8-runtime/a8cfix3-20260910/{evidence,junit,cleanup-evidence}`. `results.json` SHA-256 `2906cc1a3cbf49a2d9fe9a466fdeef2895fd2cf4289fc6bd63dfa37c5d41711c`; live-context SHA-256 `34dc52d515a8194c529babe4f1f1c74f5988f448b2fec28bc742118f6bae5c20`; launcher log SHA-256 `fe8f1edc7e6bafaaf23dae9052b6cf29f2bb83c17f4988bbb785a6a2335133fe`.

JUnit XML was not published: the Gradle process was forcibly terminated by launcher timeout. Scoped cleanup completed with `status: cleaned`, SHA-256 `66b41dffc0b9fd5582abe2ee9b77e9a2b16fa91409e7e97d7bcd602b909bd190`.

Overall A8 acceptance remains PARTIAL. Closing C requires an independent task: preserving all assertions while fitting terminal/R7.2 retry inside the real timeout; this RunID must not be rerun.
