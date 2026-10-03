# Host CLI Wrappers (`ops/e2e`)

Host-side entrypoints for building the runner container and invoking the E2E
CLI inside it:

| Script | Platform | Purpose |
|---|---|---|
| [`build-runner.sh`](build-runner.sh) | Linux / macOS (Bash) | Builds the runner Docker image from a full 40-hex commit SHA of `forward-e2e`. |
| [`Build-Runner.ps1`](Build-Runner.ps1) | Windows (PowerShell) | Windows equivalent of `build-runner.sh`. |
| [`run-e2e.sh`](run-e2e.sh) | Linux / macOS (Bash) | Translates host paths, bridges local Git repositories viacommitted objects only, and runs `list`, `plan`, `run`, `rerun`, `report`, or `recover` inside the runner container. |
| [`Run-E2E.ps1`](Run-E2E.ps1) | Windows (PowerShell) | Windows equivalent of `run-e2e.sh`. |

## Quick Usage

```bash
# 1. Build the runner image from a committed 40-hex SHA
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>

# 2. List scenarios
./ops/e2e/run-e2e.sh list

# 3. Run the smoke profile
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_40_HEX_SHA> \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> \
  --profile smoke \
  --output ./out/e2e
```

## Full Documentation

All operational details, architecture boundaries, evidence schemas, and
validation steps live in [`docs/`](../../docs/README.md):

- [`docs/operations.md`](../../docs/operations.md) — complete CLI subcommands, flags, credentials, Docker volumes, and replay semantics.
- [`docs/architecture.md`](../../docs/architecture.md) — runner vs target separation and the four isolated filesystem zones.
- [`docs/evidence.md`](../../docs/evidence.md) — run package layout, `e2e-run-result.json`, and `a8.evidence/e2e-immutable-source/2`.
- [`docs/validation.md`](../../docs/validation.md) — step-by-step operator and reviewer runbook.
- [`docs/migration.md`](../../docs/migration.md) — scenario ID and `E2E_*` / `A8_*` environment variable aliases.
