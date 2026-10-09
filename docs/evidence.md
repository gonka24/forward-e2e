# Evidence Model and Run Package Reference

This document specifies the layout of a run package, every evidence artifact
produced during a run, the producer/consumer contract, proof levels, and how the
whole-run verdict is computed.

---

## 1. Durable Stage vs Exported Run Package

Every `run` writes first to a durable staging directory on the persistent
`/workspace` volume and then exports that package to the `--output` directory
(inside the container `/out`; the host wrappers mount `<repo>/out` there by
default, so a run lands at `<repo>/out/runs/<run-id>` on the host — see
[`operations.md`](operations.md) §4):

```text
<workspace>/<run-id>/
  run/                           Durable run package (source of truth for export & recover)
  suite/runtime/                 Per-task runtime snapshots and cleanup-evidence/ownership.json
  src/ scratch/ git-helper/      Working checkouts and temporary build scratch
<workspace>/image-provenance.json  Local Docker image provenance ledger shared across runs
```

Exported run package (`<output>/<run-id>/`; file names and schema identifiers
are the constants in `forward_e2e/execution/runlock.py`, `executor.py` and
`outcome.py`):

```text
<output>/<run-id>/
  run.json                       Concise run inputs summary (schema_version: e2e/run-inputs/1)
  status.json                    Live lifecycle stage and task progress (schema_version: e2e/run-status/1)
  run.lock.json                  Byte-identical execution plan (RUN_LOCK_SCHEMA e2e/run-lock/2,
                                 wrapped in LOCK_ENVELOPE_SCHEMA e2e/run-lock-envelope/1)
  build-manifest.json            Built image IDs, contract release hashes, runtime dependencies,
                                 tool versions (BUILD_MANIFEST_SCHEMA e2e/build-manifest/2)
  execution-manifest.json        Run ID, parent/replay lineage, lock_sha256, build_manifest_sha256,
                                 suite paths (EXECUTION_MANIFEST_SCHEMA e2e/execution-manifest/2)
  delivery.json                  Export delivery ledger (DELIVERY_MANIFEST_SCHEMA e2e/delivery-manifest/1)
  build/                         Raw build outputs (including contract release manifest and Wasm binaries)
  build-logs/                    Per-step build logs
  suite/<run-id>/                Exported suite evidence (see below)
  result.json                    Whole-run verdict (RUN_RESULT_SHORT_FILENAME; same content as below)
  e2e-run-result.json            Whole-run verdict (RUN_RESULT_FILENAME; RUN_RESULT_SCHEMA e2e/run-result/2)
  e2e-run-result.historical-regrade.json
                                 Only for a historical package that already carries its own
                                 e2e-run-result.json: this runner's re-derived verdict is written
                                 beside the original, never over it (RunOutcome.write)
```

`e2e/run-result/1` documents written by an earlier runner remain readable
(`SUPPORTED_RUN_RESULT_SCHEMAS`); new verdicts are always `/2`.

Suite evidence directory (`<output>/<run-id>/suite/<run-id>/`; the suite id is
the run id, `suite_export_dir` in `forward_e2e/execution/executor.py`). Task
directories are keyed by the **task run id**, `make_task_run_id(suite_id,
ordinal, task_id)` in `forward_e2e/suite/models.py` — the suite id truncated to
30 characters, a two-digit ordinal and the truncated task id — not by the bare
task id:

```text
suite/<run-id>/
  suite-plan.json                Selected catalog tasks and suite configuration
  suite-result.json              Per-task execution status and verifier findings
  e2e-context.json               Handover expectations from E2ERunContext
  artifact-index.json            SHA-256 index of every collected task artifact
  summary.md                     Human-readable suite report ("# Forward E2E Suite Summary: <suite_id>")
  coverage.json                  Machine-readable task coverage summary
  events.jsonl                   Orchestrator event log
  runs/<task-run-id>/
    identity.json                Task source identity and evidence model (mandatory for every run)
    started.json, result.json, launcher.log
    junit/TEST-*.xml             JUnit XML copied by NativeTaskAdapter._collect_junit (NATIVE tasks)
    cleanup-evidence/ownership.json, completed.json, container-logs/*.log
                                 Post-task Docker container teardown evidence
    evidence/<task-run-id>/      The harness is handed `<snapshot>/evidence` and appends the run id
                                 itself, so the id appears twice (EvidenceRequirement.live_context_candidates)
      live-context.json          Live scenario execution log, checkpoints, deployed contract hashes (NATIVE)
      source-immutability.json   Before/after source and network-root immutability record (NATIVE)
      external-harness/          api-compat.json, upstream-classpath.log, testermint-classpath.txt(.json),
                                 harness-inputs.json
      testermint-junit/*.xml     JUnit XML as produced by the external Gradle test run
      network/network-manifest.json  Copied Gonka network-root manifest and runtime additions
      container-control/*.json   API stop/start container ID records (restart scenarios)
      genesis/*.json             Before/after genesis snapshots and delta verification
                                 (foreign-native-preservation only)
      testermint.log
    evidence/                    Boundary tasks write directly here: build.log, raw/go-test.json,
                                 raw/exit-code, report.json (GO_BOUNDARY); abi.json,
                                 a8_query_boundary.wasm, wasm-build.log, node-probe.log (WASM_ABI);
                                 <task_id>.log (CONTRACT_TEST)
```

