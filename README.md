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
