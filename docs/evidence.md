# Evidence Model and Run Package Reference

This document specifies the layout of a run package, every evidence artifact
produced during a run, the producer/consumer contract, proof levels, and how the
whole-run verdict is computed.

---

## 1. Durable Stage vs Exported Run Package

Every `run` writes first to a durable staging directory on the persistent
`/workspace` volume and then exports that package to the host `--output`
directory:

```text
<workspace>/<run-id>/
  run/                           Durable run package (source of truth for export & recover)
  suite/runtime/                 Per-task runtime snapshots and cleanup-evidence/ownership.json
  src/ scratch/ git-helper/      Working checkouts and temporary build scratch
<workspace>/image-provenance.json  Local Docker image provenance ledger shared across runs
```

Exported host directory (`<output>/<run-id>/`):

```text
<output>/<run-id>/
  run.json                       Concise run inputs summary (schema: e2e/run-inputs/1)
  status.json                    Live lifecycle stage and task progress (schema: e2e/run-status/1)
  run.lock.json                  Byte-identical execution plan (schema: e2e/run-lock/2)
  build-manifest.json            Built image IDs, contract release hashes, runtime dependencies, tool versions
  execution-manifest.json        Run ID, parent/replay lineage, lock_sha256, build_manifest_sha256, suite paths
  delivery.json                  Export delivery ledger
  build/                         Raw build outputs (including contract release manifest and Wasm binaries)
  build-logs/                    Per-step build logs
  suite/<run-id>/                Exported suite evidence (see below)
  result.json                    Whole-run verdict (re-derived on grade, identical to e2e-run-result.json)
  e2e-run-result.json            Whole-run verdict (schema: e2e/run-result/1)
```

Suite evidence directory (`<output>/<run-id>/suite/<run-id>/`):

```text
suite/<run-id>/
  suite-plan.json                Selected catalog tasks and suite configuration
  suite-result.json              Per-task execution status and verifier findings
  e2e-context.json               Handover expectations from E2ERunContext
  artifact-index.json            SHA-256 index of every collected task artifact
  summary.md                     Human-readable suite report
  coverage.json                  Machine-readable task coverage summary
  tasks/<task-id>/
    identity.json                Task source identity and evidence model
    live-context.json            Live scenario execution log, checkpoints, deployed contract hashes (NATIVE tasks)
    source-immutability.json     Before/after source and network-root immutability record
    external-harness/            Upstream API check, exported classpath, harness input hashes
    testermint-junit/*.xml       JUnit XML reports from external Gradle test run
    network/network-manifest.json  Copied Gonka network-root manifest and runtime additions
    container-control/*.json     API stop/start container ID records (restart scenarios)
    genesis/*.json               Before/after genesis snapshots and delta verification (B3 only)
    cleanup-evidence/ownership.json  Post-task Docker container teardown evidence
```

---

## 2. Producer and Consumer Matrix

When changing any document format, update the producer, every consumer, and the
offline test fixtures in the same change:

