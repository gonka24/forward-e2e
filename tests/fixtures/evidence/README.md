# Synthetic Evidence Fixtures

Every file in `tests/fixtures/evidence/` is a deterministic, producer-shaped
**synthetic test fixture** for the offline runner and harness unit suites
(`tests/unit/runner/` and `tests/unit/harness/`). None of them is, or is
derived from, a recorded chain, Docker or Testermint run.

## Contract

1. **Test-only marker.** Every JSON document carries `"test_fixture_only": true`,
   so `verify_live_context` (`forward_e2e/suite/verifier.py`) unconditionally
   rejects the file if it is ever presented directly as a live run artefact.
   Unit tests strip the marker in memory only when exercising the verifier's
   inner rules. The one non-document file, the `go test -json` line stream
   `go-query-error-classification/raw/go-test.json`, carries no marker of its
   own; it is bound to the marked `report.json` by `test_output_sha256`.
2. **Producer shape.** Field names, nesting, types and cross-document hashes
   follow the real producers (`scripts/acceptance_harness.py`,
   `scripts/run_go_boundary.py`, `scripts/test_wasm_query_boundary.mjs`). Each
   document names its producer, producer functions and the revision whose
   producer code defines the shape in its `synthetic_fixture` block
   (`schema: forward-e2e.synthetic-fixture/2`).
3. **Derived identities.** Every chain identity -- Bech32 address, transaction
   or block hash, code checksum, node id, validator key, timestamp, run id --
   is a derivation of a labelled SHA-256 seed defined once in
   [`tests/unit/runner/support/synthetic_evidence.py`](../../unit/runner/support/synthetic_evidence.py).
   The rules are repeated inside each document under
   `synthetic_fixture.identity_rules`, the address cast list under
   `synthetic_fixture.identities.addresses`. The only literals allowed to
   appear verbatim are the four product revision pins
   (`REVISION_CONSTANTS`) and the digests a document declares in
   `synthetic_fixture.bound_digests` because they bind other fixture bytes.
4. **Internal consistency.** Amounts, heights, epochs, balances, counters and
   event sets are mutually consistent, so the real verifier rules (settlement
   arithmetic, release counters, vesting totals, epoch brackets) pass on the
   positive document.
5. **Single-fact negatives.** Tests derive negative cases by deleting or
   changing **exactly one mandatory fact** of a positive fixture, never by
   inventing a simplified shape.
6. **Never live proof.** A passing unit test against these fixtures verifies
   the runner's grading logic; it is never evidence of a live chain or Docker
   execution.

## How the contract is enforced

[`tests/unit/runner/test_synthetic_evidence.py`](../../unit/runner/test_synthetic_evidence.py)
re-derives the contract from the committed bytes on every run:

| Check | What it proves |
|---|---|
| `identity_findings(document) == []` for every JSON document | No address, 64-hex or 40-hex value, timestamp or run id that is not its derivation; no runner path or review-folder substring. |
| Address map covers every address used | The cast list is complete, not a sample. |
| Canonical encoding (`json.dumps(indent=2, sort_keys=True) + "\n"`) | The on-disk key order is the order the identity numbering uses. |
| Boundary files byte-identical to `boundary_fixture_files()` | `wasm-query-allowlist-abi.json` and `go-query-error-classification/*` are generated, not edited. |
| `verify_go_boundary_report` / `verify_wasm_abi_report` accept the generated files | The generator produces what the real verifier requires. |
| In-memory contexts of `real_fixtures.py` | Only role derivations (plus the B3 genesis fixture account), registered transaction hashes, synthetic-clock timestamps and `synthetic-` run ids. |
| No file under `tests/` mentions the removed review-evidence path | The suites depend on no development artefact. |

The mutation tests in the same module show that one foreign address, one
foreign hash, one wall-clock timestamp or one runner path is enough to fail
the check.

## Regenerating

The two boundary fixtures are produced by code:

```bash
python3 -B tests/unit/runner/support/synthetic_evidence.py
```

The four live-context documents were synthesised once from the producer
shapes and are maintained by hand; after any edit run the identity test. If a
new identity is added, give it a role in `synthetic_fixture.identities.addresses`
(addresses) or let it take the next ordinal in document order (hashes).

