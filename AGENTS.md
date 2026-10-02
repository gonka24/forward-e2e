# Working in this repository

This file is the contract for anyone — human or agent — who changes code here.
Read it, then read [`ops/a8/README.md`](ops/a8/README.md) and
[`ops/e2e/README.md`](ops/e2e/README.md) before touching the runner.

Everything below describes what the code **does today**. Where a rule is only
partly enforced, that is said plainly rather than smoothed over.

---

## 1. What lives where

| Path | Role |
|---|---|
| `gonka24/forward-contracts` (separate checkout) | The Rust/CosmWasm code under test, selected by full SHA. |
| `scripts/a8_acceptance.py` | The acceptance harness. Runs the live scenarios. |
| `scripts/a8_external_harness.py` | Harness helpers: upstream API check, network work directory, Gradle argv, JUnit collection, B3 genesis verification. |
| `scripts/run_a8_go_boundary.py`, `scripts/test_wasm_query_boundary.mjs` | Boundary probes. |
| `scripts/a9_release.py` | Pinned runner-owned release helper; canonical maintenance is in `forward-contracts`, provenance in `EXTRACTION.json`. |
| `ops/a8/` | The suite runner: catalog, orchestrator, runtime, collector, verifier, reporter. |
| `ops/a8/source_snapshot.py` | The one immutability check for source snapshots (stdlib only; loaded by path from the harness). |
| `ops/a8/e2e/` | The E2E layer on top of it: plan, build, execute, grade, export. |
| `ops/a8/harness/testermint/` | External Gradle project with the Marketplace Kotlin scenarios; compiled against the selected Gonka's unmodified Testermint. |
| `ops/a8/harness/network/` | Runner-owned Compose fragments and the B3 genesis provisioner, copied into the network work directory. |
| `ops/a8/harness/container_control.py` | External Docker controller that stops/starts the same API container by saved ID. |
| `ops/a8/harness/go-boundary/`, `ops/a8/harness/wasm-probe/` | Go fixtures and the Wasm probe, built separately from production images. |
| `ops/a8/tests/` | Offline unit tests for both layers. |
| `scripts/tests/` | Offline unit tests for the harness and release tooling. |

There is **no `gonka-overlay/` any more.** Nothing is copied, patched or
committed into a Gonka checkout. Sources, build outputs, test code and network
state live in separate directories; see
[`ops/e2e/README.md`](ops/e2e/README.md) §7.

---

## 2. The import direction is one-way

`ops.a8.e2e.*` may import `ops.a8.*`. **`ops.a8.*` must never import
`ops.a8.e2e.*`.** The E2E layer is built on top of the suite runner, so the
reverse edge would be an import cycle.

How the existing code keeps that true:

- The suite runner accepts the handover context object and only reads
  attributes off it — see `SuiteOrchestrator.__init__` in
  `ops/a8/orchestrator.py` and the `e2e_context` parameters in
  `ops/a8/adapters.py`. Nothing is imported from `ops.a8.e2e`.
- `ops/a8/e2e/executor.py` imports the orchestrator **lazily**, inside
  `_default_suite_runner`, so planning, listing and reporting never pull in the
  execution machinery.
- No module under `ops/a8/*.py` contains an `ops.a8.e2e` import. Keep it that
  way; if you need a shared type, put it in `ops/a8/` (as
  `ops/a8/evidence_model.py` does) and import it downwards.

### `ops/a8/e2e/context.py` imports nothing from `ops.a8`

`E2ERunContext` is the handover object between the two layers, so it must be
importable by both. It is allowed `dataclasses`, `pathlib` and `typing` and
nothing else — **no relative imports at all, and no `ops.*` imports**.

That is why the E2E evidence-model identifier is repeated there as the literal
`E2ERunContext.E2E_EVIDENCE_MODEL` instead of being imported from
`ops/a8/evidence_model.py`. The duplication is deliberate and is pinned by
`test_e2e_run_context_has_no_ops_a8_imports` in
`ops/a8/tests/test_e2e_executor.py`, which parses the module with `ast` and
fails on any `ops.a8` import and on any relative import.

