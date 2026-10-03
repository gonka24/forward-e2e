# Forward E2E Acceptance Runner

Containerized acceptance runner, harness, boundary probes, and offline grading
framework for the Gonka Forward Marketplace.

This repository (`gonka24/forward-e2e`) executes full end-to-end validation of an
**explicitly chosen pair of immutable product commits**:

- **Gonka chain** (`gonka-ai/gonka`), selected by full 40-character Git SHA.
- **Forward Marketplace contracts** (`gonka24/forward-contracts`), selected by
  full 40-character Git SHA.

Both repositories are materialised from Git objects and verified before build,
after build, and after execution to ensure zero source modifications. All runner
logic, external Kotlin Testermint scenarios, Compose fragments, boundary probes,
and verifiers live inside the runner image.

---

## Repository Boundaries

| Repository | Responsibility |
|---|---|
| `gonka24/forward-e2e` (this repo) | E2E runner (`forward_e2e/`), live harness (`scripts/`), out-of-tree Testermint & boundary fixtures (`harness/`), runner container (`ops/`), and offline test suites (`tests/`). |
| `gonka24/forward-contracts` | Production CosmWasm contracts (`deal`, `factory`), contract unit/`cw-multi-test` suites, protobuf bindings, and canonical contract release helper. |
| `gonka-ai/gonka` | Upstream Gonka chain, `inferenced`, Decentralization API, and upstream Testermint framework. |

---

## Host Prerequisites

- **Docker Engine** with **Docker Compose V2** (`docker compose`) and **Buildx**
  (`docker buildx`).
- **Git** on the host when using `--gonka-path` or `--contracts-path` (not
  required when both sources are fetched via `--gonka-repo` and
  `--contracts-repo`).
- **Python 3.11+** on the host only when running the offline unit and local-source
  integration test suites directly on the host.

No Rust, Go, Java, Gradle, or Node.js installation is required on the host to
run the E2E suite; all target build and execution toolchains are pinned inside
the runner container (`ops/runner/Dockerfile`).

---

## Quickstart

### 1. Run offline unit and integration tests

```bash
python3 -B -m unittest discover -s tests/unit/runner -p 'test_*.py' -v
python3 -B -m unittest discover -s tests/unit/harness -p 'test_*.py' -v
python3 -B -m unittest discover -s tests/integration/local_sources -p 'test_*.py' -v
```

### 2. Build the runner image

Select the runner commit by full 40-character SHA:

```bash
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>
```

PowerShell:

```powershell
.\ops\e2e\Build-Runner.ps1 -RunnerSha <RUNNER_FULL_40_HEX_SHA>
```

### 3. List the scenario catalog

```bash
./ops/e2e/run-e2e.sh list
```

### 4. Plan and run an E2E suite

```bash
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_40_HEX_SHA> \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> \
  --profile smoke \
  --output ./out/e2e
```

### 5. Read the result

The wrapper mounts the `--output` directory (or `<repo>/out` when the flag is
absent) at `/out` inside the container, so the run package lands on the host at
`./out/e2e/<run-id>/` for the command above, or at `<repo>/out/runs/<run-id>/`
without `--output`. Inside it:

| File | Meaning |
|---|---|
| `run.lock.json` | The sealed plan: both full SHAs, the runner image id, the catalog and harness hashes. Written once, never rewritten. |
| `build-manifest.json`, `execution-manifest.json` | What this execution built and ran, bound to the lock by `lock_sha256`. |
| `result.json` = `e2e-run-result.json` | The whole-run verdict (`PASSED`, `FAILED`, `INCOMPLETE` or `CANCELLED`, schema `e2e/run-result/2`), produced only by `evaluate_run` in `forward_e2e/execution/outcome.py`. The wrapper exits with its `exit_code`: `0` only for `PASSED`. |
| `suite/<run-id>/summary.md`, `suite-result.json`, `coverage.json` | Per-task status, verifier findings and the acceptance line, which is always `acceptance_status: NOT_REVIEWED` — no automated path awards acceptance. |
| `suite/<run-id>/runs/<task-run-id>/…` | Raw per-task evidence (`live-context.json`, `source-immutability.json`, JUnit XML, boundary reports). |

Two offline commands work on that package and never start Docker:

```bash
# Re-derive the verdict and regenerate summary.md / coverage.json
./ops/e2e/run-e2e.sh report --run ./out/e2e/<run-id>

# A run that was interrupted before export: reconcile the runtime snapshots
# left in the persistent /workspace volume, export and grade them
./ops/e2e/run-e2e.sh recover --run <run-id> --output ./out/e2e-recovered
```

See [`docs/evidence.md`](docs/evidence.md) for the complete layout and
[`docs/operations.md`](docs/operations.md) for every flag.

### 6. What a result does and does not mean

- **A `PASSED` verdict is a statement about one SHA pair, in one run package,
  on one runner image.** The repository itself contains no run package, no
  recorded verdict and no historical receipt for any Gonka or contracts
  commit; the only committed evidence documents are the synthetic fixtures
  under `tests/fixtures/evidence/`, each marked `test_fixture_only: true` and
  refused by the verifier. If you need to know whether a commit pair passes,
  run it.
