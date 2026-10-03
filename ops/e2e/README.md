# Host CLI Wrappers (`ops/e2e`)

Host-side entrypoints for building the runner container and invoking the E2E
CLI inside it:

| Script | Platform | Purpose |
|---|---|---|
| [`build-runner.sh`](build-runner.sh) | Linux / macOS (Bash) | Builds the runner Docker image from a full lowercase 40-hex commit SHA of `forward-e2e` (`--runner-sha` or `E2E_RUNNER_SHA`); anything else is rejected before Compose is called. |
| [`Build-Runner.ps1`](Build-Runner.ps1) | Windows (PowerShell) | Windows equivalent of `build-runner.sh`. |
| [`run-e2e.sh`](run-e2e.sh) | Linux / macOS (Bash) | Translates host paths, bridges local Git repositories via committed objects only, and runs `list`, `plan`, `run`, `rerun`, `report`, or `recover` inside the runner container. |
| [`Run-E2E.ps1`](Run-E2E.ps1) | Windows (PowerShell) | Windows equivalent of `run-e2e.sh`. |

## Host requirements the wrappers enforce

- **Docker Compose V2 only.** `compose()` in `run-e2e.sh` and
  `build-runner.sh` runs `docker compose version` first and dies if it fails;
  there is no fallback to the retired `docker-compose` v1 binary, because
  [`ops/runner/compose.yaml`](../runner/compose.yaml) relies on a top-level
  `name:`, a `URL#SHA` build context, `platform:` and nested
  `${A:-${B:-c}}` defaults that v1 cannot parse. The PowerShell wrappers call
  `docker compose` directly.
- **`--runner-image` is refused together with `--from`** on the host
  (`run-e2e.sh` and `Run-E2E.ps1` both stop before launching anything). The
  wrapper consumes `--runner-image` itself, so the container could not apply
  its own rule that a saved plan is executed exactly as planned; the wrapper
  preserves that rule instead.
- **Credentials are mounted, not copied.** `--credential-file <host file>`
  must name an existing file; the wrapper binds that file's directory
  read-only at `/run/secrets/e2e` (`E2E_SECRETS_DIR` in
  `ops/runner/compose.yaml`) and forwards
  `--credential-file /run/secrets/e2e/<basename>` to the container. Nothing
  is baked into the image. See
  [`docs/operations.md`](../../docs/operations.md) §4.3.

## Where output lands on the host

The wrapper mounts one host directory at `/out` inside the container:

| Invocation | Host directory mounted at `/out` | Where the run package is written |
|---|---|---|
| `--output <dir>` | `<dir>` (created if missing) | `<dir>/<run-id>` — the container is given `--output /out` |
| no `--output` | `<repo>/out` | `<repo>/out/runs/<run-id>` (the container default `/out/runs`); `plan` without `--output` writes to `<repo>/out/plans/<plan-id>` |

`--run` for `report` / `recover` accepts either a host directory or a bare run
id. A host directory is enough on its own: the wrapper walks up from it to
the enclosing package (first directory containing `run.lock.json`,
`execution-manifest.json`, `build-manifest.json`, `delivery.json` or
`e2e-run-result.json`), mounts the package's parent (or its grandparent when
the parent is `runs/`) at `/out` and rewrites the path accordingly. If you
also pass `--output`, the run directory must live under it, otherwise the
wrapper dies. A bare id is forwarded unchanged; inside the container it is
tried as given, then as `/out/<id>`, then as `/out/runs/<id>`
(`_candidate_run_dirs` in `forward_e2e/execution/cli.py`), so pass the
`--output` that holds it, or none for the default `<repo>/out/runs/<run-id>`
layout.

## Quick Usage

```bash
# 1. Build the runner image from a committed 40-hex SHA
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>

# 2. List scenarios
./ops/e2e/run-e2e.sh list

# 3. Run the smoke profile; the package lands at ./out/e2e/<run-id>
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