`scripts/a8_acceptance.py` repeats the literals (`EVIDENCE_MODEL_IMMUTABLE`,
the only model it produces, plus the historical `EVIDENCE_MODEL_E2E` and
`EVIDENCE_MODEL_LEGACY`, which it recognises only to refuse them) for the same
reason: it is executed standalone inside the runner image and re-entered by
Testermint, where the `ops` package is not importable.

The current identifiers are:

| Constant | Value | Status |
|---|---|---|
| `EVIDENCE_MODEL_IMMUTABLE` | `a8.evidence/e2e-immutable-source/2` | The only model new packages may declare and the only one graded as proof. |
| `EVIDENCE_MODEL_E2E` | `a8.evidence/e2e-prepared-build/1` | Historical (overlay + prepared commit). Readable, never proof of unmodified sources. |
| `EVIDENCE_MODEL_LEGACY` | `a8.evidence/legacy-overlay/1` | Historical. Readable, never proof. |

The same "load by path" constraint applies to `ops/a8/source_snapshot.py`: the
harness loads it with `importlib` from a bare path, so it is standard library
only, with no relative and no `ops.*` imports.

> [!IMPORTANT]
> If you change an evidence-model identifier you must change it in all three
> places: `ops/a8/evidence_model.py`, `ops/a8/e2e/context.py` and
> `scripts/a8_acceptance.py`.

---

## 3. Test conventions

```bash
python3 -B -m unittest discover -s ops/a8/tests
python3 -B -m unittest discover -s scripts/tests
```

- **`unittest` only.** There is no `pytest` dependency and no `conftest.py`; do
  not introduce either. Tests are plain `unittest.TestCase` classes.
- Run with `-B` so no `__pycache__` is written into the tree.
- Tests must be **offline**: no external network, Docker, live chain, or host
  toolchain. External boundaries (Git, Docker, subprocess, filesystem roots) are
  mocked; the function under test never is. A controlled HTTP fixture may bind
  only to `127.0.0.1` on an ephemeral port; it must shut down its server and
  must not be described as evidence of external connectivity.
- **Every test module's docstring states fixture provenance and boundary use.**
  If it makes no HTTP calls, end the module docstring with the line

  ```
  All fixtures are synthetic. No network, Docker, or live chain calls.
  ```

  It is a claim about the module, so it belongs in the module docstring and
  nowhere else. A loopback-only module instead ends with:

  ```
  All fixtures are synthetic. No external network, Docker, or live chain calls; HTTP is loopback-only.
  ```
- **Test names are long and behavioural**, describing the fact being proven,
  not the function being called. Existing examples from
  `ops/a8/tests/test_e2e_build_provenance.py`:

  ```
  test_a_runtime_reporting_the_requested_commit_instead_of_the_prepared_one_fails
  test_a_missing_prepared_test_harness_sha_is_not_compensated_by_the_source_sha
  test_a_payload_without_runtime_fails_strict_e2e_verification
  ```

### The binding fixture rule

`ops/a8/tests/real_fixtures.py` states the rule and is the place to extend:

| Fixture kind | Requirement |
|---|---|
| Recorded | Load the exact document bytes from `docs/reviews/evidence` when a test needs that historical evidence; record the path and relevant source/run identity. Do not silently edit it. |
| Synthetic positive | May mimic the producer's field names, nesting and types, but every value not sourced from a recorded artifact is synthetic. Mark it test-only and never treat its passing verifier result as live provenance. |
| Negative | Derived from a positive fixture by changing or deleting **exactly one mandatory fact**, while preserving its recorded/synthetic classification and shape. Never use a simplified unrelated shape. |

A negative fixture that also differs in shape proves nothing: the test would
pass even if the check under test were deleted. If no recorded artefact exists,
build a producer-shaped synthetic fixture from the producer code at a named
commit and mark it test-only; producer-shaped data is not itself recorded
evidence.

---

## 4. Provenance invariants that must never be weakened

These are load-bearing. Do not remove one to make a run go green, and do not
widen an allowlist to route around a failure.

