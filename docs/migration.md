# Migration Reference

This document maps historical paths, scenario IDs, environment variables and
identifiers from the initial extraction commit
(`d637eea5432506d60c90c1d8436c67b93802d829` in
[`EXTRACTION.json`](../EXTRACTION.json)) and the pre-refactoring baseline
(`8079c0c274f5252800ee16b9f4fb0148bfcae2fe`) to the current repository layout.
It also states, in one place, the removal schedule for every compatibility
alias the runner still accepts and the identifiers that deliberately keep
their historical spelling.

Every statement below describes what the code does today; where a rule is
enforced, the enforcing symbol is named so it can be checked.

---

## 1. Directory and File Path Mapping

| Old Path (at extraction / baseline) | Current Path |
|---|---|
| `ops/a8/__init__.py`, `adapters.py`, `catalog.py`, `collector.py`, `evidence_model.py`, `lock.py`, `models.py`, `orchestrator.py`, `reporter.py`, `runtime.py`, `source_snapshot.py`, `supervisor.py`, `verifier.py` | `forward_e2e/suite/` (same module names) |
| `ops/a8/e2e/*` | `forward_e2e/execution/*` (same module names) |
| `ops/a8/Dockerfile` | `ops/runner/Dockerfile` |
| `ops/a8/compose.yaml` | `ops/runner/compose.yaml` |
| `ops/a8/e2e-entrypoint.sh` | `ops/runner/entrypoint.sh` |
| `ops/a8/.env.example` | `ops/runner/.env.example` |
| `ops/a8/RUNNER_VERSION` | `ops/runner/RUNNER_VERSION` (content changed, see [§5](#5-handling-pre-refactoring-plans-and-run-packages)) |
| `ops/a8/harness/testermint/gradle/a8-out-of-tree.init.gradle.kts` | `harness/testermint/gradle/out-of-tree.init.gradle.kts` |
| `ops/a8/harness/testermint/src/test/kotlin/A8ApiContainerControl.kt` | `harness/testermint/src/test/kotlin/ApiContainerControl.kt` |
| `ops/a8/harness/testermint/src/test/kotlin/A8PackageBBankFaultPlan.kt`, `A8PackageBBankFaultPlanTests.kt` | `harness/testermint/src/test/kotlin/BankSendFaultPlan.kt`, `BankSendFaultPlanTests.kt` |
| `ops/a8/harness/testermint/src/test/kotlin/A8UpstreamTestSupport.kt` | `harness/testermint/src/test/kotlin/UpstreamTestSupport.kt` |
| `ops/a8/harness/testermint/` (`build.gradle.kts`, `settings.gradle.kts`, `required-upstream-api.json`, `MarketplaceContractAcceptanceTests.kt`, `MarketplaceHarnessProcess.kt`, `MarketplaceHarnessProcessTests.kt`) | `harness/testermint/` (file names unchanged) |
| `ops/a8/harness/network/a8-ownership.yml` | `harness/network/ownership.yml` |
| `ops/a8/harness/network/a8-nats.yml` | `harness/network/nats.yml` |
| `ops/a8/harness/network/a8-b3-genesis.yml` | `harness/network/foreign-native-genesis.yml` |
| `ops/a8/harness/network/genesis/a8-genesis-provision.sh` | `harness/network/genesis/foreign-native-genesis-provision.sh` |
| `ops/a8/harness/go-boundary/a8faults/` (`plan.go`, `plan_test.go`) | `harness/go_boundary/query_faults/` |
| `ops/a8/harness/wasm-probe/p0-probe/` | `harness/wasm_query_allowlist/` (the compiled artefact keeps its name, `artifacts/p0_probe.wasm`) |
| `ops/a8/harness/container_control.py` | `harness/container_control.py` |
| `scripts/a8_acceptance.py` | `scripts/acceptance_harness.py` |
| `scripts/a8_external_harness.py` | `scripts/external_harness.py` |
| `scripts/run_a8_go_boundary.py` | `scripts/run_go_boundary.py` |
| `scripts/test_wasm_query_boundary.mjs` | `scripts/test_wasm_query_boundary.mjs` (unchanged name; it was never prefixed) |
| `scripts/a9_release.py` | `vendor/contract_release/release.py` (bytes unchanged; provenance in [`EXTRACTION.json`](../EXTRACTION.json)) |
| `ops/a8/tests/` | `tests/unit/runner/` |
| `ops/a8/tests/fixtures/catalog-ee14240.json` | Removed: no test read it at the baseline or after. Superseded by `tests/fixtures/compatibility/catalog-baseline-8079c0c.json`, a frozen dump of the baseline catalog's budget fields that `tests/unit/runner/test_catalog_baseline_equivalence.py` compares the live catalog against. |
| `scripts/tests/` | `tests/unit/harness/` |
| `ops/a8/integration_tests/` | `tests/integration/local_sources/` |
| `docs/reviews/evidence/*` (recorded development receipts) | Deleted. Replaced by deterministic synthetic fixtures in `tests/fixtures/evidence/`, generated or checked by `tests/unit/runner/support/synthetic_evidence.py` and pinned by `tests/unit/runner/test_synthetic_evidence.py`. The per-fixture replacement map is in [§7](#7-fixture-replacement-map). |
| `ops/a8/README.md`, `ops/a8/coverage-map.md`, `ops/e2e/RUNBOOK-immutable-sources.md` | Replaced by structured docs in `docs/` (`architecture.md`, `operations.md`, `evidence.md`, `coverage.md`, `validation.md`, `development.md`, `migration.md`) |

### Python CLI entrypoint

- **Old:** `python3 -m ops.a8.e2e.cli`
- **Current:** `python3 -m forward_e2e.execution.cli`

The subcommands are unchanged: `list`, `plan`, `run`, `rerun`, `report`,
`recover` (`SUBCOMMANDS` in
[`forward_e2e/execution/cli.py`](../forward_e2e/execution/cli.py)). The host
wrappers `ops/e2e/run-e2e.sh` and `ops/e2e/Run-E2E.ps1` keep their names and
forward the same subcommands.

### Live-context `command.entrypoint`

`live-context.json` records the harness invocation under `command.entrypoint`.
Documents written by the current runner carry
`python scripts/acceptance_harness.py run-live`; historical packages carry the
old script name. No verifier compares that string against a fixed value, so
both read identically.

---

## 2. Renamed Scenario IDs, Legacy Aliases and Their Removal Schedule

Eight scenario identifiers were renamed to descriptive domain names. Both the
catalog (`--scenario`, resolved by `resolve_e2e_selection` in
[`forward_e2e/suite/catalog.py`](../forward_e2e/suite/catalog.py)) and the live
harness (`scripts/acceptance_harness.py run-live --scenario`) accept the legacy
aliases and normalise them to the canonical ID:

| Canonical `task_id` | Accepted Legacy Alias | Proof Level |
|---|---|---|
| `go-query-error-classification` | `go-boundary` | `GO_BOUNDARY` |
| `contract-network-unconfirmed-policy` | `ct-network-unconfirmed` | `CONTRACT_TEST` |
| `contract-claim-expiry-policy` | `ct-claim-expiry` | `CONTRACT_TEST` |
| `contract-query-fault-policy` | `ct-package-c-policy` | `CONTRACT_TEST` |
| `foreign-native-preservation` | `b3-foreign-native` | `NATIVE` |
| `refund-boundary-and-vesting-addition` | `package-a-r1-r2` | `NATIVE` |
| `usdt-withdrawal-failure-recovery` | `package-b-r6-1` | `NATIVE` |
| `native-release-rollback-retry` | `package-b-r7-1` | `NATIVE` |

`wasm-abi-boundary` and the remaining fifteen tasks were not renamed. The task
count (24: 19 native, 5 boundary), catalog order, profile membership and every
frozen task field (`ordinal`, `proof_level`, `evidence_scopes`,
`timeout_minutes`, `stage_timeout_seconds`, `gradle_timeout_minutes`,
`expected_checkpoints`, `coverage_ids`, `limitations`, `expected_artifacts`,
`exact_test_method`) are identical to the baseline modulo the recorded
`scenario_renames` / `test_method_renames` maps; this is pinned by
`tests/unit/runner/test_catalog_baseline_equivalence.py` against
`tests/fixtures/compatibility/catalog-baseline-8079c0c.json`.

Additionally, in `scripts/acceptance_harness.py`:

- The manual allowlist-probe subcommand is `wasm-query-allowlist`; `p0-probe`
  is accepted as a deprecated alias of it.

### Where an alias is accepted, and where it is not

| Entry point | Behaviour |
|---|---|
| `plan` / `run --scenario <alias>` | Accepted. `legacy_aliases_in` makes the planner print one `notice: scenario alias ... is deprecated` line per alias; the lock records the canonical ID only (`forward_e2e/execution/planner.py`). |
| `acceptance_harness.py run-live --scenario <alias>` | Accepted with the same notice; `live-context.json` records the canonical selector. |
| `acceptance_harness.py p0-probe` | Accepted with a notice; behaves exactly like `wasm-query-allowlist`. |
| `suite-plan.json`, `run.lock.json`, reports written by this runner | Never contain an alias. |
| Reading an older package whose `suite-plan.json` names a pre-rename task | Translated once through `canonical_task_id` (`forward_e2e/suite/catalog.py`); the package is graded under the canonical task. |

### Removal schedule

The runner version is the clock for compatibility decisions, because it is
the only version the lock records (`lock.runner.runner_version`).

| Compatibility input | Accepted by | Removed in | What happens after removal |
|---|---|---|---|
| The eight scenario aliases above | every `forward-e2e-runner/3.x` release (with the deprecation notice) | the first `forward-e2e-runner/4.0.0` release | `--scenario <alias>` is a `SelectionError` ("unknown task"), like any other unknown name. |
| `p0-probe` subcommand alias | every `forward-e2e-runner/3.x` release | `forward-e2e-runner/4.0.0` | The subparser is deleted; `wasm-query-allowlist` is the only spelling. |
| `canonical_task_id` translation for old packages read by `report` / `recover` | permanent | not scheduled | Historical packages must stay readable; translation at read time is not an input alias. |

Removing an alias is a major bump by definition: an input the runner used to
accept starts being refused.

---

## 3. Environment Variables: `E2E_*` Primary, `A8_*` Legacy Fallback

### Resolution rule

The rule is the same at every consumer and is written once per language:

| Consumer | Implementation | On conflict |
|---|---|---|
| Suite runtime and orchestrator | `resolve_suite_env` in [`forward_e2e/suite/runtime.py`](../forward_e2e/suite/runtime.py) | `SuiteRuntimeError` |
| E2E planner and CLI | `resolve_operational_env` in [`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py), a thin wrapper over `resolve_suite_env` | `UsageError` (exit code 2) |
| Acceptance harness | `_read_env_with_legacy_alias` in [`scripts/acceptance_harness.py`](../scripts/acceptance_harness.py), for the four expectation variables in `LEGACY_ENV_ALIASES` | `AcceptanceError` |
| Kotlin harness | `requiredHarnessEnv(canonical, legacy)` in `harness/testermint/src/test/kotlin/ApiContainerControl.kt`; `requiredEnv` in `MarketplaceContractAcceptanceTests.kt` derives the pair from either prefix | `IllegalStateException` (test error) |
| Runner entrypoint (`E2E_APP_ROOT`) | `ops/runner/entrypoint.sh` | exits non-zero before anything runs |
| Host wrappers (`E2E_DOCKER_ROOT_VOLUME`) | `ops/e2e/run-e2e.sh`, `ops/e2e/Run-E2E.ps1` | `die` / `Fail` with exit code 2 |

In words:

1. `E2E_<NAME>` is read first.
2. If it is unset, `A8_<NAME>` is read instead.
3. If both are set to the **same** value, the value is used and the legacy
   name is simply redundant (normalised).
4. If both are set to **different** values, the run is refused with a message
   naming both variables. A conflict is never resolved by precedence, because a
   silently ignored `A8_*` value is exactly the kind of ambiguity a provenance
   runner must not tolerate.
5. A value that is empty or whitespace-only counts as unset.

`ops/runner/compose.yaml` cannot refuse a conflict (Compose interpolation has
no conditionals), which is why the wrappers check `E2E_DOCKER_ROOT_VOLUME`
against `A8_DOCKER_ROOT_VOLUME` **before** invoking Compose and then export
both names with the resolved value.

### Outbound exports are dual, baked defaults are not

Everything the runner exports to a **child process** is exported under both
names, so a consumer that still reads `A8_*` keeps working during the support
window:

- `scripts/acceptance_harness.py` exports every `E2E_*` / `A8_*` pair of the
  "Testermint subprocess" rows below into Gradle's environment;
  `harness/testermint/build.gradle.kts` forwards every variable starting with
  `E2E_` or `A8_` (plus `GONKA_REPO_ROOT`) into the test JVM.
- `ops/e2e/run-e2e.sh` and `ops/e2e/Run-E2E.ps1` export
  `A8_DOCKER_ROOT_VOLUME` next to `E2E_DOCKER_ROOT_VOLUME`.
- The internal Compose fragment `harness/network/ownership.yml` interpolates
  `${E2E_RUN_ID}`; it is rendered by Docker Compose inside the Testermint JVM's
  environment, which receives both names.

The one exception is the runner's own baked environment:
`ops/runner/Dockerfile` and `ops/runner/compose.yaml` set only the canonical
`E2E_WORKSPACE_DIR` and `E2E_OUTPUT_DIR`. The `A8_*` twins are still *read* as
a fallback (table below) but are no longer *set* by the image or the Compose
file: every reader resolves `E2E_*` first, so a baked twin had no consumer
and only turned an operator's `-e E2E_WORKSPACE_DIR=...` override into a
spurious "conflicting environment variables" refusal. `HostWrapperParityTests`
in `tests/unit/runner/test_e2e_host_wrappers.py` pins that neither file bakes
the legacy names.

### Variables with a legacy fallback

| Primary (`E2E_*`) | Legacy fallback (`A8_*`) | Read by |
|---|---|---|
| `E2E_WORKSPACE_DIR` | `A8_WORKSPACE_DIR` | `forward_e2e/execution/cli.py`, `forward_e2e/execution/planner.py`, `forward_e2e/suite/runtime.py`, `forward_e2e/suite/orchestrator.py`; the canonical name is set by `ops/runner/Dockerfile` and `ops/runner/compose.yaml`, the legacy name is only read |
| `E2E_OUTPUT_DIR` | `A8_OUTPUT_DIR` | same consumers as above |
| `E2E_APP_ROOT` | `A8_APP_ROOT` | `ops/runner/entrypoint.sh`, `forward_e2e/execution/planner.py` |
| `E2E_DOCKER_ROOT_VOLUME` | `A8_DOCKER_ROOT_VOLUME` | `ops/e2e/run-e2e.sh`, `ops/e2e/Run-E2E.ps1`, `ops/runner/compose.yaml` (volume name), `forward_e2e/execution/planner.py` |
| `E2E_EXPECTED_GONKA_SHA` | `A8_EXPECTED_GONKA_SHA` | `scripts/acceptance_harness.py` (`expected_gonka_sha`); frozen into the lock by the planner |
| `E2E_EXPECTED_PROTO_SHA` | `A8_EXPECTED_PROTO_SHA` | `scripts/acceptance_harness.py` (`expected_proto_sha`); the E2E layer never sets it and clears an ambient value |
| `E2E_EXPECTED_RUNTIME` | `A8_EXPECTED_RUNTIME` | `scripts/acceptance_harness.py` (`expected_runtime_versions`) |
| `E2E_EVIDENCE_MODEL` | `A8_EVIDENCE_MODEL` | `scripts/acceptance_harness.py` (`evidence_model`); only `a8.evidence/e2e-immutable-source/2` is accepted |
| `E2E_PYTHON` | `A8_PYTHON` | Testermint subprocess: `MarketplaceContractAcceptanceTests.kt`, `ApiContainerControl.kt` |
| `E2E_HARNESS` | `A8_HARNESS` | Testermint subprocess: `MarketplaceContractAcceptanceTests.kt` |
| `E2E_MARKETPLACE_DIR` | `A8_MARKETPLACE_DIR` | Testermint subprocess: `MarketplaceContractAcceptanceTests.kt` |
| `E2E_GONKA_DIR` | `A8_GONKA_DIR` | exported by the harness for the Testermint subprocess |
| `E2E_CONTEXT` | `A8_CONTEXT` | Testermint subprocess: `MarketplaceContractAcceptanceTests.kt`, which passes the value as `--context` to every harness re-entry |
| `E2E_RUN_ID` | `A8_RUN_ID` | `MarketplaceContractAcceptanceTests.kt`, `ApiContainerControl.kt`, `harness/network/ownership.yml` (`${E2E_RUN_ID}`) |
| `E2E_DEAL_WASM`, `E2E_FACTORY_WASM`, `E2E_CW20_WASM`, `E2E_CALLER_WASM` | `A8_DEAL_WASM`, `A8_FACTORY_WASM`, `A8_CW20_WASM`, `A8_CALLER_WASM` | `MarketplaceContractAcceptanceTests.kt` |
| `E2E_CONTAINER_CONTROL`, `E2E_CONTAINER_CONTROL_STATE_DIR` | `A8_CONTAINER_CONTROL`, `A8_CONTAINER_CONTROL_STATE_DIR` | `ApiContainerControl.kt` (invokes `harness/container_control.py`) |
| `E2E_OWNERSHIP_LABEL` | `A8_OWNERSHIP_LABEL` | exported by the harness; the label key itself is the constant `OWNERSHIP_LABEL` in `scripts/external_harness.py` and `harness/container_control.py` |

### Variables that never had a legacy twin

These were introduced with the `E2E_` prefix and have no `A8_*` fallback;
setting an `A8_`-prefixed spelling of them has no effect:

`E2E_RUNNER_IMAGE`, `E2E_RUNNER_IMAGE_ID`, `E2E_RUNNER_IMAGE_DIGEST`,
`E2E_RUNNER_REPO`, `E2E_RUNNER_SHA`, `E2E_PLAN_DIR`, `E2E_SECRETS_DIR`,
`E2E_CREDENTIAL_FILE`, `E2E_GIT_SECRET_FILE`, `E2E_GIT_USERNAME`,
`E2E_BRIDGE_DIR`.

### `A8_*` names that are not aliases

| Name(s) | Where | Why it keeps the `A8_` spelling |
|---|---|---|
| `A8_B3_FOREIGN_ADDRESS`, `A8_B3_FOREIGN_DENOM`, `A8_B3_FOREIGN_AMOUNT`, `A8_B3_COIN`, `A8_B3_COMMAND`, `A8_GENESIS_FILE`, `A8_PROVISION_DIR`, `A8_PROVISIONER_DERIVED_FROM_SHA256`, `A8_SHA_BEFORE`, `A8_SHA_AFTER`, `A8_SHA_FINAL` | `harness/network/foreign-native-genesis.yml`, `harness/network/genesis/foreign-native-genesis-provision.sh` | Environment **inside** the genesis chain-node container of the `foreign-native-preservation` scenario; part of the B3 fixture wire contract whose values (`gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny`, `ua8b3foreign`, `12345`) are shared with the Kotlin test and the verifier. |
| `A8_EXPECTED_GONKA_PREPARED_SHA`, `A8_EXPECTED_GONKA_BASE_SHA`, `A8_ALLOWED_TEST_PATHS` | `LEGACY_OVERLAY_ENVIRONMENT` in `scripts/acceptance_harness.py` | Variables of the removed overlay model. They are **refused**, not read: a caller still setting them is asking for a tree that differs from the selected commit. |
| `A8_TEST_CHILD_PID_FILE` | `MarketplaceHarnessProcessTests.kt` | Kotlin unit-test internal; never an operator input. |

### Freezing at plan time

Both `E2E_*` and `A8_*` (together with `GONKA_*` and `TESTERMINT_*`) are
registered in `SEMANTIC_ENV_PREFIXES`
([`forward_e2e/execution/planner.py`](../forward_e2e/execution/planner.py)).
Ambient variables with these prefixes are recorded into the lock at plan time
and scrubbed before execution if they are not part of the lock
(`apply_semantic_environment` in `forward_e2e/execution/executor.py`), so a
stray value on the host can neither change what a run proves nor leak into a
re-run.

### Support window and removal path for `A8_*`

| Step | Release |
|---|---|
| `A8_*` fallback accepted everywhere listed above, equal values normalised, conflicts refused | every `forward-e2e-runner/3.x` release |
| Dual outbound exports (`A8_*` next to `E2E_*`) | every `forward-e2e-runner/3.x` release |
| Baked `A8_WORKSPACE_DIR` / `A8_OUTPUT_DIR` removed from the Dockerfile and `compose.yaml` (readers unchanged) | done in `forward-e2e-runner/3.0.0` |
| Stop exporting `A8_*` from the harness and the wrappers; drop the legacy name from `resolve_suite_env` / `_read_env_with_legacy_alias` / `requiredHarnessEnv` call sites | `forward-e2e-runner/4.0.0` |
| Keep `A8_` in `SEMANTIC_ENV_PREFIXES` | permanent, so that a stale ambient `A8_*` variable is still frozen and scrubbed rather than silently ignored |

The B3 provisioner names and the refused overlay names in the table above
are not part of this schedule.

---

## 4. Preserved Wire-Format and Compatibility Identifiers

To stay compatible with on-chain state, historical evidence packages,
Gradle init scripts and existing Docker volumes, the following identifiers
intentionally keep their existing values:

- **Evidence model identifiers:** `a8.evidence/e2e-immutable-source/2` (the
  only model new packages may declare), `a8.evidence/e2e-prepared-build/1`,
  `a8.evidence/legacy-overlay/1` (historical, readable, never proof). Defined
  in three places on purpose; see `AGENTS.md` §2.
- **Source immutability schemas:** `a8.source-immutability-set/1`,
  `a8.source-immutability-set/2` (the set that also carries the network
  section), `a8.source-immutability/1` (one record) and
  `a8.source-snapshot/1` (`SNAPSHOT_SCHEMA` in
  `forward_e2e/suite/source_snapshot.py`).
- **Other document schema identifiers** matched literally by their consumers:
  `a8.external-harness-inputs/1`, `a8.network-manifest/1`,
  `a8.testermint-required-api/1`, `a8.testermint-api-compat/1`,
  `a8.testermint-classpath/1`, `a8.testermint-junit/1`,
  `a8.container-control/1`, `a8.b3-provision/1`,
  `a8.b3-genesis-verification/1`, `a8.e2e.image-provenance/v1`.
- **Build recipe and manifest keys:** recipe `contracts-a9-release-v1`, step
  `a9-release`, output directory `a9-release`, manifest hash key
  `a9_manifest_sha256` (`forward_e2e/execution/compat.py`,
  `forward_e2e/execution/deployment.py`, `forward_e2e/execution/runpackage.py`).
- **Docker ownership label:** exactly one label key is applied and checked,
  `io.gonka.a8.run-id` (`OWNERSHIP_LABEL` in `scripts/external_harness.py` and
  `harness/container_control.py`; applied by `harness/network/ownership.yml`).
  No `io.gonka.a8.owned` or `io.gonka.a8.task-id` label exists.
- **Runner Compose labels:** `owner: gonka24-smart` and
  `purpose: a8-private-dockerd` on the private Docker-root volume in
  `ops/runner/compose.yaml`.
- **Compose project name:** `a8`, set by the top-level `name:` key in
  `ops/runner/compose.yaml`. Before the relocation the name was implicit (the
  file lived in `ops/a8/`, and Compose derives the project name from the
  directory); after the move to `ops/runner/` the implicit name would have
  become `runner`, and `docker compose run` would have created a second
  project next to the containers, network and run-scoped resources of the
  old one. The explicit key keeps `docker compose ls`, `docker compose down`
  and the `com.docker.compose.project=a8` labels identical. The wrappers
  always pass `-f ops/runner/compose.yaml` and never set
  `COMPOSE_PROJECT_NAME`.
- **Default runner image tag:** `a8-runner:local` (`ops/runner/compose.yaml`,
  `ops/e2e/run-e2e.sh`, `ops/e2e/build-runner.sh`, the PowerShell twins).
  Changing it would orphan locally built images. Pass `E2E_RUNNER_IMAGE` (or
  `--runner-image` when creating a plan) to use another locator; with
  `--from` only the environment variable is accepted, see
  [§5](#5-handling-pre-refactoring-plans-and-run-packages).
- **Default named Docker volumes (`ops/runner/compose.yaml`):**
  `a8-workspace`, `a8-docker-root` (overridable through
  `E2E_DOCKER_ROOT_VOLUME`), `a8-cargo-registry-cache`, `a8-cargo-git-cache`,
  `a8-gradle-cache`, `a8-go-cache`. Renaming them would silently start every
  host from empty caches.
- **Gradle `-Pa8.*` project properties** consumed by the out-of-tree init
  script: `-Pa8.outRoot`, `-Pa8.classpathFile`, `-Pa8.upstreamClasspathFile`,
  `-Pa8.upstreamTestermintTest`, `-Pa8.upstreamTestermintTestSha256`.
- **On-chain store labels of the allowlist probe:** `p0-probe` and
  `a8-p0-probe-<run_id>` (`scripts/acceptance_harness.py`). These are labels
  recorded on chain and in evidence, not CLI aliases, and are not scheduled
  for removal.
- **B3 foreign-native fixture:** `B3_FOREIGN_ADDRESS`, `B3_FOREIGN_DENOM`,
  `B3_FOREIGN_AMOUNT` in `scripts/external_harness.py`, the Kotlin
  `B3_FOREIGN_*` constants, the `A8_B3_*` container variables and the denom
  `12345ua8b3foreign` in `forward-contracts`.
- **Target contract crate paths in `forward-contracts`:**
  `tests/contracts/a8-caller`, `tests/contracts/a8-cw20`,
  `tests/contracts/a8-query-boundary`.
- **Task run-id shape:** `make_task_run_id` in `forward_e2e/suite/models.py`
  keeps the baseline rule `<suite_id[:30]>-<ordinal:02d>-<task_id[:25]>`
  (`TASK_RUN_ID_TASK_CHARS = 25`). Several canonical IDs are longer than 25
  characters (for example `refund-boundary-and-vesting-addition` becomes
  `refund-boundary-and-vesti`); the ordinal, not the suffix, keeps run ids
  unique, exactly as before.

---

## 5. Handling Pre-Refactoring Plans and Run Packages

- **Runner version.** `ops/runner/RUNNER_VERSION` is `forward-e2e-runner/3.0.0`
  (it was `a8-runner/2.0.0`). The version is pinned separately from the
  product commits and recorded as `lock.runner.runner_version`.
  `assert_runner_matches_lock` (`forward_e2e/execution/executor.py`) refuses
  to execute a lock recorded under another version, hash or catalog. The
  version string is **not** what classifies a package: packages written by
  `a8-runner/2.0.0` keep that string, and `report` / `recover` grade them by
  their documents alone (next bullets). A `2.0.0` package whose lock is
  `e2e/run-lock/2` without overlay markers is still immutable-source
  evidence; only its lock is no longer executable on this runner.
- **Plans (`run.lock.json`).** `HARNESS_FILES`, `VERIFIER_FILES`,
  `NETWORK_FILES` and `EXTERNAL_TEST_DIRS` are hashed by path name and bytes,
  so every lock created before the relocation fails
  `assert_runner_matches_lock` on this runner. Create a fresh plan with
  `./ops/e2e/run-e2e.sh plan`; do not relax the check.
- **Replaying a plan after the image was rebuilt.** `build-runner.sh` /
  `Build-Runner.ps1` re-point the mutable tag `a8-runner:local` at the new
  image, while the lock pins the immutable image id
  (`lock.runner.image_id`); `assert_runner_image_matches`
  (`forward_e2e/execution/runner_image.py`) refuses to execute the lock inside
  any other image. `--runner-image` is a semantic flag (`SEMANTIC_FLAGS` in
  `forward_e2e/execution/cli.py`) and `--plan-id` is a lock-identity flag
  (`LOCK_IDENTITY_FLAGS` there: it does not change what is proven, but a
  replay inherits its plan id from the lock); both are refused together with
  `--from` (the bash and PowerShell wrappers repeat the `--runner-image`
  refusal because they consume that flag on the host), so the way to replay
  is the environment: `E2E_RUNNER_IMAGE=<image id or digest
  from the lock> ./ops/e2e/run-e2e.sh run --from <plan>/run.lock.json`. The
  wrappers resolve the locator to an id and inject it as
  `E2E_RUNNER_IMAGE_ID`, which is what the executor compares.
- **Historical run packages (`report` / `recover`).** `LoadedRunPackage` and
  `OfflineReporter` continue to read and grade historical packages offline
  without executing target code. Pre-rename task names inside them are
  translated through `canonical_task_id`. What a package can claim is decided
  from its documents, never from its runner version:
  `RunLock.provenance_model` (`forward_e2e/execution/runlock.py`) returns
  `immutable-source` only for an `e2e/run-lock/2` lock without overlay or
  prepared-commit markers, and `classify_provenance_model`
  (`forward_e2e/execution/outcome.py`) lets one `/1` document make the whole
  package `historical-prepared-build`, which carries the blocking
  `HISTORICAL_PREPARED_BUILD` finding. Task evidence declaring
  `a8.evidence/e2e-prepared-build/1` or `a8.evidence/legacy-overlay/1` keeps
  that classification and is never upgraded to immutable-source proof.
- **Old lock schemas.** `run` / `rerun` refuse any lock whose schema is not
  `e2e/run-lock/2` or which mentions an overlay or prepared commit
  (`assert_lock_executable` in `forward_e2e/execution/runlock.py`).

---

## 6. Compatibility Exceptions: Names Deliberately Not Renamed

The refactoring renamed paths, scenario IDs and environment variables. The
following internal names were left alone on purpose; each has a reason that
outweighs consistency.

| Name | Where | Reason |
|---|---|---|
| `_a9_manifest_path`, `test_a9_*` test names | `forward_e2e/execution/executor.py`, `tests/unit/harness/test_acceptance_harness.py` | They refer to the A9 release *artefact format* (`a9_manifest_sha256`, recipe `contracts-a9-release-v1`), which is a preserved wire identifier, not a repository path. |
| Module registration names `a8_acceptance`, `a8_external_harness`, `a8_source_snapshot` in `sys.modules` | `scripts/acceptance_harness.py` (`_load_external_harness`), `scripts/external_harness.py` and `scripts/run_go_boundary.py` (both `load_source_snapshot`), `tests/unit/harness/support.py`, `tests/unit/runner/test_e2e_evidence_model.py` | The scripts are loaded by file path, never imported by package name. The registration key is an internal handle the tests look up to share one loaded instance; renaming it changes nothing observable and would touch every by-path loader at once. |
| `import ... as a8` local aliases in tests | `tests/unit/harness/` | Same reason: a local handle for the by-path-loaded harness module. |
| Default runtime root `~/a8-runtime` and its `exclusive.lock` | `get_default_runtime_root` in `forward_e2e/suite/runtime.py`, `RuntimeLock` default in `forward_e2e/suite/lock.py` | Production callers always pass an explicit path (`<workspace>/exclusive.lock` from `DinDSupervisor`, `<runtime_root>/exclusive.lock` from the orchestrator); the home-directory default is only reached without a workspace. It is the lock domain older runners used, so renaming it would let a new and an old runner hold "exclusive" locks side by side instead of excluding each other. |
| Harness work directory `<gonka_dir>/../a8-work/<run_id>` | `default_work_root` in `scripts/external_harness.py`; mirrored by the runtime guard in `forward_e2e/suite/runtime.py` | Lives beside, never inside, the source snapshots; the name is checked by the runtime guard and recorded in evidence, so the harness and the suite must agree on it. Renaming is possible but has to change producer, guard and tests together. |
| `.a8-bundles/` ignore entry | `.gitignore` only | Nothing in this repository creates it any more (the wrappers stage local repositories under `.e2e-bridge*/`); the entry is kept so a checkout that still has the directory from an older runner does not show it as untracked. |
| Temporary-directory prefixes `a8-test-*` / `a8-immutable-*` in tests | `tests/unit/runner/`, `tests/unit/harness/` | Names of `tempfile.mkdtemp` prefixes only; they appear in no evidence. Left alone because the rename would touch ~30 test modules for no behavioural gain. |
| On-chain instantiate labels and token names (`a8-cw20-<run_id>`, `a8-foreign-cw20-<run_id>`, `a8-factory-<run_id>`, `a8-caller-<run_id>`, `a8-fee-...`, `a8-inactive-...`, `a8-late-donor-...`, `A8 Local Test USDT`, `A8 Foreign Asset`) | `scripts/acceptance_harness.py` (`bootstrap`) | Written into the chain state and into `live-context.json` of every run; a different label would make new evidence differ from historical evidence for no behavioural reason. |
| "A8" in the B3 provisioner's comments and log lines (`A8 B3 fixture`, `A8 provisioner: ...`) | `harness/network/genesis/foreign-native-genesis-provision.sh` | The script is a step-by-step reproduction of upstream `init-docker-genesis.sh` whose three documented differences are the B3 fixture; the fixture itself keeps its `A8_B3_*` wire variables (§3), so its comments keep the same label. Log lines are not evidence. |
| "the legacy A8 runner" in the exit-code contract docstring | `forward_e2e/execution/errors.py` | A historical reference that explains why the exit codes are what they are. |
| Gradle `-Pa8.*` properties and `a8*` task names | `harness/testermint/build.gradle.kts`, `gradle/out-of-tree.init.gradle.kts` | Part of the out-of-tree Gradle contract with the unmodified upstream Testermint build; see §4. |
| Kotlin `B3_FOREIGN_*` constants | `MarketplaceContractAcceptanceTests.kt` | Fixture wire values shared with the genesis provisioner and the verifier; see §4. |
| `real_*` helper names in `tests/unit/runner/real_fixtures.py` | test support | The prefix means "the real verifier is exercised on a producer-shaped document", which is still true; the module docstring says so. The fixtures themselves are synthetic (§7). |
| `load_synthetic_evidence`, `REQUIRED_SYNTHETIC_EVIDENCE` | `tests/unit/runner/real_fixtures.py` | The former `load_recorded_evidence` / `REQUIRED_RECORDED_EVIDENCE` names were removed in the fixture-provenance review: "recorded" is exactly what these documents are not. `load_synthetic_evidence` returns the committed document verbatim (marker and `synthetic_fixture` block included); callers strip the marker in memory, visibly, when they test a verifier's inner rules. |
| "recorded" in test names such as `test_recorded_actual_deltas…` and in fields such as `recorded_at_utc` | `tests/unit/runner/test_verifier.py`, producer documents | There "recorded" means "written into the document by the producer", not "captured from a live run". |
| `reporter.py` and `verifier.py` importing `forward_e2e.execution.file_safety` | `forward_e2e/suite/reporter.py`, `forward_e2e/suite/verifier.py` | The two reverse edges predate the refactoring (`ops/a8/reporter.py` → `.e2e.file_safety`). `file_safety` depends only on `execution.errors`, `execution.sources` and `execution.gitio`, none of which imports the suite, so there is no cycle. `tests/unit/runner/test_import_direction.py` pins exactly these two edges and fails if a third appears or if `file_safety` grows a suite import. |
| `vendor/contract_release/release.py --repo` default | vendored helper | The default resolves relative to the file (now `vendor/`), which is meaningless for the runner. The build recipe always passes `--repo <contracts checkout>` explicitly (`contracts-a9-release-v1` in `forward_e2e/execution/compat.py`), and the vendored bytes are intentionally unchanged (`EXTRACTION.json`). |
| JUnit artefact name `junit/TEST-MarketplaceContractAcceptanceTests.xml` | catalog `expected_artifacts` | The Kotlin class was not renamed, so the artefact name is stable. |

### Renames that are visible in evidence

- **R6.1 / R7.1 Kotlin test methods.** The `@Test` names
  `marketplace R6 dot 1 rejects all three selected CW20 sends then settles once`
  and `marketplace R7 dot 1 rejects selected second Bank send then retries once`
  are now
  `marketplace settlement commits once and each rejected USDT withdrawal rolls back atomically`
  and
  `marketplace native release rejects selected second Bank send then retries once`.
  The catalog's `exact_test_method` and `description` for
  `usdt-withdrawal-failure-recovery` and `native-release-rollback-retry` were
  changed in the same step, so the verifier's JUnit match stays exact. The
  Kotlin sources are compiled only inside the runner image against the
  selected Gonka's Testermint; the offline suites do not compile them.
  What the renamed task actually proves was traced end to end (Kotlin test →
  `claim-settle --cw20-fault-positions 1,2,3` → `claim_settle` in
  `scripts/acceptance_harness.py` → the `claim_settle` branch of
  `verify_live_context`): one settlement, then one rejected and rolled-back
  `withdraw_usdt` per selected recipient. The determination is a name-only
  discrepancy with no functional divergence; the vocabulary that keeps its
  historical spelling (checkpoint ids `cw20_three_send_rejections_asserted`
  and `settlement_atomic_rollback_verified`, the harness error text about
  "send #", the alias `package-b-r6-1`) is listed with the reasons not to
  rename it in isolation in
  [`docs/coverage.md` §5.2](coverage.md#52-usdt-withdrawal-failure-recovery-package-b-r6-1-producer-verifier-and-vocabulary).
- **Collector artefact classification.** `classify_artifact_kind`
  (`forward_e2e/suite/collector.py`) recognises boundary-run artefacts by the
  path markers in `_BOUNDARY_RUN_MARKERS` and the `go-boundary/` layout
  directory rather than by a task-id prefix, so canonical and historical task
  names classify identically.

---

## 7. Fixture Replacement Map

The recorded development receipts under `docs/reviews/evidence/` were
deleted, not relocated. Before deletion every consumer was traced and each
verified property was carried over to a self-contained synthetic fixture
whose chain identities are derivations of a labelled SHA-256 seed
(`tests/unit/runner/support/synthetic_evidence.py`; contract in
`tests/fixtures/evidence/README.md`). Every JSON document carries
`"test_fixture_only": true`, which `verify_live_context`,
`verify_go_boundary_report` and `verify_wasm_abi_report` reject outright, so
none of them can be mistaken for a live artefact. The four live-context
documents are identity-scrubbed derivations of the removed receipts (every
identity replaced, the recorded arithmetic kept); the two boundary fixtures
are generated from code and never held a recorded value.

| Removed recorded fixture | Verified property that had to survive | Synthetic replacement |
|---|---|---|
| `a8-c1-exact-e-20260910.json` (full live context of a lock-exact-E run) | Legacy pre-model `source` block is rejected as a downgrade; adapted in memory to `a8.evidence/e2e-immutable-source/2` it proves the `lock-exact-e` checkpoints (`Funded→Locked`, `recipient_locked`, `no_deal_bank_transfer`, `lock_tx_included_in_epoch_e`, `target_epoch_e_reached`); the `LEGACY_FIXTURE_SOURCE` digest pin in `test_e2e_evidence_model.py` | `tests/fixtures/evidence/lock-exact-epoch-legacy-context.json` — identity-scrubbed derivation of the receipt (addresses, hashes, validator key, timestamps, run id and store paths replaced by seed derivations; balances, heights and epochs kept) |
| `a8-abc-reviewed-20260910.json`, `claim_settle` slice | CW20 withdrawal fault rollbacks, pending-obligation preservation, per-role `withdrawal_txs`, rejected `settle_repeat` | `tests/fixtures/evidence/claim-settlement-fault-phase.json` — identity-scrubbed derivation (same rule) |
| `a8-abc-reviewed-20260910.json`, R1.1 / R2 slices (R1.1 was also read directly by `scripts/tests/test_a8_acceptance.py`) | `Refund` rejected at `E+5` after settlement; four-stage `r2_gift_checkpoint`; `vesting_addition` accounting; proportional gift `release` payouts proven by their own receipts; the harness gate `assert_refund_window_closed` on the recorded `attempt` | `tests/fixtures/evidence/settled-deal-late-refund-and-vested-gift.json` — identity-scrubbed derivation; `tests/unit/harness/test_acceptance_harness.py` reads its `refund_e_plus_5_phase` |
| `g3-terminal-release-repeat-002.json` | Terminal `ReleaseUnlockedGnk` repeat rejected with `NothingToRelease` (`code=5`, `codespace="wasm"`), unchanged Deal/Bank/CW20 snapshots | `tests/fixtures/evidence/terminal-release-repeat.json` — identity-scrubbed derivation |
| `a8-wasm-abi-probe-rerun-20260910.json` (the earlier `a8-wasm-abi-probe-20260910.json` had no test consumer) | 9-case `query_chain` envelope decoding report; `wasm_sha256` binding checked by `verify_wasm_abi_report` | `tests/fixtures/evidence/wasm-query-allowlist-abi.json` — generated from code by `synthetic_evidence.py`; never held a recorded value |
| `a8-go-boundary-20260910/` (`report.json`, `raw/go-test.json`, `build.log`, `raw/exit-code`) | `verify_go_boundary_report`: exit codes, `test_output_sha256` binding to the raw `go test -json` stream, pass events for `GO_BOUNDARY_REQUIRED_TESTS` | `tests/fixtures/evidence/go-query-error-classification/` — generated; its shape is attributed to the post-relocation `scripts/run_go_boundary.py` (`./query_faults`, `go-boundary/` layout), not to the baseline commit, whose `run_a8_go_boundary.py` wrote different paths |
| `a8-claim-expiry-positive-043.json` (D1 claim-expiry refund, target epoch 5) | Refund rejected at `E+1` with the contract's window error, committed at `E+2`; rejected bracket leaves balances unchanged, commit moves exactly the budget from Deal to Buyer with fee and bank unchanged | No committed document. The in-memory D1 contexts in `real_fixtures.py` (`real_claim_expiry_*`) use seed-derived identities, and `test_real_fixtures.py` derives every height, epoch and error text from the context's own `terms` and brackets (`assert_refund_phases_follow_the_declared_window`) instead of comparing to literals copied from the receipt |
| `a8-network-unconfirmed-stable-e1-002.json` (E1 network-unconfirmed refund; was loaded by `test_real_fixtures.py` on the baseline) | Refund rejected at `E+2`, committed at `E+3`; Buyer and Host coincide (catalog limitation of the emergency sweep); `NotFound` transport facts for the absent confirmation | No committed document. In-memory E1 contexts (`real_network_unconfirmed_*`) with seed-derived identities; the same derived-expectation checks as D1 plus `cw20.host == cw20.buyer` in every snapshot and the absence epoch computed from `terms` |
| `a8-b3-foreign-native-004.json` (named only in a `real_fixtures.py` docstring on the baseline; never loaded) | B3 foreign-native preservation: the Deal keeps the foreign denom through release | In-memory context `real_b3_context()`; the B3 genesis fixture account `gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny` is the one product literal kept because it is the wire value shared with the provisioner and the Kotlin test |
| Recorded identities (D1/E1/B3/R2 addresses, transaction hashes, timestamps, run ids) embedded in the baseline `real_fixtures.py` | Claim-expiry, network-unconfirmed, B3, R2 and late-donation in-memory contexts | Derivations: `synthetic_address(doc, role)`, `fixture_tx_hash(doc, label)`, `synthetic_timestamp(n)`, `synthetic_run_id(doc)`; `test_synthetic_evidence.py` applies the same identity checks to the in-memory contexts as to the committed documents |

**Lost on purpose.** The suites no longer regress against *recorded* producer
output. Concretely, three kinds of coverage went away with the receipts and
were not re-created:

- **Producer field-name drift.** A change in a live producer's field names is
  now caught only when the harness tests are updated together with the
  producer (`docs/evidence.md`), not by committed receipts drifting out of
  date. The four scrubbed documents still carry the producer's shape, but
  their shape is asserted by the same tests that would be edited alongside
  the producer.
- **Recorded chain arithmetic as an independent oracle.** The D1 and E1 refund
  heights, epochs and balances used to be literals copied from
  `a8-claim-expiry-positive-043.json` and
  `a8-network-unconfirmed-stable-e1-002.json`; they are now derived from the
  synthetic context's own `terms`. That proves the verifier applies the rule
  consistently, not that a real chain once produced those numbers. The
  scrubbed documents keep their recorded arithmetic, which is the only
  remaining link to a real execution, and it is not identity-bearing.
- **The dated review reports and matrices** (`docs/reviews/*.md`,
  `*-review-*.json`, `a8-matrix-source-register-*.json`) that cited run ids.
  No test depended on them; their claims about a given commit are not
  reproducible from this repository and are not restated anywhere.

No test depends on an old run id, review report or review matrix;
`NoDevelopmentArtifactDependencyTests` in
`tests/unit/runner/test_synthetic_evidence.py` scans `tests/` for the removed
path on every run. Nothing re-archives the deleted receipts: the only record
of them is Git history before the deletion commit.
