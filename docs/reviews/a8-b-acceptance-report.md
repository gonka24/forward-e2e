# A8/B: Acceptance on Local Gonka

## Historical acceptance status for the recorded A8 source and runs

This report preserves the 2026-09-10 review of earlier source revisions. Its
acceptance decision applies only to the recorded commits, scenarios and receipts;
it is **not** a current acceptance verdict for PR #27's immutable-source runner
or its head commit. That runner requires its own full run package and review as
described in [`ops/e2e/RUNBOOK-immutable-sources.md`](../../ops/e2e/RUNBOOK-immutable-sources.md).

The agreed scope of verification is complete; evidence is accepted. No
additional live runs are required to close those historical MRs. Archived row-by-row matrix:
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

Current row-by-row reconciliation of requirements, tests, source SHAs, and evidence:
[final coverage matrix](a8-final-coverage-matrix.md). It refines the earlier statement
"all native closed": R1–R7 are accepted, but strict native attribution of C1 exactly at E
was not established; VM/FFI remain open. B2/vesting and R6.2 are confirmed.

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

## Review of Repeated B/R7 and C Runs (2026-09-10)

[Detailed report](a8-abc-live-acceptance-report.md) and
[verified evidence](evidence/a8-abc-reviewed-20260910.json) updated.
B/R7 re-proved the first Bank rollback and full unlock; second send was halted by schema guard before execute. C: 37 PASS / 11 precondition FAIL / 25 NOT_RUN; all three runtime switching stages installed. Late refunds were not sent: effective epoch was still E+2, though Kotlin had already completed waiting for latestEpoch. Both JUnits remain FAIL, cleanup confirmed. These halts do not prove new contract defects; overall status remains PARTIAL. Prior to repeat, fixes to the usage tracking parser and waiting for contract-effective epoch are required, not timeout increases.

## Packages A/B/C: Preserved Results and Harness Fixes (2026-09-10)

[Verified evidence](evidence/a8-abc-reviewed-20260910.json) preserves source SHAs, links/hashes of raw context and JUnit, transactions, and accepted subcases. [Review](a8-abc-live-results-review.md) explains reasons for halts.

- A/R1: Behavior confirmed by real E+5 transaction; corrected check accepts historical receipt. Original JUnit FAIL was not rewritten.
- A/R2: PASS — new vested gift completely distributed after Completed.
- B/R6.1: PASS — three CW20 rollbacks, settlement, and repeat rejection.
- B/R7.1: PARTIAL — first Bank rollback exists; second and retry not yet proven.
- C: PARTIAL — 27 verified execute rejections and two positive native claims; late E+3/recovery/R6.3/R7.2 remain open.

Fixed stale error string in A, vesting-dependent exemption in B, and readiness polling in C. R5 is now checked/recovered before R4; C epoch/timeout headroom increased based on actual duration of first run. No new live runs were executed during fixes. Overall A8/production readiness is not upgraded to PASS.

## B2 / Late Donation After Completed PASS (2026-09-09)

Focused run `b2-late-005` progressed one funded Deal with positive native claim to `Completed`: original claim `94819671366450ngonka` was completely released, both original remaining counters and liquid/pending vesting were zero. Host, Buyer, fee recipient, caller, Deal, and donor were six distinct addresses.

Shares fixed as Buyer `100000000000 / 94819671366450`. Real Bank donations of `1423ngonka` each and successful `ReleaseUnlockedGnk` (code 0) yielded: first pair `Buyer +1 / Host +1422` (`FDF1D496…AADC`, height 217), second pair `Buyer +2 / Host +1421` (`63E25F2C…731D`, height 220). Independent rounding of a single donation would have yielded Buyer only `1`; the second payout of `2` is necessary and sufficient to prove cumulative rounding with carried remainder.

Following each release, sum of payouts equals `1423`, Deal liquid GNK returns to zero, status remains `Completed`; frozen policy and original entitlements were unchanged. Settlement/foreign CW20 and vesting state were unmodified, so there was no new USDT settlement or Buyer surcharge. JUnit: 1/1, failures/errors/skips = 0; standard cleanup completed. [Compact B2 evidence](evidence/a8-b2-late-donation-completed-005.json).

