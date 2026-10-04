# Gonka Wasm gRPC allowlist probe

This directory contains an unaudited, test-only CosmWasm contract used to
regression-test Gonka's contract-facing gRPC allowlist. A real Wasm fixture is
needed in addition to `app/legacy_test.go`: the map-level test verifies paths and
Go response constructors, while this contract proves that Wasm bytecode can
encode requests, cross `AcceptListGrpcQuerier`, and decode native protobuf
responses.

The probe exposes typed queries for exactly these production routes:

```text
/inference.inference.Query/GetCurrentEpoch
/inference.inference.Query/ListClaimRecipients
/inference.inference.Query/EpochPerformanceSummaryByParticipant
/inference.streamvesting.Query/TotalVestingAmount
```

`RawGrpc` is an intentionally adversarial test interface used only to verify
that broad or unknown paths are denied and malformed payloads on allowed paths
fail during decoding. It is not a production contract API.

## Layout and consumers

```text
src/                         Rust source and minimal protobuf wire mirror
build.sh                     canonical source-to-Wasm builder
artifacts/p0_probe.wasm      committed optimized fixture
artifacts/checksums.txt      SHA-256 integrity manifest
```

The historical full-app test `app/wasm_grpc_query_allowlist_test.go` is not
present in the pinned Gonka source commits used by this runner, and no current
runner task consumes this copy automatically. The runner's `wasm-abi-boundary`
task builds a different contract crate (`tests/contracts/a8-query-boundary`) and
injects synthetic host responses; it does not exercise Gonka's app/router or
prove its deny paths. Its coverage gap is recorded in
[`docs/coverage.md`](../../docs/coverage.md).

This fixture remains available for the explicit `wasm-query-allowlist` (legacy
alias `p0-probe`, which prints a deprecation notice) harness subcommand when an
operator supplies a compatible Wasm artifact. Its checksum manifest and
external-test tree hash bind the checked-in copy to the runner version, but
neither proves that the binary was exercised by an automated suite run or
rebuilt from these sources during that run.

What `wasm_query_allowlist` in `scripts/acceptance_harness.py` does when it
runs: it stores the probe (label `p0-probe`), instantiates it as
`a8-p0-probe-<run_id>`, checks the four supported queries, requires the broad
`/inference.inference.Query/Params` query to be rejected with Gonka's specific
`'<path>' path is not allowed from the contract` response — a timeout, RPC
failure or other error that merely mentions the route does not count as a
denial — and appends the result as the phase `p0_wasm_grpc_allowlist` to the
context it was given.

```bash
python3 scripts/acceptance_harness.py wasm-query-allowlist \
  --context <path-to-live-context.json> \
  --wasm harness/wasm_query_allowlist/artifacts/p0_probe.wasm
```

Where this can run, stated honestly:

- `--context` must be a bootstrapped live context carrying `chain.chain_id`,
  `run_id`, `accounts.host`, `contracts.deal` and `terms.target_epoch`, i.e.
  one written by a NATIVE task that has already deployed a Deal.
- The command reaches the chain through `docker exec -i genesis-node
  inferenced …` (`DockerGonka`, `DEFAULT_NODE = "genesis-node"`). That
  container exists only inside the runner's **private** Docker daemon while
  a NATIVE task is executing, and is removed by the ownership cleanup at the
  end of the task. It is never visible to the host's Docker, so the command
  above cannot work from the host.
- The runner entrypoint always executes the E2E CLI, whose subcommands are
  `list`, `plan`, `run`, `rerun`, `report` and `recover`; none of them runs
  this probe, and no catalog task does.

There is therefore **no supported path to execute the live allowlist check
today**. The only conceivable environment — a shell inside the running runner
container during a NATIVE task — is neither automated nor documented. Until a
catalog task exists for it, treat the allowlist obligation as *not covered*
(see [`docs/coverage.md`](../../docs/coverage.md) §3.3 and §4) and this
directory as a maintained source artefact whose offline checks
(`./harness/wasm_query_allowlist/build.sh verify`, the Rust unit tests below)
are the only ones that run.

## Prerequisites and canonical build

Run these commands from the repository root. Docker is the canonical engine
selected by this fixture's build recipe:

```bash
make -C harness/wasm_query_allowlist build CONTAINER_ENGINE=docker
make -C harness/wasm_query_allowlist check CONTAINER_ENGINE=docker
```

Podman may be supplied explicitly for local experimentation, but committed
fixture bytes are defined by the Docker CI build. The builder is pinned to
`cosmwasm/optimizer:0.17.0` by OCI digest and to `linux/amd64`; the fixed platform
reduces architecture variance. `Cargo.lock` is committed and all Cargo commands
use locked dependency resolution.

`checksums.txt` has deterministic relative paths and covers the Cargo metadata,
build recipe, README, every Rust source file, and the Wasm artifact. It is an
integrity and exact-file-set check. It does not by itself prove that the binary
was derived from the source; the clean CI rebuild and byte comparison provide
that guarantee.

## Verification layers

Format and test this standalone Rust crate with the pinned Rust 1.86 toolchain
from the repository root:

```bash
cargo +1.86.0 fmt --manifest-path harness/wasm_query_allowlist/Cargo.toml -- --check
cargo +1.86.0 test --manifest-path harness/wasm_query_allowlist/Cargo.toml --locked
```

Verify the manifest and run locked unit tests in the pinned container:

```bash
make -C harness/wasm_query_allowlist check CONTAINER_ENGINE=docker
```

Do not use `go test ./app -run '^TestWasmGrpcForwardMarketplaceQueryAllowlist$'`
as verification against the pinned Gonka commits: that test is absent there,
and Go can return success with `[no tests to run]` when `-run` matches nothing
([official Go test case](https://go.dev/src/cmd/go/testdata/script/test_skip.txt)).
A full-app allowlist regression must first be restored as runner-owned
out-of-tree test code or committed to a supported upstream source revision,
with a guard that confirms the test actually ran.

## Updating the fixture

1. Change Rust source or dependencies and update `Cargo.lock` with Rust 1.86.
2. Run `cargo +1.86.0 fmt --manifest-path harness/wasm_query_allowlist/Cargo.toml -- --check` and `cargo +1.86.0 test --manifest-path harness/wasm_query_allowlist/Cargo.toml --locked`.
3. Run `make -C harness/wasm_query_allowlist build CONTAINER_ENGINE=docker`.
4. Run `make -C harness/wasm_query_allowlist check CONTAINER_ENGINE=docker`.
5. Rebuild again and require no diff in `p0_probe.wasm` or `checksums.txt`.
6. Review and commit source, lockfile, build files, Wasm, and manifest together.

Changing Rust source without rebuilding leaves tests executing stale bytecode;
the exact-set manifest check and clean CI rebuild are both mandatory defenses.

## Safety

Never deploy this artifact to a public or production network. The probe is not
audited, its raw-query surface exists specifically for adversarial tests, and it
has no marketplace authorization or business logic. Production packaging and
deployment configuration must not reference `artifacts/p0_probe.wasm`.
