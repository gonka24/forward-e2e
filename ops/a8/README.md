# A8 Acceptance Runner Subsystems & Architecture

- Canonical location: `ops/a8/` in `gonka24/forward-e2e`.
- Date: 2026-09-14
- Runner version: `a8-runner/2.0.0` (see [`RUNNER_VERSION`](RUNNER_VERSION))

---

## 1. Overview

`ops/a8/` houses the **internal core subsystems** of the acceptance runner:
- **Catalog** (`catalog.py`): Scenario definitions, timeouts, and limits.
- **Runtime** (`runtime.py`): Git bundle preparation and worktree isolation.
- **Supervisor** (`supervisor.py`): Private Docker-in-Docker supervisor (`DinDSupervisor`) with flock management.
- **Orchestrator** (`orchestrator.py`): Sequential test execution and staging.
- **Collector** (`collector.py`): Artifact indexing and path filtering.
- **Verifier** (`verifier.py`): Cryptographic provenance and checkpoint verification against the active plan's exact selected `gonka_sha` (no prepared commit, no historical SHA exemptions).
- **Reporter** (`reporter.py`): Offline markdown summary and coverage generation.
- **Evidence Model** (`evidence_model.py`): The immutable-source evidence model contract (`a8.evidence/e2e-immutable-source/2`); earlier models are historical.
- **Source snapshot** (`source_snapshot.py`): The single immutability check for the Gonka and contracts snapshots.
- **External harness** (`harness/`): Kotlin scenarios, network templates, container controller, Go and Wasm probes — all outside Gonka.

> [!IMPORTANT]
> `ops/a8/` contains **internal library modules**, not a separate public execution entrypoint.
> The single supported operational interface for all runs, builds, and re-executions is the **target E2E runner**,
> documented in [`ops/e2e/README.md`](../e2e/README.md) and invoked via host wrappers
> [`ops/e2e/Run-E2E.ps1`](../e2e/Run-E2E.ps1) / [`ops/e2e/run-e2e.sh`](../e2e/run-e2e.sh) or Compose service `e2e-runner`.

> [!NOTE]
> Changing anything under `ops/a8/`? Read [`AGENTS.md`](../../AGENTS.md) first:
> import direction, test conventions, the provenance invariants that may not be
> weakened, and which files are hashed into a run lock.


### What Changed: Target Build Toolchains Inside the Runner
- **Windows / Linux / macOS Unified**: Users do not need to configure WSL2, Linux virtual machines, or host-installed target build toolchains (OpenJDK, Rust, Go, Node, cosmwasm-check, procps).
- **Host Requirements**: Docker Engine (Docker Desktop on Windows/macOS or Docker CE on Linux) with Compose V2. Local `--gonka-path` or `--contracts-path` sources also require host Git for the wrapper's Git-object bridge; remote-source runs do not.
- **Zero Host Docker Socket Exposure**: The host Docker socket (`/var/run/docker.sock` or `//./pipe/docker_engine`) is **NEVER** mounted. The runner container runs its own private, isolated inner Docker daemon (`dockerd`).

---

## 2. Key Architecture Guarantees

1. **Inner Private Dockerd (`privileged: true`)**:
   - The container runs a fully isolated inner Docker daemon using `--data-root /var/lib/docker` stored on a persistent named Docker volume (`a8-docker-root`).
   - `privileged: true` is strictly required to allow the inner dockerd to manage kernel cgroups, namespaces, and mount overlayfs filesystems.
2. **Global Exclusive Flocking**:
   - A POSIX `fcntl.flock` lock on `/workspace/exclusive.lock` (`RuntimeLock` in `ops/a8/lock.py`) is acquired **before** inner dockerd starts and held until dockerd terminates.
   - Concurrent container runs attempting `run` fail fast (exit code 1) with an informative message, preventing corruption of shared Docker data.
3. **Pure Offline Recovery & Persistent Evidence**:
   - Raw execution manifests and staged suite evidence are staged in `<workspace>/<run_id>/run` (with suite evidence at `<workspace>/<run_id>/run/suite/<run_id>` and per-task runtime snapshots at `<workspace>/<run_id>/suite/runtime`) on the persistent named volume `a8-workspace` (mounted at `/workspace`), and exported packages are written to the container output root (`/out`, configurable via `$A8_OUTPUT_DIR`, typically mapped to host output `<out>/runs/<run_id>`).
   - Even when running with `--rm` (which destroys container filesystem on exit), raw evidence **persists** on the named volume.
   - If an export is interrupted or fails, the offline `recover` subcommand reconciles interrupted task runtime evidence and delivers the durable run package without starting dockerd.