## B3 / Native Denom PASS (2026-09-09)

Focused run `b3-native-004` proved the native-denom portion of B3 on an independent immutable Linux snapshot. Opt-in genesis fixture created exactly `12345ua8b3foreign` via standard `genesis add-genesis-account`; Bank tx `0FA837…856B0` (height 189, code 0) transferred the entire sum to Deal. Subsequently, real `ReleaseUnlockedGnk` `001423…65E4` was included at height 191 / epoch 7 with code 0. Independent oracle matched actual results: from `47409835683225ngonka`, Buyer received `50000000000`, Host received `47359835683225`, and counters increased by those exact sums.

Following release, Deal preserved all `12345ua8b3foreign`; Host, Buyer, caller, and fee recipient held zero of this denom. No Deal transfer with this denom appears in the tx; settlement CW20 and foreign CW20 were unmodified. Addresses of Host/Buyer/fee recipient/caller were distinct; native fee in this local configuration was zero, so caller delta = 0 and fee recipient was also unchanged. JUnit: 1/1, failures/errors/skips = 0. Compact evidence: [B3 native-denom PASS](evidence/a8-b3-foreign-native-004.json).

This closes strictly the native-denom leg. Full B3 conclusion across both asset types continues to rely also on the already recorded foreign-CW20 snapshot from run17.

## E1 Missing-Summary Emergency Refund PASS (2026-09-09)

The isolated E1 harness was prepared without changing production code. Its native oracle accepts absence of the exact Host/E summary strictly upon actual CLI `code = NotFound, desc = not found`, concurrently validates persisted recipient Deal, and binds the observation to exact E+2/E+3. At E+2, contract-level `Gonka query failed` with full rollback is expected; at E+3: `network_unconfirmed`, `Refunded`, 100% Buyer, 0 Host/fee, Deal CW20=0, and `host_only`.

Focused run `stable-e1-002` on a native Linux snapshot closed both boundaries without changing production code or chain state. A separate Deal with deposit `10,000,000` micro-USDT was funded and Locked for Host/E=`7`; recipient query returned exact Deal, and Lock `C01508…00A6D` was included in E=7/height 184. At E+2=9, native CLI for exact Host/E returned real `NotFound/not found` with functional transport. Refund `500023…463A` was included in the same E=9/height 234 with code 5 and contract-level `Gonka query failed`; state remained `Locked`, refund accounting zero, and all CW20 balances unchanged.

At E+3=10, exact summary still returned `NotFound/not found`. Refund `6986C8…20C39` was included in the same E=10/height 259 with code 0 and events `reason=network_unconfirmed`, `status=refunded`, `amount_micro_usdt=10,000,000`. Buyer received full deposit, Deal CW20 became 0, no separate Host/fee transfer was made; state contains `buyer_refund_usdt=10,000,000`, `host_net_usdt=fee_usdt=0`, and `gnk_release_policy=host_only`. Buyer and Host in this focused run share a single chain address: exact outflow/events prove the single Buyer transfer and absence of a second Host payout, but independent address-level Host delta is not separately observable. JUnit: 1 test, 0 failures/errors/skips. Compact evidence: [stable-e1-002 PASS](evidence/a8-network-unconfirmed-stable-e1-002.json). Raw evidence and JUnit preserved in `/home/runner/a8-runtime/stable-e1-002`; earlier diagnostics remain in [runs 045–046](evidence/a8-network-unconfirmed-045-046-diagnostic.json).

## D1-Zero Special-Genesis PASS (2026-09-09)

Following approval to modify standard genesis parameters for an isolated test network, the minimal route was selected: strictly `inference.params.bitcoin_reward_params.initial_epoch_reward` was modified from production default `285000000000000` to `0`. Pinned native binary `29a58fcf…69b1` previously validated this file via `inferenced genesis validate`, and live query confirmed the parameter on the running network. Production code, chain state, and summaries were neither edited nor substituted. Separate evidence: [param validation](evidence/a8-claim-expiry-zero-param-validation.json) and [D1-zero PASS run044](evidence/a8-claim-expiry-zero-044.json).