## Fixture map

| Fixture | Producer functions | Consumers | Verified property |
|---|---|---|---|
| `lock-exact-epoch-legacy-context.json` | `run_live`, `lock_exact_e`, `prepare_runtime_snapshot`, `write_live_context` | `real_lock_exact_e_context()`; `test_e2e_evidence_model.py` (`LEGACY_FIXTURE_SOURCE` pin); `test_verifier.py` | Legacy pre-model `source` block is rejected as a downgrade (`gonka_test_harness_sha` vs `runtime.gonka_source_sha`); adapted in memory to `a8.evidence/e2e-immutable-source/2` it proves the `lock-exact-e` checkpoints (`Funded->Locked`, `recipient_locked=true`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e`, `target_epoch_e_reached`). |
| `claim-settlement-fault-phase.json` | `claim_settle`, `query_usdt_payments`, `withdraw_usdt_with_fault` | `real_claim_settle_phase()`, `real_cw20_fault_rollbacks()`; `test_verifier.py`; harness withdrawal tests | Pull-payment withdrawal fault rollbacks (`cw20_fault_rollbacks`), pending-obligation preservation (`settlement_payments`), per-role `withdrawal_txs`, rejected `settle_repeat`. |
| `settled-deal-late-refund-and-vested-gift.json` | `r1_1_refund_e_plus_5_rejected`, `release`, `r2_gift_checkpoint`, `vesting_addition` | `real_r1_*`, `real_r2_*`, `real_vesting_addition_phase()`, funded-claim helpers; `test_verifier.py`; `tests/unit/harness/test_acceptance_harness.py` (`refund_e_plus_5_phase`) | `Refund` rejected at `E+5` after settlement; four-stage `r2_gift_checkpoint` progression; `vesting_addition` schedule accounting; proportional gift `release` payouts proven by their own receipts. |
| `terminal-release-repeat.json` | `terminal_release_repeat`, `terminal_release_snapshot` | `real_terminal_release_repeat_phase()`; `test_verifier.py`; `test_real_fixtures.py` | Terminal `Completed` `ReleaseUnlockedGnk` repeat is included and rejected with `NothingToRelease` (`code=5`, `codespace="wasm"`); zero liquid/vesting preconditions; unchanged Deal/Bank/CW20 snapshots. |
| `wasm-query-allowlist-abi.json` | `scripts/test_wasm_query_boundary.mjs` (`cases`, `evidence`) | `support/fixture_artifacts.py`; `test_verifier.py`; `test_live_run_regressions.py` | 9-case `query_chain` envelope decoding report; `wasm_sha256` binding checked by `verify_wasm_abi_report`. |
| `go-query-error-classification/` (`report.json`, `raw/go-test.json`, `build.log`, `raw/exit-code`) | `scripts/run_go_boundary.py` (`generate_dockerfile`, `main`) | `go_boundary_evidence()`, `support/fixture_artifacts.py`; `test_verifier.py` | `verify_go_boundary_report`: exit codes, `test_output_sha256` binding to `raw/go-test.json`, pass events for `GO_BOUNDARY_REQUIRED_TESTS`. |
| In-memory contexts in `tests/unit/runner/real_fixtures.py` (`real_claim_expiry_*`, `real_network_unconfirmed_context`, `real_b3_context`, `synthetic_late_completed_donation_phase`, ...) | `refund_scenario`, `verify_unclaimed_scenario`, `verify_missing_summary_scenario`, `b3_foreign_native_successful_release`, `late_donation` | `test_verifier.py`, `test_real_fixtures.py` | Claim-expiry (`E+1` rejection, `E+2` refund), network-unconfirmed (`E+2` rejection, `E+3` refund), B3 foreign-native preservation and late-donation rounding checkpoints; `test_fixture_only` live-ingestion guard. |

## What was lost on purpose

The previous fixtures were recorded development receipts. With their removal
the suites no longer regress against *recorded* producer output; they regress
against producer-shaped synthetic output whose arithmetic was carried over
unchanged and whose identities were replaced. A change in the live producer's
field names is therefore caught only when the harness tests are updated
alongside it (`docs/evidence.md`, producer/consumer matrix), not by these
files drifting out of date.