4. **Git Worktree Support via Host Wrappers**:
   - Git worktrees use a `.git` text pointer referencing host paths, which cannot resolve inside a Docker container.
   - The host wrappers (`ops/e2e/Run-E2E.ps1` for Windows, `ops/e2e/run-e2e.sh` for Linux/macOS) perform full-SHA source validation. Explicit portable plans can bridge local worktrees into Git bundles mounted to `/input/bundles`.
   - An ordinary run with repository URLs and full SHAs fetches the selected checkouts directly and creates no source bundles.
5. **No Scenario Retries & Safe Continuation**:
   - A scenario failure or timeout is recorded and later scenarios run after successful cleanup. Any failed scenario keeps the overall verdict `FAILED` and exit code nonzero.
   - Cleanup/collection failures, invalid provenance, unexpected runner exceptions and cancellation halt the suite; remaining tasks are `NOT_RUN` with the stop reason. Failed tasks using shared source snapshots are re-measured before continuation.
   - Before a live scenario starts, an explicit Gradle wrapper distribution download timeout may retry up to three preparation attempts within the original timeout budget. Every attempt is logged; compilation failures and scenario execution are never retried.
6. **No Fabricated Acceptance**:
   - All automated runs record `acceptance_status: NOT_REVIEWED`. The runner has no authority to grant manual reviewer acceptance.
7. **Strict Rejection of `--keep-resources`**:
   - `--keep-resources` is incompatible with `--rm` containers and is rejected with exit code 2 and a message directing users to raw diagnostics in `a8-workspace`.

---

## 3. Persistent Volumes Lifecycle

The runner defines six dedicated Docker named volumes in [compose.yaml](compose.yaml):

| Volume Name | Container Mount | Purpose |
|---|---|---|
| `a8-workspace` | `/workspace` | Stores raw suite runs, logs, exclusive lock, and recovery states. |
| `a8-docker-root` | `/var/lib/docker` | Stores inner dockerd images and container layers. |
| `a8-cargo-registry-cache` | `/root/.cargo/registry` | Caches Cargo registry downloads between runs. |
| `a8-cargo-git-cache` | `/root/.cargo/git` | Caches Cargo Git dependencies between runs. |
| `a8-gradle-cache`| `/root/.gradle` | Caches Gradle wrappers and dependencies between runs. |
| `a8-go-cache`    | `/root/go` | Caches Go modules between runs. |

