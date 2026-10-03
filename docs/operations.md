# Operations Guide

Complete CLI reference for building the runner image and operating the
`forward-e2e` acceptance runner.

---

## 1. Building the Runner Image

Build the runner container once for a specific committed 40-hex SHA of
`gonka24/forward-e2e`:

```bash
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>
```

PowerShell:

```powershell
.\ops\e2e\Build-Runner.ps1 -RunnerSha <RUNNER_FULL_40_HEX_SHA>
```

Optional flags:

- `--runner-repo <HTTPS_URL>` (PowerShell: `-RunnerRepo <HTTPS_URL>`) — override
  the Git repository URL from which the Dockerfile fetches `<RUNNER_FULL_40_HEX_SHA>`
  (default: `https://github.com/gonka24/forward-e2e.git`).
- `--image <TAG>` (PowerShell: `-Image <TAG>`) — local tag for the built image
  (default: `a8-runner:local`).

How the build works:

1. `ops/runner/Dockerfile` clones `--runner-repo` at `--runner-sha` inside the
   build container, verifies the commit and tree via
   `python3 -B -m forward_e2e.execution.runner_source`, and writes
   `/app/runner-source.json`. Local uncommitted working-tree files never enter
   the image.
2. It runs `RunnerBuildContextTests` inside the image build to confirm all
   required fixtures and files are present.
3. It prints the immutable Docker image ID (`sha256:...`). Plans record this
   exact image ID, `runner.repo_url`, `runner.commit_sha`, and `runner.tree_sha`.

> [!WARNING]
> Rebuilding the runner produces a new image ID. Existing plans pinned to an
> earlier image ID will refuse to execute on the new image; create a new plan
> when upgrading the runner. To move a locally built runner image between hosts,
> use `docker save` / `docker load`.

---

## 2. CLI Subcommands

Invoke subcommands through [`./ops/e2e/run-e2e.sh`](../ops/e2e/run-e2e.sh)
(Linux/macOS) or [`.\ops\e2e\Run-E2E.ps1`](../ops/e2e/Run-E2E.ps1) (Windows).
Inside the container, the entrypoint invokes `python3 -m forward_e2e.execution.cli`.

| Subcommand | Purpose | Starts Inner `dockerd`? | Network / Target Code Executed? |
|---|---|---|---|
| `list` | Print the 24-task scenario catalog (`--json` for JSON output) | No | No |
| `plan` | Resolve sources, verify compatibility markers, write portable `run.lock.json` + Git bundles | No | Fetches Git objects only; never runs target code |
| `run` | Build targets (or load from plan) and execute the selected suite | **Yes** | **Yes** |
| `rerun` | Replay an existing `run.lock.json` with a fresh run ID and fresh network state (`run --from`) | **Yes** | **Yes** |
| `report` | Re-grade an existing run package offline and regenerate `summary.md` and `coverage.json` | No | No |
| `recover` | Reconcile interrupted task runtime snapshots from `/workspace`, export the run package, and grade it | No | No |

---

## 3. Flags Reference

### 3.1 Source Selection Flags (`plan` and `run` without `--from`)

For each target (`gonka` and `contracts`), supply **exactly one** source origin
(`*-repo` or `*-path`) and **exactly one** full 40-character hexadecimal SHA:

```text
--gonka-repo <HTTPS_URL>       | --gonka-path <LOCAL_DIR>
--gonka-sha <FULL_40_HEX_SHA>

--contracts-repo <HTTPS_URL>   | --contracts-path <LOCAL_DIR>
--contracts-sha <FULL_40_HEX_SHA>
```

- Branches, tags, `HEAD`, short SHAs, and revision expressions (`HEAD~1`) are
  rejected with `UsageError` (exit code `2`).
- When `--gonka-path` or `--contracts-path` is given, the host wrapper exports
  only committed Git objects for `<FULL_40_HEX_SHA>` into a temporary bare repo
  bridge. Uncommitted changes or untracked files in your local checkout are
  never copied or tested, and your checkout is never modified.

### 3.2 Scenario Selection Flags (`plan` and `run` without `--from`)

