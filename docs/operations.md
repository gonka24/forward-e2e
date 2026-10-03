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

Allowed on `plan`, `run`, `rerun`, and with `--from`:

| Flag | Purpose |
|---|---|
| `--from <PATH>` | Execute (`run` / `rerun`) from an existing `run.lock.json` or plan directory. Rejects all source and selection flags. |
| `--output <DIR>` | Host directory for the exported plan or run package. Required for `plan`; optional for `run` (defaults to `<E2E_OUTPUT_DIR>/runs`). |
| `--workspace <DIR>` | Persistent workspace root inside the container (default: `/workspace`). |
| `--runtime-root <DIR>` | Override directory for per-task runtime snapshots. |
| `--runner-image <IMAGE>` | Override the runner container image reference. |
| `--credential-file <FILE>` | Path to a file containing a Git HTTPS token for private repositories (passed via credential helper; never logged or written to evidence). |
| `--credential-username <NAME>` | Username paired with `--credential-file` (default: `x-access-token`). |
| `--run-id <ID>` | Explicit run identifier for `run` / `rerun`. |
| `--parent-run-id <ID>` | Explicit parent run identifier for `run` / `rerun`. |
| `--plan-id <ID>` | Explicit plan identifier. |
| `--docker-root-volume <NAME>` | Host-wrapper flag (`run-e2e.sh` / `Run-E2E.ps1`) selecting the named Docker volume mounted at `/var/lib/docker` inside the runner. |

---

## 4. Common Workflows

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

### 4.4 Offline Re-Grading (`report`) and Workspace Recovery (`recover`)

```bash
# Re-grade an exported run package (or its nested suite path, which resolves upwards)
./ops/e2e/run-e2e.sh report --run ./out/e2e/<run-id>

# Recover and export a run package from the persistent /workspace volume
./ops/e2e/run-e2e.sh recover --run <run-id> --output ./out/e2e-recovered
```

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