- **Packages from the retired overlay/prepared-commit runner are read-only
  history.** `report` classifies them `historical-prepared-build` from their
  documents (never from the runner version string), keeps their original
  `e2e-run-result.json` and writes its own re-derivation beside it as
  `e2e-run-result.historical-regrade.json`; they can never become proof about
  unmodified sources.
- **The runner proves what its catalog exercises and nothing more.**
  [`docs/coverage.md`](docs/coverage.md) §3 maps every lifecycle operation of
  the contracts onto the 24 tasks and names the gaps explicitly: `Cancel` and
  `ForwardExcessGnk` have no scenario, and Gonka's gRPC query allowlist is
  checked only by a manual probe that has no supported execution path, so a
  `PASSED` run says nothing about them. Boundary tasks (`GO_BOUNDARY`,
  `WASM_ABI`, `CONTRACT_TEST`) run against synthetic hosts and are never
  relabelled as live chain proof; the catalog's per-task `limitations` are
  carried unchanged into `suite-plan.json` and `coverage.json`.
- **Acceptance is a human decision.** Every report says
  `acceptance_status: NOT_REVIEWED`; release sign-off is a separate review of
  the evidence, not an output of this tool.

---

## Repository Layout

```text
forward_e2e/
  suite/                Suite catalog, orchestrator, runtime snapshots, collector, verifier, reporter
  execution/            Plan/lock lifecycle, immutable builder, executor, outcome grading, CLI
scripts/
  acceptance_harness.py Live acceptance harness and contract deployment driver
  external_harness.py   Out-of-tree Testermint build, upstream API verifier, network root prep
  run_go_boundary.py    Go query error classification boundary probe driver
  test_wasm_query_boundary.mjs  Compiled-Wasm ExternalQuerier ABI probe driver
harness/
  testermint/           External Gradle project with Kotlin Marketplace scenarios
  network/              Runner-owned Compose fragments and B3 genesis provisioner
  go_boundary/          Standalone Go boundary module
  wasm_query_allowlist/ Manual live Wasm gRPC allowlist probe
  container_control.py  Docker container stop/start controller by recorded container ID
vendor/
  contract_release/     Pinned copy of canonical contract release helper (release.py)
ops/
  runner/               Runner Dockerfile, Compose definition, DinD entrypoint, RUNNER_VERSION
  e2e/                  Host CLI wrappers (Bash and PowerShell)
tests/
  unit/runner/          Offline unit tests for forward_e2e.suite and forward_e2e.execution
  unit/harness/         Offline unit tests for scripts/ and vendor/contract_release/
  integration/local_sources/  Local Git object acquisition integration tests
  fixtures/             Deterministic synthetic test fixtures
docs/                   Architecture, operations, evidence, coverage, validation, migration, licensing
```

---

## Documentation Index

- [`docs/README.md`](docs/README.md) — full documentation map.
- [`docs/architecture.md`](docs/architecture.md) — runner layers, one-way import rule, and four-zone filesystem isolation.
- [`docs/operations.md`](docs/operations.md) — CLI reference (`list`, `plan`, `run`, `rerun`, `report`, `recover`), flags, credentials, and Docker volumes.
- [`docs/evidence.md`](docs/evidence.md) — evidence artifacts, schemas, proof levels, and whole-run verdict rules.
- [`docs/coverage.md`](docs/coverage.md) — 24-task scenario catalog, legacy aliases, proof levels, and known coverage boundaries.
- [`docs/validation.md`](docs/validation.md) — step-by-step operator and reviewer runbook.
- [`docs/development.md`](docs/development.md) — developer guide, test conventions, lock-hashed files, and maintenance rules.
- [`docs/migration.md`](docs/migration.md) — path, scenario ID, and environment variable migration reference.
- [`docs/licensing.md`](docs/licensing.md) — licensing terms and publication date policy.
- [`VERSIONS.md`](VERSIONS.md) — pinned runner toolchain and runtime image digests.
- [`AGENTS.md`](AGENTS.md) — contributor and coding-agent contract.

---

## Extraction Provenance

This repository was extracted from `gonka24/forward-contracts` at commit
`d637eea5432506d60c90c1d8436c67b93802d829`. Exact file-level SHA-256 provenance
at the extraction boundary is recorded in [`EXTRACTION.json`](EXTRACTION.json).

---

## Authors and License

### Authors

- Mikita Anikiyevich (Gonka24)

### License

Original Gonka24 material in this repository is licensed under the Business
Source License 1.1 (`BUSL-1.1`), changing to `Apache-2.0` after the Change Date
specified in [`LICENSE`](LICENSE). Third-party materials retain their original
licenses as documented in [`THIRD_PARTY.md`](THIRD_PARTY.md) and
[`docs/licensing.md`](docs/licensing.md).