Supply **either** `--profile` **or** one or more `--scenario` flags (mutually
exclusive):

- `--profile {smoke,native,boundary,all}`
  - `smoke`: `lock-exact-e` (1 native task)
  - `native`: all 19 live Testermint chain scenarios (`NATIVE_TASKS`)
  - `boundary`: the 5 non-native boundary and contract-policy tasks (`BOUNDARY_TASKS`: `go-query-error-classification`, `wasm-abi-boundary`, `contract-network-unconfirmed-policy`, `contract-claim-expiry-policy`, `contract-query-fault-policy`)
  - `all`: all 24 catalog tasks (`BOUNDARY_TASKS` followed by `NATIVE_TASKS`)
- `--scenario <ID>` (repeatable): accepts canonical scenario IDs (e.g.
  `refund-boundary-and-vesting-addition`) or legacy aliases (e.g. `package-a-r1-r2`);
  see [`docs/coverage.md`](coverage.md) and [`docs/migration.md`](migration.md).
  Duplicate flags are deduplicated, and execution order always follows the
  canonical catalog order.

### 3.3 Operational Flags

`--from` replays a sealed decision and never re-opens it
(`assert_no_semantic_overrides` in
[`forward_e2e/execution/cli.py`](../forward_e2e/execution/cli.py)). Two groups
of flags are therefore refused together with `--from`, with exit code `2` and
an error that lists the flags a replay *does* accept (`ALLOWED_WITH_FROM`):

- `SEMANTIC_FLAGS` — everything that changes **what is proven**: the four
  source flags of §3.1, `--profile`, `--scenario` and `--runner-image`. The
  bash and PowerShell wrappers repeat the `--runner-image` refusal on the host
  because they consume that flag before the container sees it.
- `LOCK_IDENTITY_FLAGS` — `--plan-id`. It does not change what is proven, but
  a replay has no plan identity of its own; it inherits the one sealed in the
  lock, so accepting the flag and ignoring it would let an operator believe a
  plan had been renamed.

| Flag | Applies to | Purpose |
|---|---|---|
| `--from <PATH>` | `run`, `rerun` | Execute from an existing `run.lock.json` or plan directory. Rejects all source and selection flags, `--runner-image` and `--plan-id`. |
| `--output <DIR>` | `plan`, `run`, `rerun`, `report`, `recover`; allowed with `--from` | Destination directory. Required for `plan`. For `run`/`rerun` the package lands at `<DIR>/<run-id>`; without the flag it lands at `<E2E_OUTPUT_DIR>/runs/<run-id>` (`/out/runs/<run-id>` inside the container). See §4 for what that means on the host. |
| `--workspace <DIR>` | `plan`, `run`, `rerun`, `report`, `recover`; allowed with `--from` | Persistent workspace root inside the container (default: `/workspace`). |
| `--runtime-root <DIR>` | `run`, `rerun`; allowed with `--from` | Override directory for per-task runtime snapshots. |
| `--runner-image <IMAGE>` | `plan`, `run` without `--from` | Override the runner container image reference. **Refused with `--from`**: the lock pins the image id, and a replay must happen in that image. To replay after a rebuild, point the wrappers at the pinned image through the environment (`E2E_RUNNER_IMAGE=<image id or digest from the lock>`), see [`migration.md`](migration.md) §5. |
| `--credential-file <FILE>` | `plan`, `run`, `rerun`; allowed with `--from` | Path to a file containing a Git HTTPS token for private repositories (passed via credential helper; never logged or written to evidence). |
| `--credential-username <NAME>` | as above | Username paired with `--credential-file` (default: `x-access-token`). |
| `--run-id <ID>` | `run`, `rerun`; allowed with `--from` | Explicit run identifier. |
| `--parent-run-id <ID>` | `run`, `rerun`; allowed with `--from` | Explicit parent run identifier. |
| `--plan-id <ID>` | `plan`, `run` without `--from` | Explicit plan identifier. **Refused with `--from`** (the plan id comes from the lock; name the execution with `--run-id` instead). |
| `--run <PATH_OR_ID>` | `report`, `recover` | The run to grade or recover: an exported run directory (or a nested suite path, which resolves upwards to the package), or a bare run id, which the container resolves against the output root (`<output>/<id>`, `<output>/runs/<id>`; `_candidate_run_dirs`) and, for `recover`, the durable stage under `--workspace` (`_resolve_recovery_target`). The wrappers translate a host directory to its location under the single `/out` mount, so a directory given with `--output` must live under that `--output`. |
| `--docker-root-volume <NAME>` | host wrappers only | Selects the named Docker volume mounted at `/var/lib/docker` inside the runner (default `a8-docker-root`, or `E2E_DOCKER_ROOT_VOLUME`). |
| `--keep-resources` | — | Accepted by the parser for parity with the suite runner but **always rejected inside the runner container** (`cmd_run`): the inner `dockerd` and its containers end with the container, so there is nothing to keep. |