| Document | Producer | Consumers |
|---|---|---|
| `run.json` | `_write_run_inputs` ([`forward_e2e/execution/executor.py`](../forward_e2e/execution/executor.py)) | Operators |
| `status.json` | `_write_run_status` ([`forward_e2e/execution/executor.py`](../forward_e2e/execution/executor.py)) | Operators |
| `run.lock.json` | `build_plan` ([`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py)) | `load_run_lock`, `execute_plan`, `LoadedRunPackage` |
| `build-manifest.json` | `Builder` + `execute_plan` ([`forward_e2e/execution/builder.py`](../forward_e2e/execution/builder.py)) | `evaluate_run`, `LoadedRunPackage.provenance_findings` |
| `execution-manifest.json` | `execute_plan` ([`forward_e2e/execution/executor.py`](../forward_e2e/execution/executor.py)) | `evaluate_run`, `cli._resolve_recovery_target`, `cli._suite_dir_of_package` |
| `delivery.json` | `DeliveryService` ([`forward_e2e/execution/delivery.py`](../forward_e2e/execution/delivery.py)) | `LoadedRunPackage.delivery`, `evaluate_run`, `DeliveryService.deliver_recovery` |
| `result.json` / `e2e-run-result.json` | `RunOutcome.write` via `grade_run_package` ([`forward_e2e/execution/outcome.py`](../forward_e2e/execution/outcome.py)) | Operators; always re-derived, never read back as input truth |
| `live-context.json` | `run_live` ([`scripts/acceptance_harness.py`](../scripts/acceptance_harness.py)) | `verify_live_context`, `verify_deployed_artifacts`, `_verify_post_run_provenance`, `LoadedRunPackage.provenance_findings` |
| `identity.json` | `prepare_runtime_snapshot` ([`forward_e2e/suite/runtime.py`](../forward_e2e/suite/runtime.py)) | `OfflineReporter.task_source_identity`, `reconcile_suite_runtime_evidence` |
| `e2e-context.json` | `SuiteOrchestrator.run_suite` ([`forward_e2e/suite/orchestrator.py`](../forward_e2e/suite/orchestrator.py)) | `OfflineReporter` (`e2e_evidence_expected`) |
| `suite-plan.json` / `suite-result.json` | `SuiteOrchestrator` ([`forward_e2e/suite/orchestrator.py`](../forward_e2e/suite/orchestrator.py)) | `OfflineReporter`, [`forward_e2e/execution/evidence.py`](../forward_e2e/execution/evidence.py), `LoadedRunPackage` |
| `artifact-index.json` | [`forward_e2e/suite/collector.py`](../forward_e2e/suite/collector.py) | `OfflineReporter`, `reconcile_suite_runtime_evidence` |
| `source-immutability.json` | `scripts/acceptance_harness.py` via [`forward_e2e/suite/source_snapshot.py`](../forward_e2e/suite/source_snapshot.py) | `check_source_immutability_document` ([`forward_e2e/suite/verifier.py`](../forward_e2e/suite/verifier.py)), `verify_source_immutability_evidence`, `_verify_post_run_provenance` |
| `external-harness/*`, `testermint-junit/*.xml` | [`scripts/external_harness.py`](../scripts/external_harness.py) | `run_live` (verifies selected test executed), `collector.py` |
| `network/network-manifest.json` | `prepare_network_root` ([`scripts/external_harness.py`](../scripts/external_harness.py)) | `verify_network_root_integrity`, `live-context.json` (`network_manifest_sha256`), `collector.py` |
| `genesis/*.json` (B3 only) | [`harness/network/genesis/foreign-native-genesis-provision.sh`](../harness/network/genesis/foreign-native-genesis-provision.sh) + `verify_b3_genesis_delta` | `run_live`, `verifier.py`, `collector.py` |
| `container-control/*.json` | [`harness/container_control.py`](../harness/container_control.py) | `collector.py`, reviewers (verifies identical `container_id` across stop and start) |

---

## 3. Evidence Models

Defined in [`forward_e2e/suite/evidence_model.py`](../forward_e2e/suite/evidence_model.py),
[`forward_e2e/execution/context.py`](../forward_e2e/execution/context.py), and
[`scripts/acceptance_harness.py`](../scripts/acceptance_harness.py):

| Identifier | Constant | Status | Meaning |
|---|---|---|---|
| `a8.evidence/e2e-immutable-source/2` | `EVIDENCE_MODEL_IMMUTABLE` | **Active (Required)** | Both Gonka and contracts commits are built and tested unmodified. Running binaries report the exact selected Gonka SHA. Only model accepted as proof in new runs. |
| `a8.evidence/e2e-prepared-build/1` | `EVIDENCE_MODEL_E2E` | Historical (Read-only) | Legacy prepared-commit model where an overlay was committed on top of Gonka before building. Classified as `historical-prepared-build` by `report`/`recover`; rejected inside new run packages. |
| `a8.evidence/legacy-overlay/1` | `EVIDENCE_MODEL_LEGACY` | Historical (Read-only) | Pre-E2E dirty-overlay model. Readable by offline verifiers for historical comparison; never accepted as E2E proof. |

In `a8.evidence/e2e-immutable-source/2`, `live-context.json.source` must record:

- `gonka_sha` and `gonka_tree_sha`
- `marketplace_commit_sha` and `marketplace_tree_sha`
- `source_immutability_verdict` (`"immutable"`) and `source_immutability_sha256`
- `external_harness_inputs_sha256` and `network_manifest_sha256`
- `a9_manifest_sha256`, `a9_contract_sha256`, and `test_contract_sha256`
- `runtime` (matching the selected `gonka_sha`, `wasmd`, `wasmvm`, and `runner_binary_sha256`)

Historical overlay fields (`gonka_prepared_sha`, `gonka_overlay_manifest_sha256`,
`gonka_test_harness_sha`) are forbidden in `a8.evidence/e2e-immutable-source/2`.

---

## 4. Proof Levels

Every catalog task declares its honest `ProofLevel`
([`forward_e2e/suite/models.py`](../forward_e2e/suite/models.py)):

| Proof Level | What It Executes | What It Proves |
|---|---|---|
| `NATIVE` | Multi-container Gonka + Testermint cluster (`inferenced`, Decentralization API, PostgreSQL, CoreDNS, NATS) with deployed CosmWasm contracts | End-to-end chain execution, epoch advancement, reward settlement, bank transfers, and API fault recovery against unmodified Gonka and contracts commits. |
| `GO_BOUNDARY` | Standalone Go test (`harness/go_boundary`) compiled against `<gonka>/inference-chain` via [`scripts/run_go_boundary.py`](../scripts/run_go_boundary.py) | Go query error classification and fault-plan boundary behavior without claiming live chain execution. |
| `WASM_ABI` | Compiled CosmWasm probe (`tests/contracts/a8-query-boundary`) driven by Node.js synthetic `ExternalQuerier` ([`scripts/test_wasm_query_boundary.mjs`](../scripts/test_wasm_query_boundary.mjs)) | Protobuf wire encoding/decoding and `ExternalQuerier` ABI handling across the Wasm boundary (`scope_results` must be `6/6` passed). |
| `CONTRACT_TEST` | `cargo test --locked` on `forward-contracts` (`contract-policy-suite` / `ct-package-c-policy`, 71 required test cases) | Deterministic contract policy, validation, and `cw-multi-test` state transitions that are unreachable through honest native chain consensus. |

Non-native tasks (`GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`) are never
relabelled as `NATIVE`.

---

## 5. Whole-Run Verdicts and Acceptance Status

`evaluate_run` ([`forward_e2e/execution/outcome.py`](../forward_e2e/execution/outcome.py))
computes one of four `RunStatus` values:

| `RunStatus` | Exit Code | Condition |
|---|---|---|
| `PASSED` | `0` | Lock, build manifest, and execution manifest are sealed and hash-bound; source snapshots are immutable before build, after build, and after execution; every built/runtime image matches provenance; every selected task passed verification with complete evidence; container ownership cleanup succeeded. |
| `FAILED` | `1` | A build step failed, a task or verifier check failed, source immutability was violated, or a provenance/deployment check failed. |
| `INCOMPLETE` | `1` | Required manifests, suite outputs, or task evidence files are missing or unsealed (absence is never treated as agreement). |
| `CANCELLED` | `130` / `143` | Run was interrupted by `SIGINT` or `SIGTERM`. Partial evidence is preserved, but a cancellation is an interruption, not a verdict. |

### `AcceptanceStatus` Is Always `NOT_REVIEWED`

Both `suite-result.json` and `summary.md` always record
`acceptance_status: "NOT_REVIEWED"`. Automated tooling verifies technical
invariants and provenance; human release sign-off is a separate review step.