In target E=5, Host remained `ACTIVE`, held standard PoC/confirmation weights `10/10`, but under zero subsidy and absent workload, unmodified `SettleAccounts` directly produced an existing exact Host/E summary. Proto3 JSON omitted default fields `earned_coins`, `rewarded_coins`, and `claimed`; the strict oracle decoded them as `0`, `0`, `false`, without conflating the summary with an absent one.

Run044 — `PASS`: Lock tx `C02E31…84B3` included in E=5/height 133. At E+1=6, Refund tx `6E581B…7934` included at height 159 with code 5 and exact `claim-expiry window has not opened`; Locked state and deposit `10000000` were unchanged. At E+2=7, Refund tx `D35BC1…88FC` included at height 184 with code 0 and `reason=claim_expiry`: Buyer balance returned from `90000000` to `100000000`, Host and fee recipient remained at 0, Deal CW20 became 0, state — `Refunded`, GNK policy — `host_only`.

This proves native D1-zero under a dedicated valid test configuration, without asserting reachability under current production subsidy. Earlier investigation of unmodified run043 configuration is preserved as [source feasibility](evidence/a8-claim-expiry-zero-source-feasibility.json).

## Focused D1 Continuation Runs 040–044 (2026-09-09)

Standard funded claim-expiry under positive exact `claimed=false` summary was closed by run `a8-claim-expiry-positive-043`; zero-reward sub-case was closed by separate run `a8-claim-expiry-zero-044` under an explicitly recorded special-test genesis. The rows below are separated so that positive PASS does not obscure provenance of zero PASS. Full A8/B matrix and production release gate are not upgraded.

`a8-claim-expiry-positive-040` reached funded and Locked Deal for Host `join1`, stopped its DAPI prior to auto-claim, and obtained authoritative summary for epoch 5: `rewarded_coins=94819671366450`, `claimed=false`. Proto3 JSON correctly omitted field with default `false`, but the earlier oracle erroneously required its literal presence. Fix `7c80e98…` now decodes absent bool as `false`; both Refund transactions in this attempt remained `NOT RUN`.

`a8-claim-expiry-positive-041` applied the corrected oracle, but Testermint failed during creation of `prod-local/mock-server/genesis/mappings`, prior to genesis and Deal operations. Rapid inspection identified not a race condition, but cross-provider failure on the reused Windows checkout: WSL lookup returned `ENOENT`, WSL `mkdir` returned `EEXIST`, while Windows API could create the same path. Docker recreated the tree as root, after which WSL encountered `EACCES`. Fix `98ef060…` always prepares the tree via Docker as root, then returns ownership to current Unix UID/GID. Fresh filesystem probe and runs 042/043 confirmed the fix.

`a8-claim-expiry-positive-042` reached settlement and re-proved exact positive unclaimed summary, but a 120-second poll initiated at start of E expired immediately prior to summary generation during transition to E+1. Refund remained `NOT RUN`. Fix `e9eb8f6…` explicitly waits for E+1 before verifying summary and dispatching early Refund; no new retries or sleeps were added.

`a8-claim-expiry-positive-043` — `PASS`: summary Host/E=5 with `rewarded_coins=94819671366450`, `claimed=false`; Refund `841556…FB4C` included at E+1=6/height 160 with code 5 and exact `claim-expiry window has not opened`, while Locked state and deposit `10000000` remained unchanged. Refund `8D83F6…8D3A` included at E+2=7/height 184 with code 0 and `reason=claim_expiry`: Buyer received full `10000000`, Host and fee recipient received 0, Deal CW20 balance became 0, final state — `Refunded`.

Compact evidence: [attempt 040](evidence/a8-claim-expiry-positive-040-diagnostic.json), [attempt 041](evidence/a8-claim-expiry-positive-041-diagnostic.json), [attempt 042](evidence/a8-claim-expiry-positive-042-diagnostic.json), [positive-unclaimed D1 PASS 043](evidence/a8-claim-expiry-positive-043.json), and [zero-unclaimed D1 PASS 044](evidence/a8-claim-expiry-zero-044.json). Raw logs preserved in run-specific paths indicated there. After capture, only verified Testermint containers/volumes, `chain-public`, and run-specific `prod-local` were deleted; third-party Docker resources remained untouched.