### Volume Management
- **Preserve Cache & Diagnostics (Default)**: Normal container runs with `--rm` destroy the ephemeral container but preserve all 6 volumes.
- **Purge (Opt-in only)**: Do not use an unscoped `down -v` as routine cleanup.
  Follow the ownership and no-active-run checks in [Stage 6](#stage-6-review-evidence-recovery--cleanup), then target only the confirmed Compose project.

---

## 4. Subcommands

The container entrypoint supports 6 subcommands. For complete end-to-end operational instructions, flag reference, and execution examples, see [ops/e2e/README.md](../e2e/README.md).

| Subcommand | Needs Dockerd | Description |
|---|:---:|---|
| `list` | **No** | Prints registered tasks, profiles, and scenarios catalog without launching daemon or network. |
| `plan` | **No** | Creates deterministic `run.lock.json` freezing SHAs, config hashes, and limits without starting dockerd. |
| `run` | **Yes** | Builds target artifacts and executes the plan sequentially, validates evidence against lock invariants, exports package to `/out/<run_id>`, cleanly stops dockerd. |
| `rerun` | **Yes** | Replays an existing plan package (`run --from`), rebuilding targets and executing without planning anew. |
| `report` | **No** | Re-grades an existing run package offline and regenerates reports. |
| `recover` | **No** | Recovers staged evidence from `a8-workspace` persistent volume and delivers the package into output. |

> [!NOTE]
> Runner image build is performed via host wrapper scripts (`./ops/e2e/build-runner.sh` / `.\ops\e2e\Build-Runner.ps1`), while CosmWasm contract and chain targets build inside `run`/`rerun`. There is no standalone `build` subcommand in the container CLI.

---

## 5. Reviewer Runbook: Step-by-Step

All live runs, builds, and containers are intentionally reserved for the reviewer in an isolated environment. Follow these stages sequentially:

### Stage 1: Static & Config Checks
1. Verify Compose configuration parses without errors (defaults need no `.env` file; set paths only if needed):
   ```bash
   docker compose -f ops/a8/compose.yaml config
   ```
2. Run the offline tests in Stage 2; `-B` avoids writing bytecode caches into the checkout.

---

### Stage 2: Offline Unit Tests
Run the offline unit tests covering CLI, supervisor lifecycle, recovery, worktree bundling, and the E2E explicit-SHA runner:
```bash
python3 -B -m unittest discover -s ops/a8/tests -v
python3 -B -m unittest discover -s scripts/tests -v
python3 -B -m unittest discover -s ops/a8/integration_tests -v
```
Every test must pass without errors, external network access, Docker calls, or
live chain access. A test may bind an ephemeral HTTP server to `127.0.0.1` to
exercise an HTTP parser; that loopback-only traffic is local test I/O, not an
external network dependency. The third suite uses temporary local Git
repositories and is separate from unit-test discovery.

> [!NOTE]
> `.github/workflows/ci.yml` configures all three suites under Python 3.11
> with zero tolerance for failure when repository Actions are enabled. Confirm
> actual checks for the commit under review. See [`AGENTS.md`](../../AGENTS.md).


---

### Stage 3: Image Build
Build the Debian Bookworm runner image. The base tag can change between builds;
each run records and checks the exact resulting runner image ID:
```bash
docker compose -f ops/a8/compose.yaml build e2e-runner
```

---

### Stage 4: Daemon & Mount Smoke Checks (Zero-Daemon Calls)
Verify CLI argument handling, catalog resolution, and execution planning without starting the inner Docker daemon:

- **List Catalog**:
  ```bash
  docker compose -f ops/a8/compose.yaml run --rm e2e-runner list
  # Or with host wrapper:
  ./ops/e2e/run-e2e.sh list     # Linux / macOS
  ./ops/e2e/Run-E2E.ps1 list    # Windows
  ```
- **Plan Execution**:
  ```bash
  ./ops/e2e/run-e2e.sh plan \
    --gonka-repo https://github.com/gonka-ai/gonka \
    --gonka-sha <GONKA_FULL_SHA> \
    --contracts-path ../forward-contracts \
    --contracts-sha <CONTRACTS_FULL_SHA> \
    --profile smoke \
    --output ./out/e2e-plan
  ```
  Replace both placeholders with full 40-hex commit SHAs. The host wrapper
  resolves the runner image ID and source mounts before calling Compose. For
  direct Compose invocation, also set `E2E_RUNNER_IMAGE_ID` as described in
  `.env.example` and pass the same source and output arguments.

---

### Stage 5: Single Live `lock-exact-e` Run
Execute the single live epoch probe scenario (`lock-exact-e`):

- **Via Host Wrapper (recommended, handles worktrees & clones)**:
  ```bash
  ./ops/e2e/run-e2e.sh run \
    --gonka-repo https://github.com/gonka-ai/gonka \
    --gonka-sha <GONKA_FULL_SHA> \
    --contracts-path ../forward-contracts \
    --contracts-sha <CONTRACTS_FULL_SHA> \
    --profile smoke \
    --output ./out/e2e
  ```
  On Windows, use `./ops/e2e/Run-E2E.ps1` with the same arguments (PowerShell
  line continuations use backticks rather than backslashes). Replace both SHA
  placeholders with full 40-hex commit IDs; the wrapper binds the local
  contracts checkout read-only and resolves the runner image ID.
- **Or via Direct Docker Compose**:
  ```bash
  docker compose -f ops/a8/compose.yaml run --rm e2e-runner run \
    --gonka-repo https://github.com/gonka-ai/gonka \
    --gonka-sha <GONKA_FULL_SHA> \
    --contracts-path /input/contracts \
    --contracts-sha <CONTRACTS_FULL_SHA> \
    --profile smoke \
    --output /out/e2e
  ```
  Set `E2E_RUNNER_IMAGE_ID` to the host image ID as described in `.env.example`.
  Compose mounts the repository at `/input/contracts` and the host output
  directory at `/out`; use the container paths in this direct command.

---

### Stage 6: Review Evidence, Recovery & Cleanup

1. **Verify the E2E export in `./out/e2e/<run-id>/`** (for the Stage 5 `--output ./out/e2e` example):
   ```text
   out/e2e/<run-id>/
   ├── run.lock.json, build-manifest.json, execution-manifest.json
   ├── result.json, e2e-run-result.json    # Whole-run verdict, not suite acceptance
   └── suite/<run-id>/
       ├── suite-plan.json, suite-result.json
       ├── summary.md, coverage.json, artifact-index.json
       └── runs/<task-run-id>/
           ├── identity.json, result.json, launcher.log
           ├── evidence/<task-run-id>/live-context.json
           └── cleanup-evidence/ownership.json, completed.json
   ```

   Check the top-level verdict and its findings first. The nested suite is
   supporting task evidence; its `PASSED` status alone does not pass the E2E run.
   The task run id is repeated inside `evidence/` because the harness appends it
   to the evidence directory supplied by the runner.

2. **Test Offline Recovery**:
   Simulate export recovery from the persistent `a8-workspace` volume without starting `dockerd`:
   ```bash
   docker compose -f ops/a8/compose.yaml run --rm e2e-runner recover --run <run-id> --output /out/e2e
   ```

3. **Re-generate Offline Report**:
   ```bash
   docker compose -f ops/a8/compose.yaml run --rm e2e-runner report --run /out/e2e/<run-id>
   ```

4. **Persistent-volume cleanup is opt-in and ownership-scoped**:
   Keep the named volumes by default; they contain durable run evidence and may
   be shared with another invocation. Before removing anything, identify the
   exact Compose project used for this run and inspect its containers and
   volumes (`docker compose -p <project> -f ops/a8/compose.yaml ps -a` and
   `docker volume inspect <volume>`). Confirm that the Compose project label
   matches, no process/run is using those resources, and the evidence has been
   exported or is intentionally disposable. Only then may
   `docker compose -p <same-project> -f ops/a8/compose.yaml down -v` remove that
   project's resources. Do not run an unscoped `down -v` as routine cleanup.

---

## 6. The E2E run package: durable stage and export

Everything in this section describes a run started through the **E2E** entry
point (`ops/e2e/run-e2e.sh`, `ops/e2e/Run-E2E.ps1`, `ops/a8/e2e/cli.py`, or Compose service `e2e-runner`).

### 6.1 Where a run is written

`execute_plan` (`ops/a8/e2e/executor.py`) writes everything it produces into a
runner-owned **staging directory on the persistent workspace volume first**.
The host output directory is an **export** of that state, taken on success and
on handled failure alike. The stage path comes from `run_stage_dir`
(`ops/a8/e2e/runpackage.py`).

**Durable stage — full run**

```text
<workspace>/image-provenance.json          # image ledger, shared by all runs
<workspace>/<run-id>/
├── run/                                   # run_stage_dir(): the run package
│   ├── run.json                           # concise run inputs summary (e2e/run-inputs/1)
│   ├── status.json                        # execution stage & task progress (e2e/run-status/1)
│   ├── run.lock.json                      # the original lock, byte-identical
│   ├── build-manifest.json
│   ├── execution-manifest.json
│   ├── build/                             # BUILD_OUTPUT_DIRNAME (A9 release etc.)
│   ├── build-logs/                        # BUILD_LOG_DIRNAME, one log per step
│   └── suite/
│       └── <run-id>/                      # exported suite (orchestrator appends the id)
│           ├── suite-plan.json
│           ├── suite-result.json
│           ├── e2e-context.json
│           ├── summary.md, coverage.json, artifact-index.json, events.jsonl
│           └── runs/<task-run-id>/       # identity.json, result.json, launcher.log
│               └── evidence/<task-run-id>/live-context.json
├── suite/                                 # suite_workspace_root()
│   └── runtime/<task-run-id>/             # per-task runtime snapshots (direct_immutable mode)
├── src/, scratch/, git-helper/            # restored worktrees and Git helpers
```

**Host export — full run**

```text
<output>/<run-id>/
├── run.json
├── status.json
├── result.json                            # the verdict, written alongside e2e-run-result.json
├── run.lock.json
├── build-manifest.json
├── execution-manifest.json
├── build/
├── build-logs/
├── suite/<run-id>/…
└── e2e-run-result.json                    # the verdict, written *after* the export
```

**Durable stage and export — partial / failed / cancelled run**

```text
<workspace>/<run-id>/run/
├── run.json, status.json                  # written before any build; status updated per stage
├── run.lock.json                          # always present: written before any build
├── build-manifest.json                    # status FAILED | PARTIAL | IN_PROGRESS,
│                                          # .failures[], .cancellation{} when stopped
├── execution-manifest.json                # build_manifest_sha256 missing if the
│                                          # process died before the sealing tail
├── build/, build-logs/                    # whatever the build got as far as
└── suite/                                 # absent when the suite never started
```

The export has the same shape; the missing pieces are simply missing. A run
killed outright by the host never reaches the export at all, and only the stage
exists — which is exactly what `recover` is for.

> [!IMPORTANT]
> A run directory that is missing the lock, the build manifest or the execution
> manifest is an **incomplete E2E run**. It never degrades into "just a suite".

### 6.2 When the durable write happens

In order, all inside `execute_plan`:

| # | Moment | What is written |
|---|---|---|
| 1 | After the cheap preconditions pass (runner image, runner hashes, adapters), **before** any source is fetched | `stage_dir` is created, the lock is copied in (`copy_lock_into_run`), and `run.json`, `status.json`, and both manifests are written immediately, so a run killed mid-build still says which plan it was executing |
| 2 | After the build recipes finish | `build-manifest.json` with images, binaries, Wasm, tools, patch manifest, `source_fingerprint`, `deployment`; a non-`COMPLETE` status is written **before** `BuildProvenanceError` is raised |
| 3 | Immediately after the suite returns (or fails/cancels) | `after_execution` source immutability is measured and written to `build-manifest.json` and `execution-manifest.json`, along with `suite_export_relpath` and `suite_workspace_dir` |
| 4 | After post-run provenance | `build-manifest.json` again, with `observed_runtime` (live contexts, containers, deployment, `missing_evidence`, `policy`, `selection_disagreements`) |
| 5 | On any exception | `record_failure`, `cancellation` if a signal was seen, `completed_at_utc`, then `build-manifest.json`; a note is appended to the execution manifest |
| 6 | Always, in the tail | `execution-manifest.json` sealed with `build_manifest_sha256` and `artifact_index_relpath`; `status.json` shows export `IN_PROGRESS` until delivery succeeds or fails, then records the delivery result; the verdict is written to `result.json` and `e2e-run-result.json` |

### 6.3 What is exported, and what is not

`export_run_package` (`ops/a8/e2e/runpackage.py`) copies the whole stage
recursively, with three rules:

- a file already at the destination with **identical bytes** is left alone and
  reported as `identical` — re-running `recover` is safe;
- a file already there with **different bytes** is a conflict: nothing is
  overwritten and `ExportConflict` is raised, pointing at a new `--output`;
- a symlink in the stage, or a symlink at the destination, is refused
  (`PathSafetyError`). A symlink is not evidence.

**Deliberately not bulk-exported:** `e2e-run-result.json` (`EXPORT_EXCLUDED_NAMES`),
top-level `result.json`, and mutable top-level `status.json`, which is published
separately during execution and after delivery. Task-level
`suite/<run-id>/runs/<task-run-id>/result.json` files *are* exported. The top-level verdict is *derived* from the package and is
re-derived wherever the package is read, so its bytes legitimately differ
between two locations. Copying it would make a correct re-grade look like a
modified file and would block the recovery it is supposed to support.

`grade_run_package` writes the verdict into the directory it graded: the export
directory when the export succeeded, the stage directory when it did not.

`SuiteOrchestrator.run_suite` writes the suite directly into
`<stage_dir>/suite/<run-id>/`, while keeping per-task runtime snapshots under
`<workspace>/<run-id>/suite/runtime/` outside `run_stage_dir` (referencing the
verified immutable checkouts directly when `use_direct_sources=True`).

### 6.4 What the reader validates

`LoadedRunPackage.load` + `evaluate_run` (`ops/a8/e2e/runpackage.py`,
`ops/a8/e2e/outcome.py`), used identically by `run`, `report` and `recover`:

| Check | Finding when it fails |
|---|---|
| The three documents are present, and each one's SHA-256 is recorded | `INCOMPLETE_LOCK_MISSING`, `INCOMPLETE_BUILD_MANIFEST_MISSING`, `INCOMPLETE_EXECUTION_MANIFEST_MISSING` |
| The lock envelope still hashes to its recorded `lock_sha256` | the lock loader's own code, reported as an unreadable/tampered lock |
| Each manifest's schema is one this runner supports | `SCHEMA_UNSUPPORTED`, `DOCUMENT_UNREADABLE` |
| Build and execution manifests belong to *this* lock | `BUILD_MANIFEST_FOREIGN`, `EXECUTION_MANIFEST_FOREIGN` |
| Both manifests describe the same run | `RUN_ID_DISAGREEMENT` |
| The build manifest hashes to the value the execution manifest sealed | `BUILD_MANIFEST_MODIFIED`, or `INCOMPLETE_BUILD_MANIFEST_UNSEALED` when never sealed |
| The recorded suite path stays inside the package (`resolve_within`) | `SUITE_PATH_UNSAFE` |
| A suite exists and its `overall_status` is `PASSED` | `INCOMPLETE_SUITE_MISSING`, `INCOMPLETE_SUITE_RESULT_MISSING`, `SUITE_NOT_PASSED` |
| The build manifest is `COMPLETE` and not cancelled | `BUILD_NOT_COMPLETE`, `RUN_CANCELLED` |
| Every selected task left the evidence its proof level owes | `TASK_EVIDENCE_MISSING`, `SELECTION_UNREADABLE`, `SELECTION_DISAGREEMENT`, `PROOF_LEVEL_DISAGREEMENT` |
| Deployment hashes replayed offline against the build manifest | `DEPLOYED_ARTEFACT_MISMATCH`, `DEPLOYED_ARTEFACT_NOT_STATED` |
| Recorded runtime comparisons re-derived from their own values | `RUNTIME_MISMATCH`, `RUNTIME_COMPARISON_INCONSISTENT` |
| Containers that ran were images this build produced | `RUNNING_IMAGE_MISMATCH` |
| Evidence the run itself recorded as missing while online | `TASK_EVIDENCE_MISSING_AT_RUNTIME` |
| Live contexts belonging to no selected task | `UNCLAIMED_LIVE_CONTEXT` (severity `NOTE`, not blocking) |

`report` pointed at a nested suite resolves **upwards** to the run package
(`_resolve_run_package` in `ops/a8/e2e/cli.py`), so a suite's local success can
never be presented as the run's verdict. A target that lacks a durable E2E run
package (`run.lock.json`, `build-manifest.json`, `execution-manifest.json`) is
rejected with `UsageError`.

`recover` (`cmd_recover`) reconciles any interrupted task runtime evidence from
`<workspace>/<run-id>/suite/runtime` into `<stage_dir>/suite/<run-id>` via
`reconcile_suite_runtime_evidence` (`ops/a8/orchestrator.py`) and exports the
durable run package from `<workspace>/<run-id>/run` into `<output>/<run-id>/`
via `DeliveryService.deliver_recovery`. A target lacking a durable run package
is rejected and never degrades into a bare-suite recovery.

---

## 7. Final states of an E2E run

`evaluate_run` is the only place a run is graded; `_status_from_findings`
(`ops/a8/e2e/outcome.py`) turns findings into one of exactly four answers.
Worst outcome wins, with cancellation and incompleteness kept distinct from
failure.

| State | Exit code | Produced when | What it means |
|---|---|---|---|
| `PASSED` | `0` | No finding of severity `BLOCKING`. | Everything that had to happen happened and every **applicable** check passed. `NOTE` findings do not block. |
| `FAILED` | `1` | At least one blocking finding remains after excluding `RUN_CANCELLED`, `INCOMPLETE_*`, and recognized non-verdict codes (`DELIVERY_MANIFEST_MISSING`, `HISTORICAL_PREPARED_BUILD`). | A blocking check failed or evidence conflicts; it is not necessarily a finding that the contract source itself is wrong. |
| `INCOMPLETE` | `1` | Blocking findings exist, but after excluding `INCOMPLETE_*` and recognized non-verdict codes (`DELIVERY_MANIFEST_MISSING`, `HISTORICAL_PREPARED_BUILD`) no failure finding remains. | The evidence is insufficient for a complete run verdict (for example, missing/unsealed documents, no suite, an interrupted run, or a recognized non-verdict condition). Not a statement that the sources are correct or incorrect. |
| `CANCELLED` | `143`, or `cancellation_exit_code` from the build manifest (`130` for SIGINT) | `RUN_CANCELLED` is present, i.e. `build-manifest.json` records `cancellation.requested`. | The operator stopped it. Also not a statement about the sources; it outranks any incompleteness it caused. |

What distinguishes them in one line: **`FAILED` = a blocking check failed or
the evidence conflicts; `INCOMPLETE` = the evidence was insufficient for a complete run verdict;
`CANCELLED` = execution was stopped before it could finish.** Some checks may
have completed before an incomplete or cancelled run, but they do not turn that
run into a complete verdict.

### Process exit codes

The verdict's `exit_code` is the process exit code of `report`, and of `run`
when the run completed without raising:

| Command | Exit code source |
|---|---|
| `run` / `rerun`, normal completion | `RunOutcome.exit_code` returned by `execute_plan` |
| `run` / `rerun`, exception | the verdict is still written, then the exception is re-raised; `main` prints it and returns the error's `exit_code` (`1`, or `2` for usage errors) |
| `run` / `rerun`, signal | `130` (SIGINT) / `143` (SIGTERM), forced by the handler in `_with_inner_dockerd` |
| `report` | `RunOutcome.exit_code` (`2` if the target is not a durable E2E run package; `1` on grading error) |
| `recover` | `ExportConflict` → its `exit_code`; missing durable run package → `1`; otherwise `RunOutcome.exit_code` |

`acceptance_status` stays `NOT_REVIEWED` in every one of these states.

---

## 8. What each proof level owes

Evidence duties are keyed on the **proof level of each selected task**, read
from `lock.selection.proof_levels` (and cross-checked against the suite's own
`suite-plan.json`) by `ops/a8/e2e/evidence.py`. They are never derived from what
the build happened to produce: a boundary-only run starts no chain and owes no
live context, and a native task's duty is not discharged by another task's
evidence.

| Proof level | Mandatory artifacts (catalog `expected_artifacts`) | Producer | Consumer | When absent |
|---|---|---|---|---|
| `NATIVE` | `live-context.json`, `junit/TEST-MarketplaceContractAcceptanceTests.xml`, `testermint.log` | `scripts/a8_acceptance.py run-live` driven by `NativeTaskAdapter`; JUnit copied by `NativeTaskAdapter._collect_junit` | `evaluate_task_evidence` → `verify_live_context` (`ops/a8/verifier.py`); `locate_task_evidence` and `_verify_post_run_provenance`; offline again in `LoadedRunPackage` | Task is `FAILED` / `INCOMPLETE` at suite level; at run level the missing live context is `TASK_EVIDENCE_MISSING` (+ `TASK_EVIDENCE_MISSING_AT_RUNTIME` if the run noticed it too) → run `FAILED` |
| `NATIVE` deployment evidence: `source.a9_manifest_sha256`, `source.a9_contract_sha256.deal`, `source.a9_contract_sha256.factory` | required **only when this build produced production Wasm** (`build_produced_production_wasm`, from `build-manifest.deployment.production_wasm`) | the harness, from the `--manifest` it was handed | `verify_deployed_artifacts` (`ops/a8/e2e/deployment.py`), called online by `_verify_post_run_provenance` and offline by `LoadedRunPackage.provenance_findings` | silent file → `INCOMPLETE` → `DEPLOYED_ARTEFACT_NOT_STATED`; wrong hash → `MISMATCHED` → `BuildProvenanceError` online, `DEPLOYED_ARTEFACT_MISMATCH` offline |
| `GO_BOUNDARY` | `build.log`, `raw/go-test.json`, `raw/exit-code`, `report.json` | `scripts/run_a8_go_boundary.py` via `BoundaryTaskAdapter._run_go_boundary`; copied by `ALLOWED_ARTIFACT_PATTERNS` in `ops/a8/collector.py` | `verify_go_boundary_report` | Missing artifact → `EvidenceStatus.INCOMPLETE`; an unreadable/failing report → `ExecutionStatus.FAILED`. No live context and no deployment check is applied |
| `WASM_ABI` | `abi.json`, `a8_query_boundary.wasm` (`wasm-build.log`, `node-probe.log` are collected as diagnostics) | `cargo build -p a8-query-boundary` then `scripts/test_wasm_query_boundary.mjs`, in `BoundaryTaskAdapter._run_wasm_abi_boundary` | `verify_wasm_abi_report` | same as above |
| `CONTRACT_TEST` | `ct-network-unconfirmed.log` / `ct-claim-expiry.log` | `cargo test` in `BoundaryTaskAdapter._run_ct_*` | `verify_cargo_test_log` (zero tests executed is a failure) | same as above |

Two details worth knowing:

- A non-native task that *does* leave a `live-context.json` in its own directory
  is recorded as `SATISFIED` rather than rejected; a live context that belongs to
  no selected task at all is reported by `unclaimed_live_contexts`.

## 9. Evidence model: reading `live-context.json`

The runner grades exactly one evidence model: **`a8.evidence/e2e-immutable-source/2`**
(`EVIDENCE_MODEL_IMMUTABLE`). The earlier identifiers are **historical**: they
stay recognisable so old packages can be read and reported by their format
version, but they are never accepted as proof of unmodified sources.

| Property | Immutable-source model (`a8.evidence/e2e-immutable-source/2`) |
|---|---|
| Producer | `ops/a8/e2e` builds the images from the unmodified selected commit (explicit `docker build` calls, outputs outside the snapshots), then runs `scripts/a8_acceptance.py run-live` with the external harness from `ops/a8/harness/` |
| Declaration | `E2ERunContext.evidence_model` -> `--evidence-model` -> `A8_EVIDENCE_MODEL` -> `source.evidence_model` |
| Source identity | `source.gonka_sha` / `gonka_tree_sha` and `marketplace_commit_sha` / `marketplace_tree_sha` — the selected commits themselves; there is no prepared commit |
| Running binary report | Strictly the selected commit — `source.runtime` must report `gonka_sha` (`requires_observed_selected_commit`) |
| Immutability | `source.source_immutability_verdict` must be `UNCHANGED` and `source.source_immutability_sha256` must match the collected `source-immutability.json`; `INCOMPLETE` or a missing document is never a pass |
| Test code / network | `source.external_harness_inputs_sha256`, `source.network_manifest_sha256` bind the evidence to the harness inputs and the network work directory actually used |
| Forbidden fields | `gonka_prepared_sha`, `gonka_overlay_manifest_sha256`, `gonka_test_harness_sha` |
| `source.runtime` section | Mandatory; missing or empty runtime section causes verification failure |
| Undeclared / historical model in a new package | Rejected with `EVIDENCE_MODEL_DOWNGRADE` |
| Unknown model | Rejected with `EVIDENCE_MODEL_UNKNOWN` |

| Historical identifier | Meaning | How this runner treats it |
|---|---|---|
| `a8.evidence/e2e-prepared-build/1` | selected commit + overlay committed as a "prepared" commit | read by `report`/`recover` under its original schema, labelled `historical-prepared-build`; never the new classification |
| `a8.evidence/legacy-overlay/1` | overlay applied to a pre-existing chain | readable only; never proof |

**Verification guarantees:**
- `resolve_evidence_model` requires `EVIDENCE_MODEL_IMMUTABLE` for every new package.
- `run`/`rerun` refuse plans older than `e2e/run-lock/2` (`PLAN_SCHEMA_SUPERSEDED`).
- Snapshots are measured by `ops/a8/source_snapshot.py` before the build, after
  the build and after execution; the harness measures them again around the
  Testermint run and writes `source-immutability.json`.
- Running containers are compared with the image IDs recorded in the build
  manifest and deployed contracts with the Wasm hashes of the A9 manifest; one
  textual SHA in metadata is not enough.

---

## 10. Profiles & Task Catalog

### Profiles

- **`smoke`**: Runs strictly `lock-exact-e` (single focused epoch lock probe).
- **`native`**: All 19 Kotlin Testermint tasks in fixed sequential order.
- **`boundary`**: 5 isolated tasks without Testermint:
  1. `go-boundary`: Go classification & JSON roundtrip in pinned Docker container.
  2. `wasm-abi-boundary`: CosmWasm ExternalQuerier ABI query_chain probe (9 cases).
  3. `ct-network-unconfirmed`: Contract test decision policy for emergency refund.
  4. `ct-claim-expiry`: Contract test decision policy for claim expiry locked accounting.
  5. `ct-package-c-policy`: Contract-policy and cw-multi-test replacements for the retired query-fault scenarios (71 cases).
- **`all`**: Runs boundary (5 tasks), then native (19 tasks) — strictly sequential.

### 19 Native Testermint Scenarios
1. `funded-claim`: Single funded claim settlement and release happy-path.
2. `network-unconfirmed`: Absent summary emergency refund at emergency deadline.
3. `claim-expiry-positive`: Positive unclaimed summary refund at claim expiry.
4. `claim-expiry-zero`: Zero unclaimed summary refund at claim expiry.
5. `terminal-release-repeat`: A terminal repeat is rejected with `NothingToRelease`; state and balances remain unchanged.
6. `b3-foreign-native`: Release preserves foreign native denom alongside GNK.
7. `late-donation-after-completed`: Late liquid donations use cumulative GNK rounding.
8. `lock-exact-e`: Lower E boundary lock probe.
9. `lock-e-plus-4`: Upper valid lock window boundary lock probe.
10. `lock-e-plus-5`: First expired lock window block rejection probe.
11. `package-a-r1-r2`: Package A R1 refund boundary and R2 streamvesting gift release.
12. `package-b-r6-1`: R6.1 CW20 send rejections and single settlement.
13. `package-b-r7-1`: R7.1 Bank send rejection under governance restriction and retry.
14. `funded-routing-refunds`: Funded routing mismatch/missing refunds and Factory isolation.
15. `unfunded-lock-boundaries`: Buyer-absent Lock behavior at E+4 and E+5.
16. `funded-gas-sweep`: Claimed positive reward refund gas sweep.
17. `no-buyer-claim-expiry`: Positive unclaimed summary expiry without a Buyer payout.
18. `no-sale-vesting-lifecycle`: No-sale donations, foreign CW20 and vesting lifecycle.
19. `emergency-host-only-recovery`: Terminal emergency refund, Host-only Bank rollback and retry.
The retired `package-c-query-faults` selector is deliberately absent. Boundary
task `ct-package-c-policy` covers 71 contract-policy/cw-multi-test cases and
`emergency-host-only-recovery` owns the two reachable native R7.2 cases. The
versioned 73-row mapping and proof limits are in
[the replacement coverage matrix](../../docs/reviews/pr27-c-replacement-coverage.md).

---

## 11. Testing & Verification Tiers

The acceptance system is structured into three distinct operational tiers:

1. **Deterministic Planning**:
   - Subcommands: `plan`, `list` (and `ops/a8/e2e/planner.py`).
   - Resolves sources, inspects the task catalog, applies limits, and computes hashes to produce an immutable `run.lock.json`.
   - Requires **no inner Docker daemon or target-code execution**. Remote source origins may require network access to fetch Git objects.

2. **Offline Unit & Regression Tests**:
   - Test suites: `ops/a8/tests` and `scripts/tests`.
   - Commands:
     ```bash
     python3 -B -m unittest discover -s ops/a8/tests -v
     python3 -B -m unittest discover -s scripts/tests -v
     ```
   - Enforces fail-closed rules, parser logic, atomic filesystem operations, delivery ledgers, verdict recalculation, and provenance invariants against synthetic fixtures.
   - Configured in CI under Python 3.11 in the `python-offline-runner` job with zero tolerance for failure when Actions are enabled; check the exact commit's job result.

3. **Live Containerized & Chain E2E**:
   - Subcommands: `run` (and `ops/e2e/run-e2e.sh`).
   - Spawns private inner Docker-in-Docker daemon, spins up Gonka testnet nodes and CosmWasm contracts, executes live transactions, and collects raw telemetry.
   - Must be executed and retained for the exact runner and product commits before claiming live verification.

> [!IMPORTANT]
> **Этот раздел описывает границы проверок, но не подтверждает, что проверки
> выполнены для конкретного коммита.** Перед принятием PR проверяйте CI и
> приложенные к выбранному коммиту E2E-доказательства.
>
> Offline tests cover runner policy, parsers, state handling and synthetic
> fixtures; loopback-only HTTP tests do not contact external services. They do
> not substitute for image builds or live Testermint runs. Use the steps in
> [Section 5](#5-reviewer-runbook-step-by-step) and inspect the resulting
> evidence package before treating a live run as verified.
