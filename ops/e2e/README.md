# E2E acceptance runner

Run the full acceptance suite against **an explicitly chosen compatible pair of commits** — a
Gonka chain commit and a contracts commit. Remote-source runs need Docker with
Compose V2 on the host; `--gonka-path` or `--contracts-path` also needs host Git
to bridge the selected commit into the container.

The target build toolchains (Go, Rust, `cosmwasm-check`, Java, Node), the harness,
the catalog and the verifier live inside the runner image. The runner installs
nothing on the host. The wrappers create or write only the directories you select for
output, the persistent workspace and runtime state, plus their small temporary
path-translation bridge; they do not write build or test outputs into either
source checkout. See [§7.1](#71-four-separate-places-sources-build-outputs-test-code-network-state)
for the boundary between source trees and run state.

> [!IMPORTANT]
> Versions are selected **only** by full 40-hex commit SHAs. Branches, tags, `HEAD`,
> short SHAs and revision expressions such as `HEAD~1` or `main@{upstream}` are
> rejected. Look the commit up first and pass it explicitly. There is no hidden
> fallback to `HEAD`, `main` or `latest`.

> [!IMPORTANT]
> **The selected commits are never modified.** Since runner `a8-runner/2.0.0`
> (plan schema `e2e/run-lock/2`, evidence model
> `a8.evidence/e2e-immutable-source/2`) there is no overlay, no prepared commit
> and no allow-list of changed paths. Gonka and the contracts are built exactly
> as selected; the Marketplace Kotlin scenarios, the network configuration and
> container control live in the runner and run *against* the unmodified Gonka.
> See [§7](#7-the-runner-is-not-the-code-under-test) and the verification
> [runbook](RUNBOOK-immutable-sources.md). Plans made by an older runner are
> refused by `run`/`rerun` (`PLAN_SCHEMA_SUPERSEDED`): create a new plan.

---

## 1. One-time bootstrap

Build the tools container once:

```bash
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>
```

```powershell
.\ops\e2e\Build-Runner.ps1 -RunnerSha <RUNNER_FULL_40_HEX_SHA>
```

This builds **only** the runner image. It does not fetch, build or run anything
from a target repository and it never starts the inner Docker daemon. It prints
the resolved image id and digest; keep them, they are what a plan is pinned to.

The runner is also selected by a full SHA. Docker fetches that commit from
`https://github.com/gonka24/forward-e2e.git`, verifies its HEAD and pristine tree,
and bakes `runner-source.json` into the image. Local uncommitted files are not
build inputs. The Dockerfile itself comes from the same selected Git context.
Use `--runner-repo` (PowerShell: `-RunnerRepo`) to select another HTTPS repository.
The plan records `runner.repo_url`, `runner.commit_sha` and `runner.tree_sha`
alongside the image ID and asset hashes. Execution refuses a missing or
different runner Git identity. Older images must be rebuilt and new plans made;
historical packages remain available for offline reporting.

> [!WARNING]
> A locally built image may show a `RepoDigest` even when it was never published.
> That digest is only a retrieval hint. A plan can be replayed only on a host
> that has the exact recorded image ID. Move a local image with
> `docker save | docker load`. Rebuilding from the same tag produces a *different*
> image id and is refused on purpose — see [§7](#7-the-runner-is-not-the-code-under-test).

Check it works (this starts no daemon and touches no network):

```bash
./ops/e2e/run-e2e.sh list
```

---

## 2. The command surface

One CLI, six subcommands. The host wrappers forward arguments verbatim, so every
example below is valid in both places.

| Subcommand | What it does | Starts inner dockerd? |
|---|---|---|
| `list` | Print the scenario catalog (`--json` for machine-readable) | no |
| `plan` | Resolve sources, write a portable `run.lock.json` + source package | no |
| `run` | Build and execute the suite | **yes** |
| `rerun` | Same as `run --from`, spelled for replay | **yes** |
| `report` | Re-grade a stored run package offline and rewrite its suite report | no |
| `recover` | Re-export a stored run (package + suite) from the workspace and re-grade it | no |

`plan` may reach the network to fetch Git objects, but it never executes code from
a target repository.

### Source flags

Per role (`gonka`, `contracts`) supply **exactly one** origin and **exactly one** SHA:

```
--gonka-repo HTTPS_URL  |  --gonka-path DIR          --gonka-sha FULL_40_HEX_SHA
--contracts-repo HTTPS_URL | --contracts-path DIR    --contracts-sha FULL_40_HEX_SHA
```

Passing both `--gonka-repo` and `--gonka-path`, or neither, is an error.

### Selection flags

`--profile {smoke,native,boundary,all}` **or** repeatable `--scenario ID`. They are
mutually exclusive and one is required. Duplicate `--scenario` values are
normalised; execution order always comes from the catalog, never from argv order.

### Operational flags

`--output DIR`, `--workspace DIR`, `--runtime-root DIR`, `--runner-image IMAGE`,
`--credential-file FILE`, `--credential-username NAME`, and for `run`/`rerun` also
`--run-id ID`, `--parent-run-id ID`, `--plan-id ID`.

The host wrappers additionally accept `--docker-root-volume NAME`. It selects
the named volume mounted at `/var/lib/docker`; the wrapper consumes the flag, so
it does not change the semantic lock. Use a fresh purpose-labelled name after a
confirmed daemon-state failure instead of deleting files inside Docker's data
root. Every selected state still shares `/workspace/exclusive.lock`, so this
option does not permit concurrent Testermint clusters.

These are the **only** flags allowed together with `--from`. Anything that changes
*what is being proven* is refused there — see [§5](#5-plan-once-replay-exactly).

---

## 3. Worked examples

Replace every `<...>` with a real full 40-hex SHA.

### 3.1 Both sources from public remotes

```bash
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile smoke \
  --output ./out/e2e
```

No host Git is needed for this remote-source example: the container does the cloning.

The runner pulls PostgreSQL and CoreDNS by immutable registry index digest for
the locked Docker platform before starting Testermint, then binds their compose
tags in its private daemon. `build-manifest.json.runtime_dependencies` records
the registry reference, platform, resolved image ID and RepoDigests. Both online
and offline grading compare container facts against those records and the lock;
the stored `pinned_runtime_dependency` flag alone is not proof. An older package
without the required dependency records cannot establish this provenance.

### 3.2 A local checkout as the contracts source

```bash
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile all \
  --output ./out/e2e
```

The host wrapper uses Git to read only committed **objects** from your checkout —
no working-tree files are copied. Your
working tree may be dirty, may sit on another branch, and may have `HEAD` pointing
somewhere completely different: the snapshot is still exactly
`<CONTRACTS_FULL_SHA>`, and your files are never modified. Uncommitted work is
therefore structurally excluded rather than accidentally tested.

### 3.3 Windows

```powershell
.\ops\e2e\Run-E2E.ps1 run `
  --gonka-repo https://github.com/gonka-ai/gonka `
  --gonka-sha <GONKA_FULL_SHA> `
  --contracts-path "C:\Users\me\My Projects\forward-contracts" `
  --contracts-sha <CONTRACTS_FULL_SHA> `
  --profile smoke `
  --output .\out\e2e
```

Paths with spaces are handled. Host Git is required for this local-source
example; no manual WSL preparation is required.

### 3.4 A private repository

Put the token in a file — never on the command line:

```bash
printf '%s' "$GH_TOKEN" > ~/.config/e2e-token
chmod 600 ~/.config/e2e-token

./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/my-org/private-gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile native \
  --credential-file ~/.config/e2e-token \
  --output ./out/e2e
```

The token is handed to Git through a credential helper. It never appears in an
argv, a URL, a log line, the lock or the evidence.

### 3.5 A single scenario

```bash
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_SHA> \
  --scenario funded-claim \
  --scenario package-a-happy-path \
  --output ./out/e2e
```

---

## 4. Supported targets

| # | Gonka (`upstream-wasm-queries-v1`) | Contracts (`contracts-workspace-v1`) | Notes |
|---|---|---|
| 1 | `gonka-ai/gonka@e86e4899bd8cf52d1ad4766c811f65230b2f9296` | any | verified upstream WASM epoch/reward/vesting queries commit |
| 2 | any commit whose `inference-chain/app/legacy.go` exposes marketplace queries | `gonka24/forward-contracts` at an explicit full SHA | matched by filesystem markers (`NewInferenceWasmCapabilities` + `EpochDataQuery`) |
| 3 | any commit matching `upstream-wasm-queries-v1` | `gonka24/forward-contracts@7497304e5dc6bf48accdd8c91549bc22de6997fc` | public repo **without** `ops/a8` |

Compatibility is decided by **measuring the source tree**, not by looking a SHA up
in a list. An adapter matches on filesystem markers; a SHA that also appears in
the adapter's verified-commit list upgrades the recorded `match_mode` from
`markers` to `verified-commit`. Pre-fix Gonka commits whose production
`inference-chain/app/legacy.go` lacks marketplace queries are rejected with
`UnsupportedCompatibilityError`.

Target 3 also shows that the contracts repository does **not** need to contain
`ops/a8`: the harness, catalog and verifier always come from the runner image, not
from the code under test.

`--profile all` includes `ct-package-c-policy`, the 71-case contract-policy and
`cw-multi-test` replacement for the retired native query-fault controller. The
two old R7.2 cases are covered by the reachable native
`emergency-host-only-recovery` scenario. Unit, Go and compiled-Wasm ABI evidence
is reported at its actual proof level; it is never labelled native E2E.

---

## 5. Plan once, replay exactly

```bash
# 1. Resolve sources and write a portable package. No dockerd, no target code run.
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile all \
  --output ./out/plan-2026-09

# 2. Execute it, possibly much later, possibly on another machine.
./ops/e2e/run-e2e.sh run --from ./out/plan-2026-09/run.lock.json --output ./out/e2e

# 3. Replay the same proof again.
./ops/e2e/run-e2e.sh rerun --from ./out/plan-2026-09/run.lock.json --output ./out/e2e-2
```

The package contains Git bundles for every source, so step 2 and 3 work **with the
remote offline or deleted**, and after the directory has been moved somewhere else.
Moving refs on the remote after step 1 cannot change what gets replayed.

An ordinary `run` with two repository URLs and full SHAs acquires both checkouts
once and executes against them directly. It writes the lock automatically but does
not create Git bundles. Replaying that lock with `--from` fetches the same exact
commits again; the explicit `plan` command above remains the option for offline
replay with bundled sources.

Planning keeps its default scratch repositories and inspection checkouts in temporary
directories outside the package and removes them on success or failure. Only the
lock and source bundles are retained in a successful plan package. Python callers
that explicitly supply `PlanRequest.scratch_dir` or `worktree_root` own those
directories and their cleanup; source worktree paths in `PlanResult` remain usable
after planning only when an explicit `worktree_root` was supplied.

`--from` refuses `--gonka-repo`, `--gonka-path`, `--gonka-sha`, `--contracts-repo`,
`--contracts-path`, `--contracts-sha`, `--profile`, `--scenario` and
`--runner-image`. Changing a version means creating a new plan, not amending an old
one. Your current checkout, branch and environment are irrelevant to a `--from`
run.

Each replay gets a **new run id**, **fresh owned network state** and new artifacts,
and records a `parent_run_id` back to the run it repeats. Earlier reports are never
rewritten. Replay is deliberately *not* a resume.

---

## 6. Reading the evidence

### 6.1 Two copies: the durable stage and the export

A run is written to a **runner-owned staging directory on the persistent
workspace volume first**; the host output directory is an **export** of that
state, taken both on success and on a handled failure. Losing or interrupting
the export therefore never destroys the only copy — `recover` repeats it.

```
<workspace>/<run-id>/
  run/                     the durable run package (everything below is exported from here)
  suite/runtime/           per-task runtime snapshots (referencing the verified immutable checkouts directly)
  src/ scratch/ git-helper/
<workspace>/image-provenance.json    the image provenance ledger, shared by all runs
```

```
out/e2e/<run-id>/
  run.json               concise run inputs summary (e2e/run-inputs/1): SHAs, selection, limits
  status.json            live stage, current/completed tasks and export status (e2e/run-status/1)
  result.json            final verdict (identical to e2e-run-result.json)
  run.lock.json          the original plan, byte-identical, with its lock_sha256
  build-manifest.json    what was actually built: image ids, binaries, WASM, tools,
                         observed runtime versions, build argv,
                         the deployed contract hashes and the restored environment
  execution-manifest.json  run id, parent, replay-of, command, fresh-state flag,
                         the hash of the build manifest, and where the suite went
  build/                 raw build outputs (the A9 release directory lives here)
  build-logs/            one log per build step
  suite/<suite-id>/      the exported suite: suite-plan.json, suite-result.json,
                         e2e-context.json, JUnit, checkpoints, release receipts,
                         logs, summary.md, coverage.json, artifact-index.json
  e2e-run-result.json    the verdict (see below)
```

A partial, failed or cancelled run has exactly this shape with the missing parts
missing: the lock and both manifests are written **before** the first build
starts. `status.json` is published to the output directory as tasks start and
finish, before the evidence export. If output storage is unavailable, the durable
stage retains the latest status for `recover`.

The suite directory is nested under `suite/<run-id>/`: the orchestrator writes the
suite directly into `<stage_dir>/suite/<run-id>/` and keeps task runtime snapshots
under `<workspace>/<run-id>/suite/runtime/`. Both locations are recorded in
`execution-manifest.json` (`suite_export_relpath`, `suite_workspace_dir`), so
`report` and `recover` read them instead of guessing.

### 6.2 `e2e-run-result.json` and `result.json` — the verdict

The whole run is graded in one place (`evaluate_run` in
`ops/a8/e2e/outcome.py`), and `run`, `report` and `recover` all use it, so the
three cannot answer differently about the same run. Both `e2e-run-result.json`
and `result.json` are written with the exact same verdict payload: the
status, the exit code, the identity of the run, the hash of every document that
was graded, the per-task evidence requirements and what was found for each, and
every finding with its code.

| Status | Exit code | Meaning |
|---|---|---|
| `PASSED` | `0` | Every applicable check passed. |
| `FAILED` | `1` | Something that had to hold did not. A verdict about the sources. |
| `INCOMPLETE` | `1` | The run never got far enough to have a verdict: missing or unsealed documents, no suite, a crash. |
| `CANCELLED` | `143` (or `130` for `Ctrl-C`) | The operator stopped it. Not a verdict either. |

A `PASSED` suite is **necessary but not sufficient**: a build failure, a
cancellation, a missing manifest, a deployment-hash mismatch, a runtime
mismatch, a container running an image this build did not produce, or a selected
task that left no evidence all keep the run from passing — even when
`suite-result.json` says `PASSED`.

The verdict files (`e2e-run-result.json` and top-level `result.json`) are
**derived**, so they are deliberately *not* copied by an export: they are
re-derived wherever the package is read. Everything else in the stage is
exported verbatim, an identical file at the destination is left alone, and a
*different* file at the destination is a conflict that aborts the export rather
than overwriting somebody else's evidence.

The build manifest is bound to the lock by `lock_sha256`, and the execution
manifest records the hash of the build manifest. Editing any of them breaks the
binding, so metadata cannot be swapped to make a run look like it proved
something else.

A failed suite can follow a successfully completed build; its build manifest may
therefore say `COMPLETE`. The whole-run verdict still cannot be `PASSED` and
does not exit zero. Acceptance stays `NOT_REVIEWED`.

### 6.3 `report` and `recover`

Both are fully offline: no daemon, no build, no target code, no network, and no
original checkout is needed.

- `report --run <path-or-id>` grades the **whole run package**. Pointing it at a
  part of a package — in particular the nested suite — resolves *upwards* to the
  package, because a suite result on its own is not an E2E verdict. It also
  regenerates that suite's `summary.md` / `coverage.json`.
- Any directory lacking a durable E2E run package (`run.lock.json`,
  `build-manifest.json`, `execution-manifest.json`) is rejected with `UsageError`.
- `recover --run <path-or-id>` reconciles any interrupted task runtime evidence
  into `<stage_dir>/suite/<run-id>` and exports the durable run package to
  `<output>/<run-id>/`, then re-grades and writes the verdict. Repeating it is
  safe. If the durable run package is not present in the workspace, `recover`
  fails closed. It never repeats a test or a build.
  Delivery attempts for the same durable package are mutually exclusive. If another
  delivery is active, `recover` fails with `DELIVERY_BUSY`; retry after it finishes.
  A destination belonging to another package is rejected before its delivery ledger
  is changed.

### 6.4 Output locations

`--output` is optional for `run`: without it the run goes to
`$A8_OUTPUT_DIR/runs` (`/out/runs` inside the container, i.e. the directory the
wrapper bound) and an implicit plan to `$A8_OUTPUT_DIR/plans/<plan-id>`. It is
still required for `plan`, because a plan package with no destination is
useless. The plan package itself is mounted read-only and is never written to by
a run.

`report` and `recover` accept either a run id or a run directory. The host
wrappers translate a host path given after `--run` into its location under the
mounted output directory, so the path printed by `run` can be pasted back
verbatim.


---

## 7. The runner is not the code under test

The harness, the external Kotlin scenarios, the network templates, the catalog
and the verifier are taken from the runner **image**, and the image's immutable
id — not its tag — is recorded in the lock, together with the runner version
(`ops/a8/RUNNER_VERSION`) and the hashes of every external test, build recipe
and network template. Before the inner network starts, the observed image id is
compared with the recorded one; a different image will not silently execute
someone else's plan.

### 7.1 Four separate places: sources, build outputs, test code, network state

Scenario failures and timeouts are recorded without skipping later scenarios
when cleanup is safe. They still make the whole run `FAILED`. Cleanup failures,
invalid provenance, unexpected runner errors and cancellation halt execution;
unrun tasks record why continuation was unsafe. No scenario is automatically retried.

| What | Where | Who may write it |
|---|---|---|
| **Sources** — the Gonka and contracts snapshots | materialised from Git objects at the selected SHAs, with pinned submodules | **nobody**. Measured by `ops/a8/source_snapshot.py` before the build, after the build and after execution (even when a task or the suite fails); any change, addition (including ignored files and planted binaries) or deletion stops the run and rules out `PASSED`. |
| **Build outputs** — staged Docker contexts, the mock-server jar, Gradle/cargo caches | the run's build directory, outside both snapshots | the build steps of the recipe. The recipe reproduces the upstream Makefile `docker build` calls with explicit, recorded `--build-arg`s instead of running `make build-docker` (which writes `.docker-context/` and Gradle output into the tree). `Builder.assert_outputs_outside_sources` refuses any step whose output points into a snapshot. |
| **Test code** — `ops/a8/harness/testermint` | runner image; compiled into `<work>/harness-build` | the external Gradle build. The selected Gonka's unmodified Testermint is compiled out-of-tree (`gradle/a8-out-of-tree.init.gradle.kts`) and consumed through its exported classpath; upstream `TestermintTest.kt` is copied read-only after a hash check; no other upstream test is compiled. Gradle is launched through the selected Gonka's own `gradle-wrapper.jar`, so no `gradlew.a8-linux` copy exists. |
| **Network state** — `GONKA_REPO_ROOT` for Testermint | `<work>/network-root` | the runner and Testermint. The **full working tree** (`copy_mode: "full_working_tree"`) of every tracked file and submodule file in the selected Gonka checkout (excluding `.git` metadata) is copied into `<work>/network-root` (hashes recorded in `network/network-manifest.json`), and verified before and after the run by `verify_network_root_integrity` (with runtime-created files such as `prod-local/**` recorded in `runtime_additions`); runner-owned Compose fragments (`a8-ownership.yml`, `a8-nats.yml`, `a8-b3-genesis.yml`) and the B3 genesis provisioner have their own paths. |

Further rules at this boundary:

- **One Gradle download cache per run.** All live scenarios use the build
  stage's `<build_out>/gradle-home` for the selected wrapper distribution and
  dependencies. Project caches, compiled outputs and chain state stay in each
  task's work root. Harness invocations disable Gradle build/configuration
  caches, so a cached task result cannot replace a fresh compilation or test.

- **Upstream API check before any network.** `required-upstream-api.json` lists
  every upstream Testermint symbol the scenarios use; a missing one fails with
  `TESTERMINT_API_MISSING` naming it. Gonka is never patched to make it fit.
- **Genesis.** `init-docker-genesis.sh` stays unmodified and is never shadowed
  by a mount. Only `b3-foreign-native` uses the external provisioner
  (`ops/a8/harness/network/genesis/a8-genesis-provision.sh`, mounted at its own
  path `/a8-provision`), which repeats the upstream sequence with standard
  `inferenced` commands and adds `12345ua8b3foreign` to the fixed test address
  with `genesis add-genesis-account`. It records the command and the genesis
  before and after, and the harness checks that every other balance and the
  supply are otherwise unchanged. The provisioner is pinned to the hash of the
  upstream script it was derived from; a different script refuses B3
  (`GENESIS_PROVISIONER_INCOMPATIBLE`). Editing chain state after start is
  forbidden.
- **API restarts.** The six restart scenarios stop and start the API through
  `ops/a8/harness/container_control.py`: it records the container id and the
  run-ownership label (`io.gonka.a8.run-id`) before stopping, starts **the same
  id**, and waits for the admin API. A foreign or unowned container is refused.
- **Go and Wasm probes** (`ops/a8/harness/go-boundary`, `ops/a8/harness/wasm-probe`)
  are built separately from production images, with the selected Gonka as a
  read-only dependency; Gonka's `go.mod`/`go.sum` hashes are recorded before and
  after. Their proof levels are unchanged: they never become native E2E.

Consequences worth knowing:

- A tag such as `a8-runner:local` is only a *locator*. The wrapper resolves it and
  warns that it is mutable.
- Rebuilding the image produces a new id, so an old plan will refuse to run on it.
  That is intentional: the proof includes which runner produced it.
- Dependency and image caches are allowed and keyed on source/build/adapter
  identity, but the state chain between runs is never reused, and caches can never
  change the selected version. Cache volumes are deliberately mounted at
  `/root/.cargo/registry` and `/root/.cargo/git` rather than over `/root/.cargo`,
  so a stale cached install cannot shadow the versioned tools baked into the image.
- An image that predates the build is only accepted when the local provenance
  ledger (`<workspace>/image-provenance.json`) records that exact image id as
  produced by an earlier build of the *same* source identity — which is what a
  genuine Docker cache hit looks like. An unknown or differently-sourced old image
  is refused, and the manifest marks every accepted image with `cache_hit` and the
  proof it relied on.
- `Ctrl-C`/`SIGTERM` stops the build that is running: each build step leads its own
  process group, the group is signalled (`SIGTERM`, then `SIGKILL` after a grace
  period), the remaining stages are abandoned, the partial evidence is kept, and
  the exit code is 130/143. A cancelled run is an interruption, never a verdict.

---

## 8. What the runner hands to the harness

The executor never lets the suite re-decide what is being proven. It builds an
`E2ERunContext` (`ops/a8/e2e/context.py`) and the adapters append
`harness_argv_extras()` to the `run-live` command line, so every expectation the
plan made is passed explicitly:

| Flag | Value | Why it exists |
|---|---|---|
| `--manifest` | the verified A9 release manifest of this build | the suite deploys those artifacts instead of starting a second release build |
| `--expected-gonka-sha` | the **selected** commit | mandatory; the snapshot must be exactly this commit and the running binary must report exactly it — there is no second, "prepared" commit |
| `--expected-marketplace-sha` | the selected contracts commit | the contracts snapshot is checked for pristine-ness against it |
| `--work-root` | a directory outside both snapshots | build output, Gradle state, test code output and network state go here, never into a source tree |
| `--testermint-harness-dir` | the **runner's** `ops/a8/harness/testermint` | the target checkout must not supply the tests that judge it |
| `--expected-runtime FIELD=VALUE` | measured from the selected sources | proves the running binary is the one that was built |
| `--evidence-model` | `a8.evidence/e2e-immutable-source/2` | see below |

The removed flags `--overlay-dir`, `--expected-gonka-prepared-sha`,
`--expected-gonka-base-sha` and `--allowed-test-path` no longer exist; passing
one is a usage error.

### The `--evidence-model` contract

The producer **declares which rules it followed** instead of leaving the
consumer to guess:

| | historical: legacy overlay | historical: E2E prepared build | current: immutable source |
|---|---|---|---|
| identifier | `a8.evidence/legacy-overlay/1` | `a8.evidence/e2e-prepared-build/1` | `a8.evidence/e2e-immutable-source/2` |
| sources | selected commit + overlay | selected commit + overlay committed as a "prepared" commit | exactly the selected commit, verified before/after |
| the running binary must report | the requested commit | the prepared commit | the selected commit |
| graded as proof by this runner | no (readable only) | no (readable only, classified `historical-prepared-build`) | yes |

The value is written into `source.evidence_model` of every `live-context.json`
the run produces, and into `identity.json`. Inside a new run package an
undeclared or historical-declared context is a **downgrade error**: removing the
field cannot buy weaker rules. An unknown value is an error on both sides.

`live-context.json` `source` carries, in the new model: `gonka_sha`,
`gonka_tree_sha`, `marketplace_commit_sha`, `marketplace_tree_sha`,
`source_immutability_verdict`, `source_immutability_sha256`,
`external_harness_inputs_sha256`, `network_manifest_sha256`,
`a9_manifest_sha256`, `a9_contract_sha256`, `test_contract_sha256`, `command`,
and the observed `runtime`. The fields `gonka_prepared_sha`,
`gonka_overlay_manifest_sha256` and `gonka_test_harness_sha` are rejected if
present.

Per live task the evidence directory additionally holds
`source-immutability.json`, `external-harness/*` (API check, exported classpath,
harness input hashes), `testermint-junit/*.xml`, `network/network-manifest.json`,
`container-control/*.json`, and for B3 `genesis/*.json`.

### Old plans and old packages

`run`/`rerun` accept only `e2e/run-lock/2` plans. An older lock, or any lock
that mentions an overlay or a prepared commit, is refused with
`PLAN_SCHEMA_SUPERSEDED` (exit 1) before anything is started or written:
create a new plan with `plan`. `report` and `recover` still read old packages
by their format version and grade them by their original rules, labelled
`historical-prepared-build`; they are never presented as proof of unmodified
sources, and existing reports and raw evidence are never overwritten.

`A8_EVIDENCE_MODEL` is a semantic variable: it is frozen into the lock, and an
ambient value that was not part of the plan is cleared before the run starts.

Full tables, including what each proof level owes, are in
[`ops/a8/README.md`](../a8/README.md) §8 and §9.

---

## 9. Honest caveats

### Cache and runtime retention

The runner intentionally preserves its Docker-root and dependency-cache
volumes, raw suites, and durable run packages. This makes reruns faster and
preserves failure evidence, but storage is not automatically bounded. Measure
named A8 volumes, export durable `run/` packages first, and then remove only
confirmed-owned obsolete runtime `src/`, `scratch/` and `git-helper/`
directories or explicitly retired Docker-root volumes. Never use a global
prune as an A8 cleanup mechanism; raw `suite/` directories may contain
diagnostics not present in the normal export.

### Not yet verified live

Validation is commit-specific. Historical development runs do not verify
this extracted runner image or a new product pair; record fresh results for them. Follow the
[runbook](RUNBOOK-immutable-sources.md) before relying on a result.

### Behaviour that moved out of Gonka

The old overlay replaced upstream `DockerGroup.kt`. Its extra 5-second sleeps
and one-time retry of the join stack `up` cannot be reproduced from outside
Testermint. Bounded restarts and readiness are expressed in the runner's Compose
fragments instead, so join-stack start-up timing may differ from the overlay
runs.


> [!NOTE]
> **The contracts are built once.** The executor builds the A9 release, records its
> hashes in the build manifest, and hands the resulting `build-manifest.json` to the
> harness with `--manifest`, so `run-live` deploys exactly those artifacts instead
> of rebuilding from `HEAD`. Independence is preserved differently and more
> strictly than by a duplicate build: after the run, the hashes the evidence claims
> were deployed (`source.a9_manifest_sha256`, `source.a9_contract_sha256`) are
> compared with the hashes this build produced, and a mismatch fails the run. The
> harness's own test fixtures (`a8_caller`, `a8_cw20`) are accounted for separately
> from the production contracts.

> [!NOTE]
> **Image portability.** A `RepoDigest` does not prove registry availability.
> Replay requires the exact image ID, obtained by pulling a published image or
> transferring the local image. See [§1](#1-one-time-bootstrap).

> [!NOTE]
> **No instrumented runtimes.** Test-only patches on top of the selected commit
> no longer exist, so there is no "instrumented" build. A runtime that reports
> any commit other than the selected one fails the run.

---

## 10. Reviewer runbook

These are the commands for a reviewer who wants to verify the implementation
by executing it. Check the CI results and run evidence for the exact commit
under review; this runbook alone is not a passing result. The runner image,
external harness and live chain stages need their own recorded results.

For the immutable-source change specifically, follow
[`RUNBOOK-immutable-sources.md`](RUNBOOK-immutable-sources.md) first: offline
tests, then the external harness build, then one short native scenario, then
all 24 checks on the pinned SHA pair.

> [!NOTE]
> `.github/workflows/ci.yml` configures `ops/a8/tests`, `scripts/tests` and
> the local Git integration suite under Python 3.11 in `python-offline-runner`.
> Check that repository Actions are enabled and that the job ran for this commit.
> Live containerized runs require Docker and are executed during dedicated
> validation/release runs.

The [local Git integration suite](../a8/integration_tests/README.md) checks
acquisition from dirty checkouts using host-installed Git. Run it separately
with `python3 -B -m unittest discover -s ops/a8/integration_tests -v`; it is
also a separate CI step.

```bash
# 0. Offline unit tests (no Docker, no network).
python3 -B -m unittest discover -s ops/a8/tests -v
python3 -B -m unittest discover -s scripts/tests -v
python3 -B -m unittest discover -s ops/a8/integration_tests -v

# 1. Build the runner image once.
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>

# 2. Catalog listing. Must not start a daemon.
./ops/e2e/run-e2e.sh list
./ops/e2e/run-e2e.sh list --json

# 3. Negative checks: each must fail fast, before any network or daemon.
./ops/e2e/run-e2e.sh plan --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha main --contracts-path ../forward-contracts --contracts-sha <SHA> --profile smoke   # branch
./ops/e2e/run-e2e.sh plan --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e489 --contracts-path ../forward-contracts --contracts-sha <SHA> --profile smoke  # short
./ops/e2e/run-e2e.sh plan --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-path ../gonka --gonka-sha <SHA> --contracts-path ../forward-contracts \
  --contracts-sha <SHA> --profile smoke                                       # repo+path
./ops/e2e/run-e2e.sh plan --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <SHA> --contracts-path ../forward-contracts --contracts-sha <SHA> \
  --profile all --scenario funded-claim                                       # profile+scenario

# 4. Plan, then confirm the package is portable and offline-replayable.
./ops/e2e/run-e2e.sh plan --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-path ../forward-contracts --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile smoke --output ./out/plan-check
mv ./out/plan-check /tmp/plan-moved            # relocate the package
./ops/e2e/run-e2e.sh run --from /tmp/plan-moved/run.lock.json --output ./out/e2e

# 5. --from must refuse semantic overrides.
./ops/e2e/run-e2e.sh run --from /tmp/plan-moved/run.lock.json --profile all

# 6. Full run against the upstream commit, then replay.
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke --output ./out/e2e
./ops/e2e/run-e2e.sh rerun --from ./out/e2e/<run-id>/run.lock.json --output ./out/e2e-2

# 7. Boundary-only selection. Must not ask for deployment evidence.
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile boundary --output ./out/e2e-boundary

# 8. Report and recover must not start a daemon.
./ops/e2e/run-e2e.sh report  --run ./out/e2e/<run-id>
cat ./out/e2e/<run-id>/e2e-run-result.json
./ops/e2e/run-e2e.sh report  --run ./out/e2e/<run-id>/suite/<run-id>   # must grade the whole run

# 9. Move the host export aside, then recover it from the persistent workspace.
mv ./out/e2e/<run-id> ./out/e2e/<run-id>.saved
./ops/e2e/run-e2e.sh recover --run <run-id> --output ./out/e2e-recovered
./ops/e2e/run-e2e.sh report  --run ./out/e2e-recovered/<run-id>
```

Expected observations: step 3 fails on all four with a non-zero exit and an
explanatory message; step 4 succeeds with the remote unreachable; step 5 is
refused and lists the rejected flags; step 6's second command produces a
different run id with a `parent_run_id`; step 7 passes without any
`live-context.json` being demanded; step 8's second `report` resolves upwards to
the run package and prints the same verdict as the first; step 9 rebuilds the
package from the workspace and reports the **same** verdict as before the export
was moved aside. None of steps 8–9 writes a dockerd log.

---

## 11. Related documents

- [`AGENTS.md`](../../AGENTS.md) — how to work in this repository: import direction, test conventions, provenance invariants.
- [`ops/a8/README.md`](../a8/README.md) — the legacy runner, the run layout, the state truth table, the evidence and compatibility tables.
- [`RUNBOOK-immutable-sources.md`](RUNBOOK-immutable-sources.md) — step-by-step verification of the immutable-source runner.
- [`ops/a8/harness/testermint/README.md`](../a8/harness/testermint/README.md) — the external Kotlin harness and its Gradle boundary.