Only paths matching `ALLOWED_ARTIFACT_PATTERNS` in
[`forward_e2e/suite/collector.py`](../forward_e2e/suite/collector.py) are
copied into the suite; the patterns exist for both the nested
(`evidence/*/…`) and the flat (`evidence/…`) layouts, and the reporter
tolerates both (`live_context_candidates` lists them in a fixed order so that
one task's file can never stand in for another's).

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
| `result.json` / `e2e-run-result.json` / `e2e-run-result.historical-regrade.json` | `RunOutcome.write` via `grade_run_package` ([`forward_e2e/execution/outcome.py`](../forward_e2e/execution/outcome.py), [`executor.py`](../forward_e2e/execution/executor.py)) | Operators; always re-derived, never read back as input truth |
| `live-context.json` | `run_live` ([`scripts/acceptance_harness.py`](../scripts/acceptance_harness.py)) | `verify_live_context`, `verify_deployed_artifacts`, `_verify_post_run_provenance`, `LoadedRunPackage.provenance_findings` |
| `live-context.json.settlement_token`, `.bootstrap.funding_rejections`, `.source.settlement_token_*` | `bootstrap` / `run_live` | `suite/settlement_token.py.verify_local_token_evidence`, called by both suite verification and shared online/offline deployment verification; the latter compares the declared token mode with the lock's semantic environment |
| `live-context.json.phases[].withdrawal_repeats` in mainnet-USDT settlement | `claim_settle` | `verifier.py._validate_mainnet_usdt_settlement`; verifies native summary arithmetic, positive three-role payouts, included repeat rejections and unchanged paid/pending ledgers |
| `identity.json` | `prepare_runtime_snapshot` ([`forward_e2e/suite/runtime.py`](../forward_e2e/suite/runtime.py)) | `build_task_source_identity` through `verify_suite_artifacts_integrity` ([`forward_e2e/suite/verifier.py`](../forward_e2e/suite/verifier.py)), `reconcile_suite_runtime_evidence` ([`forward_e2e/suite/orchestrator.py`](../forward_e2e/suite/orchestrator.py)) |
| `e2e-context.json` | `SuiteOrchestrator.run_suite` ([`forward_e2e/suite/orchestrator.py`](../forward_e2e/suite/orchestrator.py)) | `OfflineReporter` (`e2e_evidence_expected`) |
| `suite-plan.json` / `suite-result.json` | `SuiteOrchestrator` ([`forward_e2e/suite/orchestrator.py`](../forward_e2e/suite/orchestrator.py)) | `OfflineReporter`, [`forward_e2e/execution/evidence.py`](../forward_e2e/execution/evidence.py), `LoadedRunPackage` |
| `artifact-index.json` | [`forward_e2e/suite/collector.py`](../forward_e2e/suite/collector.py) | `OfflineReporter`, `verify_suite_artifacts_integrity`, `reconcile_suite_runtime_evidence` |
| `summary.md` / `coverage.json` | `OfflineReporter` ([`forward_e2e/suite/reporter.py`](../forward_e2e/suite/reporter.py)) | Reviewers |
| `source-immutability.json` | `write_source_immutability` in `scripts/acceptance_harness.py`, fingerprints from [`forward_e2e/suite/source_snapshot.py`](../forward_e2e/suite/source_snapshot.py) | `check_source_immutability_document` ([`forward_e2e/suite/verifier.py`](../forward_e2e/suite/verifier.py)) — the single implementation, called by `verify_live_context` and by `verify_source_immutability_evidence` / `_verify_post_run_provenance` |
| `external-harness/*`, `testermint-junit/*.xml` | [`scripts/external_harness.py`](../scripts/external_harness.py) (`run_required_api_check`, `export_upstream_classpath`, `write_harness_inputs`) | `run_live` (verifies the selected test executed), `collector.py`; `harness-inputs.json` is hashed into `live-context.json` |
| `network/network-manifest.json` | `prepare_network_root` ([`scripts/external_harness.py`](../scripts/external_harness.py)) | `verify_network_root_integrity`, `live-context.json` (`network_manifest_sha256`), `check_source_immutability_document` (network-root block), `collector.py` |
| `genesis/*.json` (`foreign-native-preservation` only) | [`harness/network/genesis/foreign-native-genesis-provision.sh`](../harness/network/genesis/foreign-native-genesis-provision.sh) + `verify_b3_genesis_delta` | `run_live`, `verifier.py`, `collector.py` |
| `container-control/*.json` | [`harness/container_control.py`](../harness/container_control.py) | `collector.py`, reviewers (verifies identical `container_id` across stop and start) |
| `report.json`, `raw/go-test.json`, `raw/exit-code`, `build.log` | [`scripts/run_go_boundary.py`](../scripts/run_go_boundary.py) | `verify_go_boundary_report` |
| `abi.json`, `a8_query_boundary.wasm` | `BoundaryTaskAdapter` + [`scripts/test_wasm_query_boundary.mjs`](../scripts/test_wasm_query_boundary.mjs) | `verify_wasm_abi_report` |

