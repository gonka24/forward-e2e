# Validation Runbook

Step-by-step runbook for validating the `forward-e2e` runner and a target pair
of Gonka and Forward Marketplace contract commits.

---

## Stage 1: Offline Verification (No Docker Daemon or Network Required)

Run the three test suites and static checks from the repository root:

```bash
# 1. Runner unit test suite (suite + execution layers)
python3 -B -m unittest discover -s tests/unit/runner -p 'test_*.py' -v

# 2. Harness and release helper unit test suite
python3 -B -m unittest discover -s tests/unit/harness -p 'test_*.py' -v

# 3. Local Git object acquisition integration tests (requires host git)
python3 -B -m unittest discover -s tests/integration/local_sources -p 'test_*.py' -v

# 4. Verify Wasm allowlist probe manifest and checksums
./harness/wasm_query_allowlist/build.sh verify

# 5. Validate Compose configuration syntax
docker compose -f ops/runner/compose.yaml config --quiet
```

All five commands must exit `0` before building the runner image.

---

## Stage 2: Build and Inspect the Runner Container

Commit any runner changes first; the runner Dockerfile fetches the runner tree by
full 40-hex commit SHA from Git and ignores uncommitted working-tree files.

```bash
# 1. Build the runner image for the exact 40-hex runner SHA
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>

# 2. Verify catalog listing (must not start inner dockerd)
./ops/e2e/run-e2e.sh list
./ops/e2e/run-e2e.sh list --json
```

---

## Stage 3: Fast-Fail Negative CLI Checks

Each command below must fail immediately with exit code `2` (`UsageError`)
before touching the network or starting `dockerd`:

```bash
# Reject branch name instead of 40-hex SHA
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha main \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke \
  --output ./out/neg-branch

# Reject short SHA
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e489 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke \
  --output ./out/neg-short

# Reject supplying both --gonka-repo and --gonka-path
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-path ../gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke \
  --output ./out/neg-both-sources

# Reject combining --profile and --scenario
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile all \
  --scenario funded-claim \
  --output ./out/neg-both-selectors
```

---

## Stage 4 (optional, developer-only): Out-of-Tree Testermint Harness Compilation (`build-external-harness`)

> [!WARNING]
> This stage is **not a supported validation path** and is not part of any
> wrapper, plan or run package. It runs `scripts/acceptance_harness.py`
> directly on the host, which contradicts the runner's promise that nothing
> but Docker is needed there: it requires a host JDK (the runner image pins
> Temurin 21), Git, and a full Gonka working tree at the selected commit, and
> it runs Gonka's own Gradle wrapper. It exists for people editing
> `harness/testermint` who want a compile check before a multi-hour suite.
> Skip it unless you are doing exactly that; nothing later depends on it.

`build_external_harness` is the build-only half of `run_live`: pristine check
of the Gonka snapshot, the required upstream API check, the upstream classpath
export, the harness `testClasses` build and the after-snapshot comparison. It
never starts Docker or a network.

```bash
python3 scripts/acceptance_harness.py build-external-harness \
  --gonka-dir <PATH_TO_GONKA_CHECKOUT> \
  --expected-gonka-sha <GONKA_FULL_40_HEX_SHA> \
  --work-root /tmp/e2e-harness-check \
  --testermint-harness-dir ./harness/testermint
```

`--work-root` and the evidence directory (default `<work-root>/evidence`) must
lie outside the Gonka checkout (`_assert_outside_snapshots`). On success the
command prints `{"status": "pass", "evidence": "<work-root>/evidence"}`;
verify that `<PATH_TO_GONKA_CHECKOUT>` is still exactly the selected commit
(`git status --ignored` shows nothing) and that
`<work-root>/evidence/source-immutability.json` has top-level
`"verdict": "UNCHANGED"` with a `roots.gonka` record (this command measures the
Gonka root only; there is no `marketplace` or `network_root` entry here). Any
other verdict is reported as `[SOURCE_SNAPSHOT_MUTATED]` and the command exits
non-zero.

---

## Stage 5: Portable Plan, `smoke` Execution, and Replay

```bash
# 1. Create a portable plan package with bundled Git objects
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile smoke \
  --output ./out/plan-smoke

# 2. Relocate the plan package to prove path independence
mv ./out/plan-smoke /tmp/plan-smoke-moved

# 3. Execute the plan
./ops/e2e/run-e2e.sh run \
  --from /tmp/plan-smoke-moved/run.lock.json \
  --output ./out/e2e-smoke

# 4. Confirm --from rejects semantic overrides (must exit 2)
./ops/e2e/run-e2e.sh run \
  --from /tmp/plan-smoke-moved/run.lock.json \
  --profile all

# 5. Replay the exact same lock into a second output directory
./ops/e2e/run-e2e.sh rerun \
  --from ./out/e2e-smoke/<run-id>/run.lock.json \
  --output ./out/e2e-smoke-replay
```

Check in `./out/e2e-smoke/<run-id>/` (the task directory is keyed by the task
run id, `<run-id truncated to 30 chars>-01-lock-exact-e`; see
[`evidence.md`](evidence.md) §1):

- `e2e-run-result.json` and `result.json` have `"status": "PASSED"`,
  `"exit_code": 0` and `"schema_version": "e2e/run-result/2"`.
- `suite/<run-id>/runs/<task-run-id>/evidence/<task-run-id>/live-context.json`
  has, under `source`, `"evidence_model": "a8.evidence/e2e-immutable-source/2"`,
  `"source_immutability_verdict": "UNCHANGED"`, and `gonka_sha` /
  `marketplace_commit_sha` equal to the two SHAs in `run.lock.json`.
- `suite/<run-id>/runs/<task-run-id>/evidence/<task-run-id>/source-immutability.json`
  has `"schema": "a8.source-immutability-set/2"`, top-level
  `"verdict": "UNCHANGED"`, `"verdict": "UNCHANGED"` in each of
  `roots.gonka` and `roots.marketplace`, and a `network_root` block whose
  `verdict` is also `UNCHANGED`.
- `suite/<run-id>/summary.md` starts with
  `# Forward E2E Suite Summary: <run-id>` and records
  `acceptance_status: NOT_REVIEWED`.

---

## Stage 6: `boundary` Profile and Full 24-Task `all` Suite

```bash
# 1. Run the 5-task boundary profile (must not demand live-context.json)
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile boundary \
  --output ./out/e2e-boundary

# 2. Run the full 24-task suite
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha e86e4899bd8cf52d1ad4766c811f65230b2f9296 \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha 7497304e5dc6bf48accdd8c91549bc22de6997fc \
  --profile all \
  --output ./out/e2e-all
```

---

## Stage 7: Offline `report` and `recover` Verification

Neither `report` nor `recover` may start `dockerd`:

```bash
# 1. Offline report on the run package root
./ops/e2e/run-e2e.sh report --run ./out/e2e-smoke/<run-id>

# 2. Offline report pointed at the nested suite directory (must resolve upwards to the run package)
./ops/e2e/run-e2e.sh report --run ./out/e2e-smoke/<run-id>/suite/<run-id>

# 3. Move the exported host package aside and recover it from /workspace
mv ./out/e2e-smoke/<run-id> ./out/e2e-smoke/<run-id>.backup
./ops/e2e/run-e2e.sh recover --run <run-id> --output ./out/e2e-recovered
./ops/e2e/run-e2e.sh report --run ./out/e2e-recovered/<run-id>
```
