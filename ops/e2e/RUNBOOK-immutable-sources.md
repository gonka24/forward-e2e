# Runbook: verifying the immutable-source E2E runner

> [!IMPORTANT]
> **Status is commit-specific.** Check CI and saved run evidence for the exact
> runner commit and product pair under review. Passing offline tests do not
> establish that the external harness, Docker image or live scenarios work.
> Complete Steps 1–4 before treating the full change as verified.

This runbook checks four things in order:

1. The runner's own logic works offline.
2. The external Kotlin harness compiles against an **unmodified** Gonka.
3. One short native scenario passes end to end.
4. All 24 checks pass on the pinned product pair.

Do not start a later step until the earlier one passes.

## Historical baseline pair for this runbook

| Product | Commit |
|---|---|
| Marketplace (`gonka24/forward-contracts`) | `ee142406863f287cc6e065bc4762c78506f75063` |
| Gonka | `c33c9eaa5bc40c53b564159b5e1534bbfdab8a08` |

The runner version is pinned separately, in [`ops/a8/RUNNER_VERSION`](../a8/RUNNER_VERSION).
The plan records it, together with the runner image ID, next to the product SHAs and their Git trees. Changing the runner therefore never changes which product commit is being proven.

> [!NOTE]
> `ee14240` is the historical Marketplace baseline this runner change was written on top of. It is a proposed product under test for this runbook, **not** the current PR head. To validate PR #27's product code, use its exact full commit SHA as `--contracts-sha`, record that choice with the runner image ID, and review the resulting run package. The runner code comes from the runner image, never from the contracts checkout.

## Step 1 — offline unit tests (no Docker, no network)

```bash
python3 -B -m unittest discover -s ops/a8/tests -p 'test_*.py' -v
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py' -v
```

**Expected:** both suites finish with `OK`, and skipped tests are only the platform-conditional ones.

These tests are the regression net for the new model. Among other things they cover:

- **Snapshot tampering.** A changed, deleted or added snapshot file, a moved submodule, an overlay attempt or a planted binary is rejected.
- **Harness boundary.** A missing upstream Testermint API or a missing selected JUnit test fails with an explicit error.
- **Container control.** The API container controller restarts the same container by its saved ID, and refuses a foreign or unowned container.
- **B3 genesis.** The B3 genesis delta is checked, with one-fact negative fixtures.
- **Go boundary.** The Go boundary build leaves Gonka's `go.mod`/`go.sum` untouched.
- **Old plans and packages.** `run`/`rerun` refuse an old plan. An old package is read as historical and never receives the immutable-source classification.
- **Catalog.** `catalog-ee14240.json` remains a frozen pre-change snapshot. The current 24-scenario catalog intentionally differs because G3 now names the actual `NothingToRelease` rejection oracle; current selector order and deterministic catalog hashing are covered by `ops/a8/tests/test_catalog.py`.

## Step 2 — build the external harness against the unmodified Gonka

This step compiles `ops/a8/harness/testermint` against the selected Gonka's own Testermint classes. It starts no network and no chain.

```bash
./ops/e2e/build-runner.sh                      # builds a8-runner:local

git clone https://github.com/gonka-ai/gonka /tmp/gonka-checkout
git -C /tmp/gonka-checkout checkout --detach <GONKA_FULL_SHA>
git -C /tmp/gonka-checkout submodule update --init --recursive

docker run --rm --entrypoint python3 \
  -v /tmp/gonka-checkout:/input/gonka:ro \
  -v /tmp/a8-harness-work:/work \
  a8-runner:local \
  /app/scripts/a8_acceptance.py build-external-harness \
    --gonka-dir /input/gonka --work-root /work
```

The Gonka checkout is mounted **read-only**. That is the point: any write into it fails loudly.

**Expected:**

- The exit code is 0.
- `/tmp/a8-harness-work/evidence/` contains:
  - `external-harness/api-compat.json`, with every required symbol present;
  - `external-harness/testermint-classpath.txt`;
  - `external-harness/harness-inputs.json`;
  - `source-immutability.json`, with `"verdict": "UNCHANGED"`.
- `git -C /tmp/gonka-checkout status --porcelain --ignored` prints nothing, and there is no `testermint/gradlew.a8-linux`.

If an API is missing, the error names it. Do **not** patch Gonka to fix it. A missing API means the selected Gonka commit is not supported by this runner version.

## Step 3 — one short native scenario

`lock-exact-e` is the `smoke` profile.

```bash
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile smoke --output ./out/plan-smoke

./ops/e2e/run-e2e.sh run --from ./out/plan-smoke/run.lock.json --output ./out/e2e-smoke
./ops/e2e/run-e2e.sh report --run ./out/e2e-smoke/<run-id>
```

**Expected:**

- `run.json`, `status.json`, `result.json` and `e2e-run-result.json` are present at the run root; `result.json` and `e2e-run-result.json` show `status: PASSED`, `provenance_model` for immutable sources, and acceptance `NOT_REVIEWED`.
- The suite evidence for the task contains:
  - `source-immutability.json` (`UNCHANGED` for both `gonka` and `marketplace`, plus `network_root.verdict = "UNCHANGED"`);
  - `external-harness/*`;
  - `testermint-junit/*.xml`, with the selected test executed;
  - `network/network-manifest.json`, with `copy_mode: "full_working_tree"`, `verification.verdict = "UNCHANGED"`, and every upstream copy's hash equal to its source hash;
  - `live-context.json`, with `source.evidence_model = "a8.evidence/e2e-immutable-source/2"` and `source.gonka_sha` equal to `<GONKA_FULL_SHA>`.
- The build manifest lists explicit `docker build` argv/build args for every Gonka image, and there is no `make build-docker`.

**Negative check — an old plan must be refused.** Take any `run.lock.json` written before this change:

```bash
./ops/e2e/run-e2e.sh run --from <old-plan>/run.lock.json --output ./out/should-fail
```

It must exit non-zero with `PLAN_SCHEMA_SUPERSEDED` and write nothing over existing reports.

## Step 4 — all 24 checks on the selected pair

```bash
./ops/e2e/run-e2e.sh plan \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_SHA> \
  --contracts-path ../forward-contracts --contracts-sha <CONTRACTS_FULL_SHA> \
  --profile all --output ./out/plan-all

./ops/e2e/run-e2e.sh run --from ./out/plan-all/run.lock.json --output ./out/e2e-all
./ops/e2e/run-e2e.sh report --run ./out/e2e-all/<run-id>
```

**Expected:**

- All 24 tasks are graded: 19 native, `go-boundary`, `wasm-abi-boundary` and 3 contract tests. Each one keeps its original proof level, so a boundary or contract test never becomes native E2E.
- `b3-foreign-native` additionally carries:
  - `genesis/b3-provision.json`;
  - `genesis/genesis-before-b3.json`;
  - `genesis/genesis-final.json`;
  - `genesis/b3-genesis-verification.json`, with no findings. It checks that the address holds exactly `12345ua8b3foreign`, that every other balance is unchanged, and that supply changed by exactly that coin.
- The six restart scenarios carry `container-control/*.json`, where the stop and start records show the **same** `container_id`.
- The whole run is `PASSED` only if every task's source-immutability verdict is `UNCHANGED`.

## What to send back

Send `result.json` (or `e2e-run-result.json`) and the `report` output of steps 3 and 4. If a step failed, also send the first blocking finding's `code`.