### Wire identifiers inside the documents

The schema and evidence-model identifiers written *into* the documents keep
their historical `a8.` prefix. They are compared as exact strings by every
consumer, including consumers of packages produced before the rename, so
changing the spelling would make every existing document unreadable for no
gain; the names of files, directories and tasks were renamed, the wire
identifiers were not (see [`migration.md`](migration.md) §4). The identifiers
in use:

| Identifier | Written by | Document |
|---|---|---|
| `a8.evidence/e2e-immutable-source/2` (and the two historical models, §3) | `run_live`, `prepare_runtime_snapshot` | `live-context.json.source.evidence_model`, `identity.json` |
| `a8.source-immutability-set/1` / `a8.source-immutability-set/2` | `write_source_immutability` (`/2` when a network root was verified) | `source-immutability.json` |
| `a8.source-immutability/1` | `forward_e2e/suite/source_snapshot.py` | per-root record inside `source-immutability.json` |
| `a8.source-snapshot/1` | `forward_e2e/suite/source_snapshot.py` (`SNAPSHOT_SCHEMA`) | source fingerprints before/after build and execution |
| `a8.network-manifest/1` | `prepare_network_root` (`NETWORK_MANIFEST_SCHEMA`) | `network/network-manifest.json` |
| `a8.testermint-api-compat/1` | `run_required_api_check` (`API_COMPAT_SCHEMA`) | `external-harness/api-compat.json` |
| `a8.external-harness-inputs/1` | `write_harness_inputs` (`HARNESS_INPUTS_SCHEMA`) | `external-harness/harness-inputs.json` |
| `a8.container-control/1` | `harness/container_control.py` (`STATE_SCHEMA`) | `container-control/*.json` |

The E2E layer's own documents use the `e2e/…` identifiers listed in §1.

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

In `a8.evidence/e2e-immutable-source/2`, `live-context.json.source` must record
(`verify_live_context` in `forward_e2e/suite/verifier.py`):

