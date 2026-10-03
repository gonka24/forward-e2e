# Development Guide

Guide for developing, testing, and maintaining `gonka24/forward-e2e`. Read
[`AGENTS.md`](../AGENTS.md) alongside this document.

---

## 1. Codebase Navigation

| Directory / Module | Purpose |
|---|---|
| [`forward_e2e/suite/`](../forward_e2e/suite/) | Lower-level suite runner: `catalog.py`, `orchestrator.py`, `adapters.py`, `runtime.py`, `collector.py`, `verifier.py`, `reporter.py`, `models.py`, `evidence_model.py`, `source_snapshot.py`, `supervisor.py`, `lock.py`. |
| [`forward_e2e/execution/`](../forward_e2e/execution/) | Upper-level E2E lifecycle: `cli.py`, `planner.py`, `runlock.py`, `sources.py`, `compat.py`, `builder.py`, `executor.py`, `deployment.py`, `evidence.py`, `outcome.py`, `runpackage.py`, `delivery.py`, `cancel.py`, `ownership.py`, `runner_image.py`, `runner_source.py`, `file_safety.py`, `gitio.py`, `context.py`, `errors.py`. |
| [`scripts/`](../scripts/) | Subprocess drivers executed inside the runner container (`acceptance_harness.py`, `external_harness.py`, `run_go_boundary.py`, `test_wasm_query_boundary.mjs`). |
| [`harness/`](../harness/) | Out-of-tree test code and Compose fragments (`testermint/`, `network/`, `go_boundary/`, `wasm_query_allowlist/`, `container_control.py`). |
| [`vendor/contract_release/`](../vendor/contract_release/) | Pinned copy of `release.py` from `forward-contracts`. |
| [`ops/runner/`](../ops/runner/) | Runner container `Dockerfile`, `compose.yaml`, `entrypoint.sh`, `.env.example`, and `RUNNER_VERSION`. |
| [`ops/e2e/`](../ops/e2e/) | Host CLI wrappers (`run-e2e.sh`, `Run-E2E.ps1`, `build-runner.sh`, `Build-Runner.ps1`). |
| [`tests/`](../tests/) | Offline unit suites (`tests/unit/runner`, `tests/unit/harness`), local Git integration suite (`tests/integration/local_sources`), and synthetic fixtures (`tests/fixtures/`). |

---

## 2. Running the Offline Test Suites

All three suites use Python's standard library `unittest` (no `pytest`, no
third-party packages) and must be run with `-B` to prevent writing `__pycache__`
into the repository:

```bash
# Suite and E2E runner unit tests
python3 -B -m unittest discover -s tests/unit/runner -p 'test_*.py' -v

# Harness and release helper unit tests
python3 -B -m unittest discover -s tests/unit/harness -p 'test_*.py' -v

# Local Git object acquisition integration tests
python3 -B -m unittest discover -s tests/integration/local_sources -p 'test_*.py' -v
```

### Writing Tests and Fixtures

1. **Module Docstring Contract:** Every test file docstring must end with:
   ```text
   All fixtures are synthetic. No network, Docker, or live chain calls.
   ```
   (Or, if using a loopback-only `127.0.0.1` HTTP server on an ephemeral port:
   `All fixtures are synthetic. No external network, Docker, or live chain calls; HTTP is loopback-only.`)
2. **Self-Contained Synthetic Fixtures (`tests/fixtures/evidence/`):**
   - Positive baseline fixtures under [`tests/fixtures/evidence/`](../tests/fixtures/evidence/README.md)
     are deterministic, producer-shaped synthetic documents. Each carries
     `"test_fixture_only": true` (rejected outright by `verify_live_context`,
     so a committed fixture can never pass as a live artefact) and a
     `synthetic_fixture` block naming the producer, its functions and the
     revision whose code defines the shape.
   - Every chain identity (Bech32 address, transaction/block hash, code
     checksum, timestamp, run id) is derived from the labelled seed in
     [`tests/unit/runner/support/synthetic_evidence.py`](../tests/unit/runner/support/synthetic_evidence.py):
     `synthetic_address(doc, role)`, `synthetic_tx_hash` / `fixture_tx_hash`,
     `synthetic_timestamp(n)` (minutes after `2026-01-01T00:00:00Z`),
     `synthetic_run_id(doc)`. Use these helpers for new values; never paste a
     real address, hash or wall-clock time.
   - The two boundary fixtures (`wasm-query-allowlist-abi.json`,
     `go-query-error-classification/`) are generated:
     `python3 -B tests/unit/runner/support/synthetic_evidence.py` rewrites them
     and they must stay byte-identical to its output.
   - [`tests/unit/runner/test_synthetic_evidence.py`](../tests/unit/runner/test_synthetic_evidence.py)
     re-derives the contract from the committed bytes on every run (identity
     derivations, complete address cast list, canonical encoding, generator
     identity, no dependency on removed development artefacts). Run it after
     any fixture edit.
   - Negative fixtures in unit tests are derived from a valid positive fixture
     (`copy.deepcopy`, then one change) by mutating or deleting **exactly one
     mandatory field**. Never construct simplified dummy dicts for negative
     tests, and take addresses from the positive document
     (`release["state_after"]["buyer"]`, `deal_config["host"]`) rather than
     from literals, so a negative test cannot pass by filtering on a value
     that no longer occurs.
