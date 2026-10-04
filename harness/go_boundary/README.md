# Go Boundary Fixture (`harness/go_boundary`)

Standalone Go module executed by [`scripts/run_go_boundary.py`](../../scripts/run_go_boundary.py)
for the `go-query-error-classification` (legacy alias `go-boundary`) catalog task.

## Purpose

Verifies deterministic error classification and fault-injection plan boundaries in
[`query_faults/`](query_faults/) against the selected Gonka checkout's
`inference-chain` Go module without modifying `<gonka>/inference-chain/go.mod` or
`<gonka>/inference-chain/go.sum`. The two tests that must pass are named in
`REQUIRED_TESTS` of the driver and repeated in `GO_BOUNDARY_REQUIRED_TESTS` of
[`forward_e2e/suite/verifier.py`](../../forward_e2e/suite/verifier.py):
`TestToQuerierResultClassifiesVMSystemErrors` and `TestStrictPlanValidation`.

## Execution Model

Everything below is `main` in `scripts/run_go_boundary.py`; the adapter invokes
it as `python3 scripts/run_go_boundary.py <gonka_dir> <task evidence dir>`.

1. `validate_paths` refuses an output directory inside the Gonka snapshot or
   inside the runner checkout; the Gonka checkout is fingerprinted with
   `forward_e2e/suite/source_snapshot.py` (loaded by file path) and must be a
   pristine snapshot of the selected commit before anything runs.
2. The SHA-256 of `<gonka>/inference-chain/go.mod` and `go.sum` is recorded on
   the host (`gonka_go_hashes`).
3. `generate_dockerfile` takes the prefix of Gonka's own
   `inference-chain/Dockerfile` up to `ARG LDFLAGS` — it must contain the
   pinned builder `golang:1.24.2-alpine3.21`, otherwise the driver refuses —
   and appends a stage that copies this directory in as a named build
   context (`--build-context harness=…`), completes the harness `go.mod`
   from Gonka's (`go mod edit`: module `github.com/gonka24/forward-e2e-go-boundary`,
   a local `replace` of `github.com/productscience/inference` to
   `../inference-chain`, Gonka's `go` / `toolchain` directives and every Gonka
   `replace` re-based), copies Gonka's `go.sum` next to it, and runs

   ```text
   go test -mod=mod -tags=muslc -count=1 -json ./query_faults \
     -run 'TestToQuerierResultClassifiesVMSystemErrors|TestStrictPlanValidation'
   ```

   The `-run` filter is built from `REQUIRED_TESTS`. Gonka's `go.mod`/`go.sum`
   are hashed again inside the build before and after the test
   (`gonka-go-before.sha256`, `gonka-go-after.sha256`), and
   `go list -m all` of both modules is captured.
4. The driver runs `docker build … --target boundary-evidence --output
   type=local,dest=<out>/raw` against the inner Docker daemon. **There is no
   host-Go path**: the tests always run inside the container built from
   Gonka's pinned builder, so the Go toolchain of the runner image is not
   used for this task.
5. After the build the Gonka snapshot is fingerprinted again and the host
   hashes of `go.mod`/`go.sum` are compared. `report.json` is `PASS` only when
   both required tests report `pass` in the `-json` stream, the Docker and
   `go test` exit codes are zero, Gonka's `go.mod`/`go.sum` are unchanged on
   the host and inside the container, every dependency version of the
   harness module matches Gonka's, and the snapshot shows no mutation.
6. Outputs in the task evidence directory: `build.log`, `raw/go-test.json`,
   `raw/exit-code`, `report.json` (with `test_output_sha256` over
   `raw/go-test.json`, which `verify_go_boundary_report` recomputes) plus the
   raw build-stage files (`gonka-modules.txt`, `harness-modules.txt`,
   `harness-go.mod`, `go-mod-edit.log`, the two `.sha256` files).

Proof level: `GO_BOUNDARY`. The report's own `level` field reads
"Go classification and JSON roundtrip, not FFI"; the task is never classified
as native chain E2E. See [`docs/evidence.md`](../../docs/evidence.md) and
[`docs/coverage.md`](../../docs/coverage.md).