## Live Continuation Runs 016–017 (2026-09-09)

Recent runs do not close full A8/B acceptance and do not constitute deployment approval. They provide native evidence and record oracle constraints.

`a8-negative-016`: exact Bank donation event concurrent with vesting unlock; `current_epoch=15 >= E+3`, `claimed=true`, 14 gas attempts, 2 OOG, sufficient-gas rejection `native claim ... already confirmed`; no-sale release, pruned Lock rejection, and Factory isolation PASS.

`a8-negative-017`: vesting + donation PASS after event oracle; gas PASS; network Refund tx `3D05…FE83` at E+3; no-Buyer terminal `expired/claim_expiry` without CW20 transfers. Full run stopped by oracle errors; E+2 and reproducible ordinary `claimed=false` remain NOT RUN.

Evidence: `evidence/a8-negative-016-diagnostic.json`, `evidence/a8-negative-017-diagnostic.json`, and corresponding cleanup manifests. For coincident Host/Buyer addresses, expected CW20 delta is computed by address, and for donations exact amount is proved by Bank `transfer` events.

- Date: 2026-09-08.
- Functional slice outcome: `PASS` for funded claim/settle/release lifecycle and P0.
- Production release outcome: `NO-GO` due to open native/fault matrix and applicable CWA-2025-007 in running `wasmd v0.54.2`.
- Marketplace base: `bffb60c0fdabda9d6d22a1cc9924fbe32a91cbac`.
- Marketplace acceptance/oracle commit: `e369b48bd2640c79161f9566cc507c51307e813c`.
- Gonka production runtime: `29a58fcf64b87967cb874b6169ce2c61e1f269b1`.
- Testermint harness: `9f32e0274a3e22ddb567ea024820cc2522a250db`, published in [Gonka PR #2](https://github.com/gonka-ai/gonka/pull/2) against `forward-marketplace-blockchain-prep`.
- Contract protobuf provenance: `379bebced638aeb5e6077bfd51c986f898443832`.
- Evidence run: `a8-funded-009`; local raw evidence: `artifacts/a8-evidence/a8-funded-009/live-context.json`.
- Reviewable compact evidence: [a8-funded-009.json](evidence/a8-funded-009.json).
- Independent clean-checkout rerun from the published Gonka branch: [a8-funded-010-clean-checkout.json](evidence/a8-funded-010-clean-checkout.json), result `PASS`.
- Independent settlement-oracle rerun from a fresh checkout: [a8-funded-012-settlement-oracle.json](evidence/a8-funded-012-settlement-oracle.json), result `PASS`.

`PASS` below signifies a transaction included in a block with `code=0` and validated state/balance deltas. `PARTIAL` is not inflated to `PASS` on account of contract-only tests. `NOT RUN` is explicitly retained: this report does not represent mocks or cw-multi-test as native evidence.

## 1. Isolation and Runtime Provenance

Testing was conducted in WSL2 Ubuntu 24.04, as a native Linux checkout eliminates CRLF and Windows path/keyring divergences. Marketplace resided in separate worktree `feat/a8-live-gonka-acceptance`; Gonka runtime was compiled from an isolated LF checkout of exact SHA. Production source was not patched: only Gonka changes pertained to Testermint test harness and accelerated genesis parameters.

- Docker Desktop `29.5.3`, Linux `amd64`, 32 CPUs, ~32 GB RAM.
- Go runtime `1.24.2`; Cosmos SDK `v0.53.3-ps19-observability`.
- `wasmd v0.54.2`; `wasmvm v2.2.4`.
- Chain ID `gonka-mainnet`; transfer restriction in test genesis: `is_active=false`, `restriction_end_block=0`.
- Epoch length `25` blocks; work/reward vesting `2` epochs each.
- Runtime image: `sha256:c37fdd682e777331e203804078c191c69d17836eb85174c7cae869ff34bec5bc`.
- API image: `sha256:00e6fe9a986f52c6e7699106426e46393a06e2e87c5e10795bd683fc232943a4`.
- Proxy image: `sha256:8c97260a7ac42437057b3fca35193e9f22f6a791928e9dd4c24cd01b1938bc8e`.
- Edge image: `sha256:663eeb9b8282ded3d1f384847c4c6850ac2993ec005c09d7b1c9284932852779`.

Prior to destructive reboot, the harness inspects network and node containers, compose containers with labels `genesis/join1/join2/testdns`, known API/proxy/edge/Postgres/mock-server names, volumes by label/prefix, and `chain-public`. In addition to Docker guards, path `gonka/prod-local` is checked: any existing file, directory, or symlink triggers abort, because Testermint unconditionally purges this ignored path. Therefore, live runs are allowed strictly on fresh checkouts. The guard was verified on a checkout with pre-existing `prod-local`: execution halted before A9/evidence/Testermint, preserving the directory. The volume guard also flagged a test Postgres volume. Post evidence capture, only four compose projects were stopped, three Postgres volumes removed, and confirmed-empty `chain-public` deleted; unrelated containers and images were not cleared.

## 2. Scenario Matrix

| ID | Level | Scenario / Invariant | Status | Evidence / Reason |
|---|---|---|---|---|
| P0 | live network | 4 exact gRPC routes accessible from Wasm; broad `Query/Params` forbidden | PASS | Probe code 5, SHA, and denial in compact evidence |
| A1 | live network | Store/deploy Factory/Deal without admin, immutable config, exact artifact provenance | PASS | A9 manifest `a4e5e641…67439`, exact HEAD; Deal code 1, Factory code 2; on-chain `admin=""` |
| A2 | live network | Exact CW20 funding → Lock → native ClaimRewards → SettleClaim → 2 releases | PASS | Heights 134/160/161/187/212; final `Completed` |
| A3 | live network | Settlement from native summary + offer terms and release split via independent oracle | PASS | Run 012: expected=actual for GNK shares, state, and CW20 deltas; 100m = 98.5m + 1.5m + 0 |
| B1 | live network | No-sale claim/release: 100% GNK to Host, no CW20 transfer | PASS | run17 no-sale release + terminal repeat no-op |
| B2/late-donation-after-Completed | live network | Late liquid donation after `Completed` distributed per frozen Buyer/Host shares | **PASS** | `b2-late-005`: two real Bank donations of `1423ngonka`, DeliverTx release code 0; `1/1422`, then `2/1421` prove cumulative rounding, without modifying CW20/vesting/entitlements. [Evidence](evidence/a8-b2-late-donation-completed-005.json). |
| B2/vesting-timing | native | Original tranches + new vested gift after Completed | PASS | A/R2: pre_gift/fully_locked/first_unlocked/final, gift 10000000001; Buyer 1055134 + Host 9998944867. See a8-final-coverage-matrix.md. |
| B3/native-denom | live network | Extraneous native denom remains untouched | **PASS** | `b3-native-004`: Bank funding `0FA837…856B0` code 0 delivered `12345ua8b3foreign` to Deal; successful release `001423…65E4` code 0 paid out expected GNK and retained full foreign denom on Deal. [Evidence](evidence/a8-b3-foreign-native-004.json). Overall B3 across both asset types additionally relies on foreign-CW20 snapshot run17. |
| C1 | native + CT | Routing and epoch boundaries | PARTIAL strictly for exact-E native attribution | Missing/mismatch, E+4/E+5 Lock, and E+5 Refund PASS. Lower exact E boundary covered by CT; explicit native inclusion epoch bracket not found. See breakdown in a8-final-coverage-matrix.md. |
| C2 | native fault injection | Query error fail-closed + healthy recovery | PASS | C R3 receipts, including healthy Lock; a8-c-final-review-20260910.json. |
| D1-positive | live network | E+1 reject; E+2 exact unclaimed positive summary; claimed blocks | PASS | run043: exact `claimed=false` Host/E summary, E+1 code 5 `too_early` with full rollback, E+2 code 0 `claim_expiry`, 100% Buyer, 0 Host/fee, Deal CW20=0, Refunded; claimed=true rejection proven by earlier runs |
| D1-zero | live network, special genesis | Existing exact Host/E summary with `earned=0`, `rewarded=0`, `claimed=false`; E+1 reject; E+2 ClaimExpiry | PASS | run044: native-validated `initial_epoch_reward=0`; Host ACTIVE with PoC weight 10; exact zero/false summary; E+1 code 5 `too_early` and rollback; E+2 code 0 `claim_expiry`, 100% Buyer, 0 Host/fee, Deal CW20=0, Refunded, `host_only`. Does not prove production-config reachability |
| D2 | contract-only | Synthetic `claimed=true,total=0` defensive branch | PASS | Workspace Deal tests |
| E1 | live network | Missing summary before E+3 reject, E+3 NetworkUnconfirmed | PASS | `stable-e1-002`: exact native `NotFound/not found`; E+2=9 tx `500023…463A` code 5 + unchanged Locked/deposit; E+3=10 tx `6986C8…20C39` code 0, NetworkUnconfirmed, full Buyer refund, no separate Host/fee payout, Deal CW20=0, `host_only`. Buyer/Host address alias limits independent role-balance attribution |
| E2 | native fault injection / VM boundary | ADR-0013 error matrix | NATIVE PASS / VM NOT RUN | C R4 native variants E+2/E+3 proven. Typed InvalidResponse and R4.5 remain VM/FFI-boundary NOT RUN; R4.6 — contract-only. |
| E3 | native fault injection | Claimed, summary unavailable, refund/recovery | PASS | C R5 recover/cancel, real native ledger, full refund/HostOnly, and healthy settlement confirmed. |
| F1 | live/native Wasm | Direct Refund gas sweep near success boundary | PASS | run16/17: 14 attempts, OOG and sufficient-gas contract rejection |
| F2 | live/native Wasm | Limited-gas caller/submessage/reply does not alter outcome | PASS | run16/17 caller + submessage reply, no Deal/CW20/native transfer |
| G1 | native fault injection | CW20 failure rollback + retry | PASS | B/R6.1 send #1/#2/#3 + C/R6.3 emergency refund rollback; reviewed evidence preserved. |
| G2 | native fault injection | Bank failure rollback + retry | PASS | B/R7.1 both positions; C/R7.2 HostOnly rollback, retry h716, and terminal repeat h717. |
| G3 | live network | Real `ReleaseUnlockedGnk` after `Completed` at zero liquid/pending GNK | **Historical suite FAIL; transaction semantics satisfy no-payout invariant** | `g3-terminal-002` records receipt status `FAIL`. The included tx `238CDC…73C5` at height 214 returned DeliverTx code 5, codespace `wasm`, exact error `NothingToRelease`; state/counters/GNK/CW20 were unchanged, with no repeat payout or Deal Bank transfer. This is not a successful no-op or a passing JUnit result, and it is not a current-source E2E result. [Evidence](evidence/g3-terminal-release-repeat-002.json). This focused run does not extend earlier Factory/index/multi-Deal evidence. |

The G3 criterion was re-evaluated post-run: correct terminal behavior in the
absence of available GNK is an included DeliverTx rejected by the Deal strictly with
`NothingToRelease`, with zero mutations to state or balances. The recorded transaction
satisfies that semantic no-payout criterion, but the raw receipt and JUnit suite status
remain `FAIL`; this semantic review does not convert them into a passing test run. The
current test oracle requires DeliverTx, codespace `wasm`, non-zero code, tx hash,
positive inclusion height, and exact contract error text; arbitrary errors are rejected.
No new network run was executed, so the corrected oracle still needs a fresh run against
the selected source before a current-run PASS can be claimed.

## 3. Verified Funded Lifecycle

| Artifact | Code ID | SHA-256 |
|---|---:|---|
| Deal | 1 | `2caa08099be647a9c3d5fa9868ec6faee30ae7c2a9136cc4891cd75ccd55c0e5` |
| Factory | 2 | `8366a874619c03e54ed36edf6b0ebbde4001afcf6c1b02ed40f9762350e6d82b` |
| test CW20 | 3 | `50a3d9d47245235f2c99a4bba6c98a5d3fead5c4fadb4c809ffd206879203226` |
| test caller | 4 | `af6ddcc305a3292e6e99cfafadc38ea0f0def7892f4f9fc37a0a5c3f55baa147` |
| P0 probe | 5 | `7eacebc656412fe59d290ecac1a0608686372c39ae6d63a8cb7fcba23417a91d` |

- budget `100,000,000` micro-USDT, price `1,000,000`, fee `150 bps`;
- native claim `94,819,671,366,450 ngonka`;
- Buyer entitlement `100,000,000,000`, Host entitlement `94,719,671,366,450`;
- CW20: Host `98,500,000`, fee recipient `1,500,000`, Buyer refund `0`;
- settlement expected was computed without reading economic fields of Deal state:
  inputs were native summary (`epoch=5`, exact Host, `claimed=true`, `earned=0`,
  `rewarded=94,819,671,366,450`) and independently supplied offer terms. Expected
  was then separately compared against Deal state, release policy, three recipient deltas,
  and exact Deal outflow;
- each of two native releases: `47,409,835,683,225`;
  Buyer `50,000,000,000`, Host `47,359,835,683,225`;
- Deal bank balance after each release is zero. Independent cumulative oracle
  computes Buyer as `floor(total_released * numerator / denominator)`, Host receives
  remainder; expected and actual bank deltas plus both lifetime counters matched for
  each tranche;
- late donation `1,000,000`: Buyer `1,054`, Host `998,946`, remainder `0`, status
  remains `Completed`, release policy does not change.

Key tx hashes:

- offer `D677512E…D50E`, recipient `F7485815…AB69`, funding `56FDF792…149C`;
- lock `9D3E9708…8646` (height 134);
- native claim `63C0C1B4…238D` (height 160, gas used 129195);
- settle `C23ADC16…3239` (height 161, gas used 281978);
- releases `7150FF0B…3702` (height 187) and `DF13062A…9652` (height 212);
- late donation `1E97B56C…F154`, release `D47B8A9C…375F` (height 214).

Full hashes and sums are located in compact JSON, ensuring abbreviations above
are not the sole evidence record.

### Security Decision Record: Destructive State and Settlement Oracle

- Our invariant: Acceptance does not delete unknown local data; expected settlement
  cannot be derived from state of the Deal under test; fees, payouts, refunds, and
  GNK shares must follow exact offer terms and authoritative native summary.
- Pinned Gonka constraint: Testermint unconditionally wipes all `prod-local` before
  genesis; native summary specifies exact epoch, participant, claimed, and Work/Reward,
  while protobuf JSON may omit zero `earned_coins`.
- External pattern: Astroport/Oak are referenced strictly as cross-checks for fixed
  recipients, cumulative accounting, rounding, and zero/event handling; their code is
  not a source of Gonka requirements and was not copied.
- Relevant audit finding: OroSwap HAL-08 highlights stale pending state, but does not
  cover our Testermint path or settlement math; primary audit includes the specified
  Factory commit, while Marketplace is not covered by that audit.
- Decision: Fail-closed guard via `lexists(prod-local)` prior to Docker/Testermint;
  independent oracle from native summary + independently supplied Deal terms;
  semantic comparison of release policy, state, recipient deltas, and Deal outflow.
- Evidence: Unit regression rejects `fee=0` with payout of full budget to Host,
  invalid GNK shares, epoch/Host/claimed mismatches, and existing `prod-local`;
  live run `a8-funded-012` produced exact expected=actual.

## 4. P0 Wasm gRPC Allowlist

Test-only probe was stored/instantiated without admin on the same network. From the
real Wasm VM, `GetCurrentEpoch`, `ListClaimRecipients`, `EpochPerformanceSummaryByParticipant`,
and `TotalVestingAmount` were successfully decoded. Summary for epoch 5 returned
`claimed=true` and reward `94,819,671,366,450`; vesting after two releases returned
correct empty remainder.

Adversarial raw query `/inference.inference.Query/Params` terminated non-zero and
explicitly named the forbidden path. This proves the exact allowlist boundary, rather
than the presence of a generic gRPC bypass.

## 5. Contract-Only Regression Layer

Following changes, the following passed:

- `cargo fmt --all -- --check`;
- `cargo test --workspace --all-targets`: common 20, Deal 51, Factory 9,
  Factory integration 41, protobuf golden 4;
- `python -m unittest discover -s scripts/tests -p 'test_*.py'`: 39 tests on
  Windows (38 pass, one POSIX-only skip); A8 harness 17 pass plus one
  Windows-only skip under WSL/Linux;
- `git diff --check`.

This layer covers no-sale, routing/refund/expiry, negative transitions, rounding,
contamination, multi-Deal, and rollback. Additionally, regression tests explicitly
reject conserving-yet-incorrect 80/20 payouts ("all to Host"), stale A9 manifests,
incorrect runtime versions, and lingering Postgres resources. This layer is not
re-labeled as native evidence in the table above.

## 6. Security Advisory Gate

Running binary utilizes `github.com/CosmWasm/wasmd v0.54.2`. The official
[CWA-2025-007](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2025-007.md)
flags `wasmd <= v0.54.2` as vulnerable to unbounded reply recursion / stack overflow
and designates `v0.54.3` as the patched branch. The advisory mandates a coordinated
consensus-breaking upgrade.

Consequence: Successful Marketplace lifecycle does not permit public deployment.
The release gate can only be opened following an upgrade of the Gonka runtime or a
verifiable backport patch with repeated binary provenance/advisory review and
native regression testing.

## 7. Reproduction

Historical reproduction command from the former overlay-era workflow follows.
It is retained to explain the recorded evidence, **not** as an executable
procedure for this PR: `gonka-overlay/` was removed and live execution now uses
the immutable-source Docker runner in
[`ops/e2e/README.md`](../../ops/e2e/README.md).

From the historical Marketplace worktree with Docker Desktop and WSL available:

```powershell
git clone https://github.com/gonka-ai/gonka.git ..\gonka-a8-clean
git -C ..\gonka-a8-clean checkout test/a8-marketplace-harness
python scripts/a8_acceptance.py run-live `
  --gonka-dir ..\gonka-a8-clean `
  --run-id a8-funded-local --timeout-minutes 60
```

`--manifest` must point to the A9 manifest matching exact current Marketplace HEAD;
Deal/Factory hashes are rechecked before Testermint. If omitted, harness itself executes
two A9 optimizer builds in the evidence directory. Test-only CW20/caller are always
rebuilt in a run-specific target. The running binary must report exact Gonka SHA,
`wasmd v0.54.2`, and `wasmvm v2.2.4`, or the run fails closed.
Checkout must contain no `prod-local`: Git-clean is insufficient because this path is in
`.gitignore`. After one Testermint run, that checkout cannot be reused; its data must be
explicitly saved/purged outside the harness or a new clone created. Testermint then spins
up the network, invokes Python phases, and saves evidence. Sequence verified by run
`a8-funded-010`, and new settlement oracle by run `a8-funded-012`; both executed from
fresh clones of published branch with recorded harness SHA `9f32e027…`.

For P0 before teardown:

```bash
python3 scripts/a8_acceptance.py p0-probe   --context artifacts/a8-evidence/a8-funded-009/live-context.json   --wasm ../a8-gonka-worktree/inference-chain/contracts/p0-probe/artifacts/p0_probe.wasm
```

Current requirement: treat A2/P0 integration as proven, but do not release to production.
Next logical milestone: fix CWA-2025-007 in Gonka and add dedicated native fault/gas
suite for E/F/G, followed by a clean rerun.

## Completeness Check and Runner Finalization

All 73 native cases of C are confirmed; A/R1/R2 and B/R6.1/R7.1 are accepted via
independent runs. This closes the prepared native packages A/B/C, rather than all
possible VM/FFI errors, and is not an automated approval for production release.
VM-only R4.4 InvalidResponse and R4.5 have no live proof; R4.6 has contract-test
coverage. Historical PARTIAL B2/C1 requires reconciling their legacy subcases with
R1/R2; they cannot be treated as newly discovered defects.

Runner now finalizes C results immediately following bank-retry. Outer launcher
timeout is 75 minutes, inner JUnit C remains 65: headroom for Gradle/JUnit.
Offline 128 tests: 127 PASS, 1 platform skip. No new live run occurred; absence of
JUnit XML from old run is not concealed. Finalization verification does not inflate
native evidence.