The host wrappers require **Docker Compose V2** (`docker compose`); the retired
Python `docker-compose` v1 binary cannot parse
[`ops/runner/compose.yaml`](../ops/runner/compose.yaml) and is not tried
(`compose()` in [`run-e2e.sh`](../ops/e2e/run-e2e.sh) and
[`build-runner.sh`](../ops/e2e/build-runner.sh); `Run-E2E.ps1` and
`Build-Runner.ps1` call `docker compose` directly).

---

## 4. Common Workflows

Every example is shown for `run-e2e.sh` (Linux/macOS) and `Run-E2E.ps1`
(Windows). Both wrappers forward the arguments verbatim to the single parser
inside the container; they only translate host paths and bind mounts.

**Where the results land on the host.** The wrapper mounts exactly one host
directory at `/out`: the directory given with `--output`, or `<repo>/out` when
the flag is absent (`OUTPUT_DIR` in `run-e2e.sh` / `Run-E2E.ps1`,
`ops/runner/compose.yaml`). Combined with the container-side defaults of §3.3:

| Invocation | Host location of the run package |
|---|---|
| `run … --output ./out/e2e` | `./out/e2e/<run-id>/` |
| `run …` (no `--output`) | `<repo>/out/runs/<run-id>/` |
| `plan … --output ./out/plan-package` | `./out/plan-package/run.lock.json` (plus bundles) |

Inside the run package you will find `run.lock.json`, `build-manifest.json`,
`execution-manifest.json`, `result.json` / `e2e-run-result.json` (the whole-run
verdict) and `suite/<run-id>/` with `summary.md`, `suite-result.json` and the
per-task evidence; see [`evidence.md`](evidence.md) §1 for the full layout. The
wrapper's exit code is the verdict's exit code (§5 of `evidence.md`): `0` only
for `PASSED`.

### 4.1 Direct Run from Remote Repositories

```bash
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke \
  --output ./out/e2e
```

```powershell
.\ops\e2e\Run-E2E.ps1 run `
  --gonka-repo https://github.com/gonka-ai/gonka `
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 `
  --contracts-repo https://github.com/gonka24/forward-contracts `
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc `
  --profile smoke `
  --output .\out\e2e
```

### 4.2 Plan Once, Replay Offline (`plan` -> `run --from` -> `rerun --from`)

```bash
# 1. Create a portable plan package with bundled Git objects
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> \
  --profile all \
  --output ./out/plan-package

# 2. Execute the locked plan (works offline)
./ops/e2e/run-e2e.sh run \
  --from ./out/plan-package/run.lock.json \
  --output ./out/e2e

# 3. Replay the exact same lock in a fresh run directory
./ops/e2e/run-e2e.sh rerun \
  --from ./out/plan-package/run.lock.json \
  --output ./out/e2e-replay
```

```powershell
.\ops\e2e\Run-E2E.ps1 plan `
  --gonka-repo https://github.com/gonka-ai/gonka `
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 `
  --contracts-path ..\forward-contracts `
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> `
  --profile all `
  --output .\out\plan-package

.\ops\e2e\Run-E2E.ps1 run --from .\out\plan-package\run.lock.json --output .\out\e2e