| Invariant | Where it is enforced |
|---|---|
| **Full-SHA-only selection.** No branch, tag, `HEAD`, short SHA or revision expression, anywhere. | `normalize_full_sha` / `build_source_spec` in `ops/a8/e2e/sources.py`; the host wrappers repeat the check (`bridge_local_repo` in `ops/e2e/run-e2e.sh`). |
| **Sources are immutable.** Both product commits are materialised from Git objects and must stay exactly the selected commit (HEAD, tree, tracked bytes, submodules, no added or ignored files) before the build, after the build and after execution. There is no overlay, no prepared commit and no allow-list of paths that may differ. Build outputs, Gradle state, test code and network state live outside the snapshots. | `ops/a8/source_snapshot.py`; the before/after checks in `execute_plan` and `_verify_post_run_provenance` (`ops/a8/e2e/executor.py`); `Builder.assert_outputs_outside_sources` (`ops/a8/e2e/builder.py`); `run_live` in `scripts/a8_acceptance.py` (writes `source-immutability.json`); `verify_source_immutability_evidence` in `ops/a8/e2e/deployment.py`. |
| **Old plans are not executable.** `run`/`rerun` refuse a lock whose schema is not `e2e/run-lock/2` or which mentions an overlay/prepared commit; old packages are read by format version and classified as historical, never as immutable-source proof. | `assert_lock_executable` / `superseded_lock_markers` in `ops/a8/e2e/runlock.py`; `PlanSchemaSupersededError` in `ops/a8/e2e/errors.py`. |
| **The runner is independent of the targets.** Harness, external Kotlin tests, network templates, catalog and verifier always come from the runner image, never from the contracts checkout or the Gonka checkout. | `RunnerLayout` and `_runner_section` in `ops/a8/e2e/planner.py`; `BoundaryTaskAdapter.resolve_script` and the harness override in `ops/a8/adapters.py`; `assert_runner_matches_lock` in `ops/a8/e2e/executor.py`. |
| **The lock envelope is immutable.** A lock is written once, is never rewritten, and is verified by content hash on load. | `write_run_lock`, `load_run_lock`, `RunLock.envelope`, `copy_lock_into_run` in `ops/a8/e2e/runlock.py`. Post-build facts go into `build-manifest.json` / `execution-manifest.json`, bound to the lock by `lock_sha256`. |
| **Private dockerd.** The host Docker socket is never mounted; the runner starts its own inner daemon under an exclusive flock, and only `run`/`rerun` may start it. | `DinDSupervisor` in `ops/a8/supervisor.py`; `_with_inner_dockerd` in `ops/a8/e2e/cli.py` (called only from `cmd_run`). |
| **Ownership cleanup.** Every container a task started is recorded and torn down; the ownership record is evidence. | `perform_runtime_cleanup` in `ops/a8/runtime.py`; `cleanup-evidence/ownership.json` is re-read by `_verify_post_run_provenance` in `ops/a8/e2e/executor.py`. |
| **Cancellation is an interruption, not a verdict.** A signal stops the running build's process group, partial evidence is kept, and the result is `CANCELLED`. | `Cancellation` in `ops/a8/e2e/cancel.py`; the failure path and tail of `execute_plan`; `RUN_CANCELLED` handling in `_status_from_findings` in `ops/a8/e2e/outcome.py`. |
| **Timeouts are per task and come from the catalog**, and are frozen into the lock. | `timeout_minutes` / `stage_timeout_seconds` / `gradle_timeout_minutes` in `ops/a8/catalog.py`; `_limits_section` in `ops/a8/e2e/planner.py`; `SubprocessRunner` in `ops/a8/adapters.py`. |
| **Acceptance is always `NOT_REVIEWED`.** No automated path may award acceptance. | `AcceptanceStatus.NOT_REVIEWED` is the only value written, in `ops/a8/orchestrator.py` and `ops/a8/reporter.py`. |
| **`EXPECTED_PROTO_SHA` is never derived from the selected Gonka SHA.** It describes ABI/format compatibility, which is a different claim from "this binary is running". | `expected_proto_sha()` in `scripts/a8_acceptance.py`. The E2E layer never sets `A8_EXPECTED_PROTO_SHA`; it is treated as a frozen semantic environment variable (`SEMANTIC_ENV_PREFIXES` in `ops/a8/e2e/planner.py`, `apply_semantic_environment` in `ops/a8/e2e/executor.py`), so an ambient value is cleared rather than silently applied. |

Two further rules that follow from the above:

- **A suite result is not an E2E verdict.** `PASSED` for the whole run is
  decided only in `evaluate_run` (`ops/a8/e2e/outcome.py`), which is what `run`,
  `report` and `recover` all call.
- **Absence is never agreement.** A missing document, a missing live context or
  a silent evidence file is `INCOMPLETE`/`MISSING`, never a pass. See
  `verify_deployed_artifacts` in `ops/a8/e2e/deployment.py` and
  `locate_task_evidence` in `ops/a8/e2e/evidence.py`.

---

## 5. Files hashed into the lock

`ops/a8/e2e/planner.py` defines these lists whose contents are hashed into
`run.lock.json`:

| Constant | Hashed into | Members |
|---|---|---|
| `HARNESS_FILES` | `lock.runner.harness_hash` | `scripts/a8_acceptance.py`, `scripts/a9_release.py`, `scripts/run_a8_go_boundary.py`, `scripts/test_wasm_query_boundary.mjs`, `scripts/a8_external_harness.py`, `ops/a8/source_snapshot.py`, `ops/a8/harness/container_control.py` |
| `VERIFIER_FILES` | `lock.runner.verifier_hash` | `ops/a8/verifier.py`, `ops/a8/collector.py`, `ops/a8/reporter.py`, `ops/a8/models.py`, `ops/a8/catalog.py`, **`ops/a8/evidence_model.py`** |
| `NETWORK_FILES` | `lock.network.config_hashes` | `ops/a8/harness/network/a8-ownership.yml`, `ops/a8/harness/network/a8-nats.yml`, `ops/a8/harness/network/a8-b3-genesis.yml`, `ops/a8/harness/network/genesis/a8-genesis-provision.sh` |
| `EXTERNAL_TEST_DIRS` | `lock.external_tests.trees` / `tests_hash` | every file under `ops/a8/harness/testermint`, `ops/a8/harness/network`, `ops/a8/harness/go-boundary`, `ops/a8/harness/wasm-probe` |
| `RUNNER_VERSION_FILE` | `lock.runner.runner_version*` | `ops/a8/RUNNER_VERSION` — the runner version, pinned separately from the product commits |

`ops/a8/evidence_model.py` is in `VERIFIER_FILES` on purpose: it selects which
grading rules apply to a piece of evidence, so editing it changes what "passed"
means.

> [!WARNING]
> **Editing any file in these lists invalidates every existing lock.**
> `hash_runner_files` hashes the path name and the full bytes of each file, and
> `assert_runner_matches_lock` (`ops/a8/e2e/executor.py`) refuses to execute a
> lock whose `harness_hash`, `verifier_hash` or `catalog_hash` no longer match
> this runner. The correct response is to create a new plan, never to relax the
> check. Adding `ops/a8/evidence_model.py` to `VERIFIER_FILES` already
> invalidated locks planned before it existed.

`hash_runner_files` treats a **missing** file as an error rather than a shorter
hash, so deleting the verifier cannot produce a valid-looking lock. If you add
or rename a file in these lists, update `RunnerLayout.assert_complete` coverage
expectations accordingly — it checks `HARNESS_FILES + VERIFIER_FILES` for
existence.

The catalog itself is hashed separately by `compute_catalog_hash`
(`ops/a8/catalog.py`) into `lock.runner.catalog_hash`, with the same
consequence.

---

## 6. CI runs offline runner tests

`.github/workflows/ci.yml` defines three Python 3.11 suites in the
`python-offline-runner` job:

```yaml
- name: Run A9 release and deployment tooling tests
  run: python3 -B -m unittest discover -s scripts/tests -p 'test_*.py' -v

- name: Run A8 suite and E2E offline runner tests
  run: python3 -B -m unittest discover -s ops/a8/tests -p 'test_*.py' -v

- name: Verify local Git source acquisition
  run: python3 -B -m unittest discover -s ops/a8/integration_tests -p 'test_*.py' -v
```

When repository GitHub Actions are enabled, all three suites run on pull
requests and pushes to `main` with zero tolerance for failure. A configured
workflow is not evidence that GitHub actually ran it for a particular commit.
Real chain/Docker E2E executions require live infrastructure and are triggered on dedicated release/validation runs rather than every commit.

Consequences you must plan for:

