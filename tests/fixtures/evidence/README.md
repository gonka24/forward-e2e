# Synthetic Evidence Fixtures

Every file in `tests/fixtures/evidence/` is a deterministic, producer-shaped
**synthetic test fixture** for the offline runner and harness unit suites
(`tests/unit/runner/` and `tests/unit/harness/`). None of them is a recorded
chain, Docker or Testermint run, and none may be presented as one. Two kinds
of document live here, and the difference matters:

| Kind | Files | Where the bytes come from |
|---|---|---|
| **Identity-scrubbed derivations of former development receipts** | `lock-exact-epoch-legacy-context.json`, `claim-settlement-fault-phase.json`, `settled-deal-late-refund-and-vested-gift.json`, `terminal-release-repeat.json` | A development receipt was passed once through `renumber_identities()` in [`tests/unit/runner/support/synthetic_evidence.py`](../../unit/runner/support/synthetic_evidence.py), which replaced **every** chain identity (Bech32 address, transaction and block hash, code checksum, node id, validator public key, timestamp, run id, host path, run label) with a labelled seed derivation. The recorded **arithmetic** -- amounts, heights, epochs, gas, balances, runtime versions, event attributes -- was deliberately kept so the verifier's cross-field rules still meet producer-shaped numbers. The files have been hand-maintained since. |
| **Code-generated** | `wasm-query-allowlist-abi.json`, `go-query-error-classification/` (`report.json`, `raw/go-test.json`, `raw/exit-code`, `build.log`) | Produced by `boundary_fixture_files()` in the same module from constants. No value in them was ever recorded. |

Each document states which kind it is in `synthetic_fixture.notice`
(`SCRUBBED_NOTICE` or `GENERATED_NOTICE` in the generator module, pinned word
for word by the identity test), and the scrubbed ones carry a
`synthetic_fixture.provenance` block naming the method, what was replaced,
what was retained and what regression value remains (`live_receipt: false`).

## Contract

1. **Test-only marker.** Every JSON document carries `"test_fixture_only": true`.
   `verify_live_context`, `verify_go_boundary_report` and
   `verify_wasm_abi_report` (`forward_e2e/suite/verifier.py`) all refuse a
   marked document before reading anything else, so a fixture copied into a
   run directory can never be graded as that run's evidence. Unit tests strip
   the marker **in memory only** when exercising the verifiers' inner rules
   (`real_lock_exact_e_context()`, `go_boundary_evidence()` in
   `real_fixtures.py`; `_staged_report()` in `support/fixture_artifacts.py`),
   and `test_verifier.py` presents the committed Go and Wasm files verbatim to
   prove the refusal. The one non-document file, the `go test -json` line
   stream `go-query-error-classification/raw/go-test.json`, carries no marker
   of its own; it is bound to the marked `report.json` by `test_output_sha256`.
2. **Producer shape and truthful attribution.** Field names, nesting, types
   and cross-document hashes follow the real producers
   (`scripts/acceptance_harness.py`, `scripts/run_go_boundary.py`,
   `scripts/test_wasm_query_boundary.mjs`). Each document's
   `synthetic_fixture` block (`schema: forward-e2e.synthetic-fixture/2`) names
   its producer, the **producer functions** that write the shape -- every name
   must be a top-level definition of that producer; the identity test resolves
   them -- and a `producer_revision` that says exactly which code defines the
   shape:
   * the four harness documents: `8079c0c274f5252800ee16b9f4fb0148bfcae2fe`,
     where the harness was still `scripts/a8_acceptance.py`; the relocation to
     `scripts/acceptance_harness.py` changed environment names, scenario
     aliases and paths, not the phase shapes;
   * the Wasm report: the same commit, because
     `scripts/test_wasm_query_boundary.mjs` is byte-identical there and on
     this branch;
   * the Go report: the **post-relocation** `scripts/run_go_boundary.py` on
     `refactor/repository-layout` and explicitly *not* that commit, whose
     predecessor `scripts/run_a8_go_boundary.py` wrote a different shape
     (module `a8-go-boundary`, directory `/app/a8-go-boundary`, package
     `./a8faults`). The fixture reproduces only the current shape
     (`github.com/gonka24/forward-e2e-go-boundary`, `/app/harness/go_boundary`,
     `./query_faults`) and says so in `producer_shape_note`.
