# Vendored Contract Release Helper (`vendor/contract_release`)

This directory holds a pinned, runner-owned copy of the canonical contract
release helper (`release.py`).

## Provenance

- **Canonical repository:** `https://github.com/gonka24/forward-contracts`
- **Extraction commit:** `d637eea5432506d60c90c1d8436c67b93802d829`
- **Source path at extraction:** `scripts/a9_release.py`
- **Pinned SHA-256 (`EXTRACTION.json`):**
  `d2495788af9f74c6ff9759ce06f65efdb8f1373b36d516e7632f5a4904114d97`

The exact file bytes of `vendor/contract_release/release.py` are preserved
without modification so they remain verifiable against
[`EXTRACTION.json`](../../EXTRACTION.json).

## Why it lives in the runner

1. **Target independence:** The runner builds and verifies the selected
   `forward-contracts` commit using its own pinned `release.py` (included in
   `HARNESS_FILES` in [`forward_e2e/execution/planner.py`](../../forward_e2e/execution/planner.py))
   rather than executing an unverified script from the target checkout.
2. **Import sharing:** [`scripts/acceptance_harness.py`](../../scripts/acceptance_harness.py)
   imports release manifest and deployment helpers from `vendor/contract_release`
   when deploying pre-built contract artifacts onto the live Testermint chain.

## Updating `release.py`

Canonical maintenance happens in `gonka24/forward-contracts`. When syncing a new
version into `forward-e2e`:

1. Copy the updated file from a committed revision of `forward-contracts` into
   `vendor/contract_release/release.py`.
2. Record the upstream commit and new SHA-256 in the commit message and PR
   description (never edit historical entries in `EXTRACTION.json`).
3. Run the harness unit tests (`python3 -B -m unittest discover -s tests/unit/harness -p 'test_*.py' -v`)
   and runner unit tests (`python3 -B -m unittest discover -s tests/unit/runner -p 'test_*.py' -v`).
4. Because `vendor/contract_release/release.py` is in `HARNESS_FILES`, updating
   it changes `lock.runner.harness_hash` and invalidates existing `run.lock.json`
   files; create new plans after rebuilding the runner image.