- Run the two offline unit suites and the local Git integration suite shown above before submitting changes.
- Do not claim a written-but-unexecuted test as passing.
- A static-only review is a legitimate state; call it "ready for static review",
  not "verified".

---

## 7. Changing a document format

Producer and every consumer change **together, in the same change**, along with
the fixtures. The pairs that exist today:

| Document | Producer | Consumers |
|---|---|---|
| `run.json` | `_write_run_inputs` (`ops/a8/e2e/executor.py`) | operators |
| `status.json` | `_write_run_status` (`ops/a8/e2e/executor.py`) | operators |
| `run.lock.json` | `build_plan` (`ops/a8/e2e/planner.py`) | `load_run_lock`, `execute_plan`, `LoadedRunPackage` |
| `build-manifest.json` | `Builder` + `execute_plan` | `evaluate_run`, `LoadedRunPackage.provenance_findings` |
| `execution-manifest.json` | `execute_plan` | `evaluate_run`, `cli._resolve_recovery_target`, `cli._suite_dir_of_package` |
| `delivery.json` | `DeliveryService` (`ops/a8/e2e/delivery.py`) | `LoadedRunPackage.delivery`, `evaluate_run`, `DeliveryService.deliver_recovery` |
| `result.json` / `e2e-run-result.json` | `RunOutcome.write` via `grade_run_package` | operators; re-derived, never read back as truth |
| `live-context.json` | `scripts/a8_acceptance.py` (`run_live`) | `verify_live_context`, `verify_deployed_artifacts`, `_verify_post_run_provenance`, `LoadedRunPackage.provenance_findings` |
| `identity.json` | `prepare_runtime_snapshot` (`ops/a8/runtime.py`) | `OfflineReporter.task_source_identity`, `reconcile_suite_runtime_evidence` |
| `e2e-context.json` | `SuiteOrchestrator.run_suite` | `OfflineReporter` (`e2e_evidence_expected`) |
| `suite-plan.json` / `suite-result.json` | `SuiteOrchestrator` | `OfflineReporter`, `ops/a8/e2e/evidence.py`, `LoadedRunPackage` |
| `artifact-index.json` | `ops/a8/collector.py` | `OfflineReporter`, `reconcile_suite_runtime_evidence` |
| `source-immutability.json` | `scripts/a8_acceptance.py` (`run_live`, `build-external-harness`) via `ops/a8/source_snapshot.immutability_record`; live network runs use `a8.source-immutability-set/2` and include `network_root` | `check_source_immutability_document` (`ops/a8/verifier.py`) compares the working-copy verdict with `network/network-manifest.json`; `verify_source_immutability_evidence` and `_verify_post_run_provenance` delegate to the same rule |
| `external-harness/*`, `testermint-junit/*.xml` | `scripts/a8_external_harness.py` | `run_live` (selected test must have run); collector |
| `network/network-manifest.json` | `prepare_network_root` (`scripts/a8_external_harness.py`) | `verify_network_root_integrity`, live-context `network_manifest_sha256`; collector |
| `genesis/*.json` (B3 only) | `ops/a8/harness/network/genesis/a8-genesis-provision.sh` + `verify_b3_genesis_delta` | `run_live`; collector |
| `container-control/*.json` | `ops/a8/harness/container_control.py` | collector; reviewers (same `container_id` on stop and start) |

When a new artefact path is added by a producer, add it to
`ALLOWED_ARTIFACT_PATTERNS` in `ops/a8/collector.py` as well, or it is simply
never copied into the suite and the reporter can only report it as missing.

---

## 8. House style

- Comments and docstrings explain **why**, especially why a check exists and
  what failure it prevents. Do not delete existing rationale while editing
  around it.
- Errors carry a stable machine-readable `code` and an `exit_code`
  (`ops/a8/e2e/errors.py`). Add a subclass rather than a bare `RuntimeError`.
- Exit codes: `0` success, `1` failure, `2` usage error, `130`/`143` on
  SIGINT/SIGTERM.
- Paths coming out of a document are resolved with `resolve_within`
  (`ops/a8/e2e/sources.py`) before being read or written. Symlinks in evidence
  are refused, not followed.
- Prefer one shared function called by both the online and the offline path over
  two implementations that can drift — `verify_deployed_artifacts` is the model.