3. **Derived identities.** Every chain identity is a derivation of a labelled
   SHA-256 seed defined once in the generator module and repeated inside each
   document under `synthetic_fixture.identity_rules` (pinned equal to
   `IDENTITY_RULES`). The check, `identity_findings()`, requires:
   * every **checksum-valid Bech32 string of any human-readable part**
     (`gonka1`, `cosmos1`, `wasm1`, `gonkavaloper1`, ...) to be listed in
     `synthetic_fixture.identities.addresses` and to equal its role
     derivation;
   * every **32-byte base64 value** (the shape of a Tendermint Ed25519
     validator key) to be listed in `identities.pubkeys` and to equal its
     derivation;
   * every 64-hex and 40-hex value to be the *k*-th derivation of its kind in
     document order, or one of the four product revision pins
     (`REVISION_CONSTANTS`), or a **bound digest that the test itself
     recomputed** from the bytes it claims to bind (the committed
     `raw/go-test.json`, the placeholder Wasm module). A digest merely listed
     in `synthetic_fixture.bound_digests` is reported, not exempted: the
     document's say-so cannot launder a recorded SHA-256;
   * every RFC 3339 timestamp to be a **whole minute within the first day of
     the synthetic clock origin** (`2026-01-01T00:00Z`) after its zone offset
     is applied, so `2025-12-31T19:00:00-05:00` is the origin and a wall-clock
     time cannot hide behind a `-05:00` spelling or a missing designator;
   * no substring that would betray a recorded run: the host and runner path
     prefixes `/home/`, `/root/`, `/Users/`, `/private/`, `/tmp/`,
     `/var/folders/`, `/workspace/`, `/out/`, the old run folders and the
     dated run labels (`FORBIDDEN_SUBSTRINGS`). Two paths are allowed by
     design because the producers write them: the Go report's
     `harness_module.dir = /app/harness/go_boundary` (a container path in the
     producer's shape) and the `/synthetic/evidence/...` store paths of the
     lock-exact document (the scrubbed replacement of the recorded local
     paths). The generated `build.log` mentions
     `/root/.cache/go-build`; that is the BuildKit cache-mount *target inside
     the build container* which `scripts/run_go_boundary.py`'s Dockerfile
     declares, not a host directory, and `build.log` is not a JSON document
     and is not scanned -- it is regenerated from code like the rest.
4. **Internal consistency.** Amounts, heights, epochs, balances, counters and
   event sets are mutually consistent, so the real verifier rules (settlement
   arithmetic, release counters, vesting totals, epoch brackets) pass on the
   positive document.
5. **Single-fact negatives.** Tests derive negative cases by deleting or
   changing **exactly one mandatory fact** of a positive fixture, never by
   inventing a simplified shape.
6. **Never live proof.** A passing unit test against these fixtures verifies
   the runner's grading logic; it is never evidence of a live chain or Docker
   execution. The scrubbed documents regress the *arithmetic shape* of
   recorded producer output; they regress nothing about recorded identities
   and are not receipts.

## How the contract is enforced

[`tests/unit/runner/test_synthetic_evidence.py`](../../unit/runner/test_synthetic_evidence.py)
re-derives the contract from the committed bytes on every run:

| Check | What it proves |
|---|---|
| `identity_findings(document, recomputed_digests=...) == []` for every JSON document | No Bech32 address of any prefix, 32-byte key, 64-hex or 40-hex value, timestamp or run id that is not its derivation; the only exempt digests are the ones the test recomputed; no runner path or review-folder substring. |
| Notice, `producer_revision`, `identity_rules` and (for scrubbed documents) `provenance` pinned exactly | A document cannot drift into claiming it is generated when it is scrubbed, or attribute a shape to a commit whose producer wrote a different one. |
| Every `producer_functions` entry resolves to a top-level definition of the named producer | Attribution names real code, not remembered names. |
| Address map and public-key map cover every address and key used | The cast list is complete, not a sample. |
| Canonical encoding (`json.dumps(indent=2, sort_keys=True) + "\n"`) | The on-disk key order is the order the identity numbering uses. |
| Boundary files byte-identical to `boundary_fixture_files()` | `wasm-query-allowlist-abi.json` and `go-query-error-classification/*` are generated, not edited. |
| Committed Go and Wasm files presented verbatim are refused; with only the marker removed they pass `verify_go_boundary_report` / `verify_wasm_abi_report` | The marker is what protects the fixtures, and the generator produces what the real verifier requires. |
| In-memory contexts of `real_fixtures.py` (including `go_boundary_evidence()`) | Only role derivations (plus the B3 genesis fixture account), registered transaction hashes, validator keys from the committed documents, synthetic-clock timestamps and `synthetic-` run ids. |
| No file under `tests/` mentions the removed review-evidence path | The suites depend on no development artefact. |

The mutation tests in the same module change exactly one fact of a passing
document and expect exactly one finding: one address from another document,
one `cosmos1`/`wasm1`/`gonkavaloper1` address, one hash out of sequence, one
timestamp a year off, one wall-clock timestamp spelled with a `-05:00` offset
(with the origin spelled the same way as the passing control), one foreign
validator key, one key declared under `identities.pubkeys` that is not its
derivation, one pasted SHA-256 declared as bound, one `/Users/...` path and
every other forbidden prefix.

[`tests/unit/runner/test_real_fixtures.py`](../../unit/runner/test_real_fixtures.py)
checks the in-memory D1/E1 contexts against their own declarations (target
epoch, budget, accounts, brackets) rather than against the module's literal
heights, so that a negative test failing one relation fails for that relation.

## Regenerating

The two boundary fixtures are produced by code:

```bash
python3 -B tests/unit/runner/support/synthetic_evidence.py
```

The four scrubbed documents are maintained by hand; after any edit run the
identity test. If a new identity is added, give it a role in
`synthetic_fixture.identities.addresses` (addresses) or a label in
`identities.pubkeys` (validator keys), or let it take the next ordinal in
document order (hashes). Never paste an identity from a run, however the
file is labelled: the check will report it, and the review history is the
reason the check exists.

## Fixture map

| Fixture | Producer functions (all top-level definitions of the producer) | Consumers | Verified property |
|---|---|---|---|
| `lock-exact-epoch-legacy-context.json` | `scripts/acceptance_harness.py`: `bootstrap`, `create_deal`, `lock_exact_e_scenario` (via `lock_e_plus_4_scenario(offset=0)`), `run_live`, `write_object` | `real_lock_exact_e_context()`; `test_e2e_evidence_model.py` (`LEGACY_FIXTURE_SOURCE` pin); `test_verifier.py` | Legacy pre-model `source` block is rejected as a downgrade (`gonka_test_harness_sha` vs `runtime.gonka_source_sha`); adapted in memory to `a8.evidence/e2e-immutable-source/2` it proves the `lock-exact-e` checkpoints (`Funded->Locked`, `recipient_locked=true`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e`, `target_epoch_e_reached`). |
| `claim-settlement-fault-phase.json` | `scripts/acceptance_harness.py`: `claim_settle`, `cw20_settlement_fault_targets`, `configure_cw20_transfer_failure`, `scenario_financial_snapshot`, `assert_injected_cw20_failure`, `assert_terminal_settlement_repeat`, `append_phase` | `real_claim_settle_phase()`, `real_cw20_fault_rollbacks()`; `test_verifier.py`; harness withdrawal tests | Pull-payment withdrawal fault rollbacks (`cw20_fault_rollbacks`), pending-obligation preservation (`settlement_payments`, the Deal's `usdt_payments` query as `withdraw_pending_usdt` / `verify_recorded_settlement` read it), per-role `withdrawal_txs`, rejected `settle_repeat`. |
| `settled-deal-late-refund-and-vested-gift.json` | `scripts/acceptance_harness.py`: `refund_e_plus_5_rejected_scenario`, `close_gnk_release`, `r2_gift_checkpoint`, `r2_gift_snapshot`, `verify_vesting_addition_scenario`, `reconcile_vesting_addition` | `real_r1_*`, `real_r2_*`, `real_vesting_addition_phase()`, funded-claim helpers; `test_verifier.py`; `tests/unit/harness/test_acceptance_harness.py` (`refund_e_plus_5_phase`) | `Refund` rejected at `E+5` after settlement; four-stage `r2_gift_checkpoint` progression; `vesting_addition` schedule accounting; proportional gift `release` payouts proven by their own receipts. |
| `terminal-release-repeat.json` | `scripts/acceptance_harness.py`: `terminal_release_repeat`, `terminal_release_snapshot`, `assert_terminal_release_preconditions`, `assert_terminal_nothing_to_release` | `real_terminal_release_repeat_phase()`; `test_verifier.py`; `test_real_fixtures.py` | Terminal `Completed` `ReleaseUnlockedGnk` repeat is included and rejected with `NothingToRelease` (`code=5`, `codespace="wasm"`); zero liquid/vesting preconditions; unchanged Deal/Bank/CW20 snapshots. |
| `wasm-query-allowlist-abi.json` | `scripts/test_wasm_query_boundary.mjs`: `cases`, `evidence` | `support/fixture_artifacts.py`; `test_verifier.py`; `test_live_run_regressions.py` | 9-case `query_chain` envelope decoding report; `wasm_sha256` binding checked by `verify_wasm_abi_report`. |
| `go-query-error-classification/` (`report.json`, `raw/go-test.json`, `build.log`, `raw/exit-code`) | `scripts/run_go_boundary.py`: `generate_dockerfile`, `main` (post-relocation shape only, see Contract 2) | `go_boundary_evidence()`, `support/fixture_artifacts.py`; `test_verifier.py` | `verify_go_boundary_report`: exit codes, `test_output_sha256` binding to `raw/go-test.json`, pass events for `GO_BOUNDARY_REQUIRED_TESTS`. |
| In-memory contexts in `tests/unit/runner/real_fixtures.py` (`real_claim_expiry_*`, `real_network_unconfirmed_context`, `real_b3_context`, `synthetic_late_completed_donation_phase`, ...) | `scripts/acceptance_harness.py`: `refund_scenario`, `verify_unclaimed_scenario`, `verify_missing_summary_scenario`, `b3_foreign_native_release` (phase `b3_foreign_native_successful_release`), `late_donation` | `test_verifier.py`, `test_real_fixtures.py` | Claim-expiry (`E+1` rejection, `E+2` refund), network-unconfirmed (`E+2` rejection, `E+3` refund; Buyer and Host coincide by the catalog's stated limitation), B3 foreign-native preservation and late-donation rounding checkpoints; `test_fixture_only` live-ingestion guard. |

## What was lost on purpose

The previous fixtures were recorded development receipts. The four scrubbed
documents keep their arithmetic and nothing else, so the suites no longer
regress against *recorded* producer output in any way that could identify a
run; they regress against producer-shaped numbers whose identities are all
derivations. A change in the live producer's field names is therefore caught
only when the harness tests are updated alongside it (`docs/evidence.md`,
producer/consumer matrix), not by these files drifting out of date -- and a
change in `scripts/run_go_boundary.py`'s report shape must be followed by
regenerating the Go fixture and updating `GO_PRODUCER_REVISION`, because the
fixture is attributed to that script's current shape, not to a commit.