3. **macOS note:** the collector opens destination directories component by
   component without following symlinks (`_open_anchored_directory` in
   `forward_e2e/suite/collector.py`), which is a deliberate evidence-safety
   check. On macOS the default temp roots (`/tmp`, `/var/folders/...`) are
   symlinks into `/private`, so roughly forty tests that write artefacts
   through the collector (`test_models`, `test_verifier`,
   `test_e2e_cli_layout`, `test_e2e_evidence_policy`, ...) fail spuriously
   with `NotADirectoryError: 'tmp'`. Export a `TMPDIR` whose path contains no
   symlink (for example a directory under your home) before running the
   suites; CI on Linux does not need this.

---

## 3. Files Hashed into `run.lock.json`

[`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py) hashes
the following runner-owned files into every `run.lock.json`:

- **`HARNESS_FILES` (`lock.runner.harness_hash`):**
  - `scripts/acceptance_harness.py`
  - `vendor/contract_release/release.py`
  - `scripts/run_go_boundary.py`
  - `scripts/test_wasm_query_boundary.mjs`
  - `scripts/external_harness.py`
  - `forward_e2e/suite/source_snapshot.py`
  - `harness/container_control.py`
- **`VERIFIER_FILES` (`lock.runner.verifier_hash`):**
  - `forward_e2e/suite/verifier.py`
  - `forward_e2e/suite/collector.py`
  - `forward_e2e/suite/reporter.py`
  - `forward_e2e/suite/models.py`
  - `forward_e2e/suite/catalog.py`
  - `forward_e2e/suite/evidence_model.py`
- **`NETWORK_FILES` (`lock.network.config_hashes`):**
  - `harness/network/ownership.yml`
  - `harness/network/nats.yml`
  - `harness/network/foreign-native-genesis.yml`
  - `harness/network/genesis/foreign-native-genesis-provision.sh`
- **`EXTERNAL_TEST_DIRS` (`lock.external_tests`):**
  - `harness/testermint`
  - `harness/network`
  - `harness/go_boundary`
  - `harness/wasm_query_allowlist`
- **`RUNNER_VERSION_FILE` (`lock.runner.runner_version`):**
  - `ops/runner/RUNNER_VERSION`

Editing any file above changes the runner's asset hashes and invalidates
previously generated `run.lock.json` plans. If you add or rename a file in these
lists, update `planner.py`, `RunnerLayout.assert_complete()`, and the layout unit
tests in `tests/unit/runner/`.

---

## 4. Updating `harness/wasm_query_allowlist`

Whenever any file in [`harness/wasm_query_allowlist/`](../harness/wasm_query_allowlist/)
(`Cargo.toml`, `Cargo.lock`, `Makefile`, `README.md`, `build.sh`, `src/*.rs`, or
`artifacts/p0_probe.wasm`) is modified, `artifacts/checksums.txt` must be
updated and verified:

```bash
./harness/wasm_query_allowlist/build.sh verify
```

If Rust source or dependencies in `harness/wasm_query_allowlist` change, rebuild
the Wasm binary and manifest using the pinned optimizer container:

```bash
make -C harness/wasm_query_allowlist build CONTAINER_ENGINE=docker
make -C harness/wasm_query_allowlist check CONTAINER_ENGINE=docker
```

---

## 5. Updating `vendor/contract_release/release.py`

See [`vendor/contract_release/README.md`](../vendor/contract_release/README.md)
for the procedure to sync `release.py` from `gonka24/forward-contracts`. Never
modify `EXTRACTION.json` when updating `release.py` in later commits.