- `gonka_sha` and `gonka_tree_sha`
- `marketplace_commit_sha` and `marketplace_tree_sha`
- `source_immutability_verdict`, which must be exactly `"UNCHANGED"` (the
  producer's other values are `VIOLATED` and `INCOMPLETE`; neither can pass)
  and `source_immutability_sha256`
- `external_harness_inputs_sha256` and `network_manifest_sha256` — together
  with `source_immutability_sha256` these are `IMMUTABLE_SOURCE_HASHED_FILES`:
  each declared hash is recomputed from the file next to the context
- `a9_manifest_sha256`, `a9_contract_sha256`, and `test_contract_sha256`
- `runtime`: the identity reported by the running `inferenced` binary
  (`parse_runtime_identity` in the harness): `gonka_source_sha`, which must
  equal the selected `gonka_sha`, plus `wasmd`, `wasmvm`, `go` and
  `cosmos_sdk`

The four retired prepared-source fields in `RETIRED_PREPARED_SOURCE_FIELDS`
— `gonka_prepared_sha`, `gonka_overlay_manifest_sha256`,
`gonka_test_harness_sha` and `gonka_base_sha` — are forbidden in
`a8.evidence/e2e-immutable-source/2`.

A document carrying `test_fixture_only: true` is refused by
`verify_live_context` outright, whatever else it contains: the committed
fixtures under `tests/fixtures/evidence/` are synthetic and must never grade as
live evidence. The boundary verifiers (`verify_go_boundary_report`,
`verify_wasm_abi_report`) apply the same refusal to their reports.

---

## 4. Proof Levels

Every catalog task declares its honest `ProofLevel`
([`forward_e2e/suite/models.py`](../forward_e2e/suite/models.py)):

| Proof Level | What It Executes | What It Proves |
|---|---|---|
| `NATIVE` | Multi-container Gonka + Testermint cluster (`inferenced`, Decentralization API, PostgreSQL, CoreDNS, NATS) with deployed CosmWasm contracts | End-to-end chain execution, epoch advancement, reward settlement, bank transfers, and API fault recovery against unmodified Gonka and contracts commits. |
| `GO_BOUNDARY` | Standalone Go test (`harness/go_boundary`) compiled against `<gonka>/inference-chain` inside a Docker build derived from Gonka's own Dockerfile via [`scripts/run_go_boundary.py`](../scripts/run_go_boundary.py) | Go query error classification and fault-plan boundary behavior (`GO_BOUNDARY_REQUIRED_TESTS`) without claiming live chain execution or FFI behaviour. |
| `WASM_ABI` | Compiled CosmWasm probe (`tests/contracts/a8-query-boundary` in the contracts checkout) driven by Node.js synthetic `ExternalQuerier` ([`scripts/test_wasm_query_boundary.mjs`](../scripts/test_wasm_query_boundary.mjs)) | Protobuf wire encoding/decoding and `ExternalQuerier` ABI handling across the Wasm boundary: exactly the nine cases in `WASM_ABI_REQUIRED_CASES` must pass (`verify_wasm_abi_report`). |
| `CONTRACT_TEST` | `cargo test --locked` on `forward-contracts` (`contract-network-unconfirmed-policy`, `contract-claim-expiry-policy`, `contract-query-fault-policy` / alias `ct-package-c-policy`; the last requires the 18 `PACKAGE_C_POLICY_TESTS` and 71 `PACKAGE_C_POLICY_CASES`) | Deterministic contract policy, validation, and `cw-multi-test` state transitions that are unreachable through honest native chain consensus. |

Non-native tasks (`GO_BOUNDARY`, `WASM_ABI`, `CONTRACT_TEST`) are never
relabelled as `NATIVE`. See [`coverage.md`](coverage.md) for the per-task
checkpoints and the obligation mapping.

---

## 5. Whole-Run Verdicts and Acceptance Status

`evaluate_run` ([`forward_e2e/execution/outcome.py`](../forward_e2e/execution/outcome.py))
computes one of four `RunStatus` values:

| `RunStatus` | Exit Code | Condition |
|---|---|---|
| `PASSED` | `0` | Lock, build manifest, and execution manifest are sealed and hash-bound; source snapshots are immutable before build, after build, and after execution; every built/runtime image matches provenance; every selected task passed verification with complete evidence; container ownership cleanup succeeded; the provenance model is `immutable-source`. |
| `FAILED` | `1` | A build step failed, a task or verifier check failed, source immutability was violated, or a provenance/deployment check failed. |
| `INCOMPLETE` | `1` | Required manifests, suite outputs, or task evidence files are missing or unsealed (absence is never treated as agreement). |
| `CANCELLED` | `130` / `143` | Run was interrupted by `SIGINT` or `SIGTERM`. Partial evidence is preserved, but a cancellation is an interruption, not a verdict. |

A package classified `historical-prepared-build` by `classify_provenance_model`
additionally carries the blocking `HISTORICAL_PREPARED_BUILD` finding and can
never be `PASSED` on this runner, whichever `runner_version` string it records.

### `AcceptanceStatus` Is Always `NOT_REVIEWED`

Both `suite-result.json` and `summary.md` always record
`acceptance_status: "NOT_REVIEWED"`. Automated tooling verifies technical
invariants and provenance; human release sign-off is a separate review step.