.\ops\e2e\Run-E2E.ps1 rerun --from .\out\plan-package\run.lock.json --output .\out\e2e-replay
```

Steps 2 and 3 accept only the flags in `ALLOWED_WITH_FROM` (§3.3). In
particular, do not add `--runner-image` or `--plan-id`: both are refused, and
the replay must run in the image id recorded in the lock.

### 4.3 Private Repositories with `--credential-file`

```bash
printf '%s' "$GH_TOKEN" > ~/.config/e2e-token
chmod 600 ~/.config/e2e-token

./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/my-org/private-gonka \
  --gonka-sha <GONKA_FULL_40_HEX_SHA> \
  --contracts-path ../forward-contracts \
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> \
  --profile native \
  --credential-file ~/.config/e2e-token \
  --output ./out/e2e
```

```powershell
Set-Content -Path "$env:USERPROFILE\e2e-token" -Value $env:GH_TOKEN -NoNewline

.\ops\e2e\Run-E2E.ps1 run `
  --gonka-repo https://github.com/my-org/private-gonka `
  --gonka-sha <GONKA_FULL_40_HEX_SHA> `
  --contracts-path ..\forward-contracts `
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> `
  --profile native `
  --credential-file "$env:USERPROFILE\e2e-token" `
  --output .\out\e2e
```

The token file is bind-mounted read-only under `/run/secrets/e2e/` and handed
to Git through a credential helper; it is never written into the plan, the
lock or the evidence.

### 4.4 Offline Re-Grading (`report`) and Workspace Recovery (`recover`)

`report` re-derives the whole-run verdict from the documents of an exported run
package without executing anything (`grade_run_package` → `evaluate_run`),
rewrites `result.json` / `e2e-run-result.json` and regenerates `summary.md` and
`coverage.json`. `recover` is for a run that was interrupted before export: it
reconciles the per-task runtime snapshots left in the persistent `/workspace`
volume (`reconcile_suite_runtime_evidence`), exports the durable stage as a run
package and grades it. Neither starts the inner `dockerd`.

```bash
# Re-grade an exported run package (or its nested suite path, which resolves upwards)
./ops/e2e/run-e2e.sh report --run ./out/e2e/<run-id>

# Re-grade a run that was written without --output
./ops/e2e/run-e2e.sh report --run ./out/runs/<run-id>

# Recover and export a run package from the persistent /workspace volume
./ops/e2e/run-e2e.sh recover --run <run-id> --output ./out/e2e-recovered
```

```powershell
.\ops\e2e\Run-E2E.ps1 report --run .\out\e2e\<run-id>

.\ops\e2e\Run-E2E.ps1 recover --run <run-id> --output .\out\e2e-recovered
```

A bare suite directory (one holding `suite-plan.json` / `suite-result.json`
but no run package envelope) is refused by `report`: a suite result is never
an E2E verdict. A historical package keeps its original `e2e-run-result.json`;
the re-derived verdict is written beside it as
`e2e-run-result.historical-regrade.json` and carries the blocking
`HISTORICAL_PREPARED_BUILD` finding.

---

## 5. Docker Volumes, Exclusivity, and Cancellation

- **Private Inner `dockerd`:** The host `/var/run/docker.sock` is never mounted
  into the runner container. During `run` and `rerun`, `DinDSupervisor` starts an
  isolated `dockerd` inside the container backed by the named volume mounted at
  `/var/lib/docker` (`a8-docker-root` by default, or overridden via
  `--docker-root-volume`).
- **Single-Cluster Exclusivity (`/workspace/exclusive.lock`):** Every `run` and
  `rerun` acquires an exclusive flock on `/workspace/exclusive.lock` before
  starting `dockerd`. Concurrent runs against the same workspace volume fail
  fast rather than colliding on cluster ports or container names.
- **Signal Handling and Cancellation:** `SIGINT` (`Ctrl-C`, exit code `130`) and
  `SIGTERM` (exit code `143`) terminate the active build step's process group
  (`SIGTERM` followed by `SIGKILL` after grace period), perform container
  ownership cleanup, export partial evidence, and record the run outcome as
  `CANCELLED`.
