# Go Boundary Fixture (`harness/go_boundary`)

Standalone Go module executed by [`scripts/run_go_boundary.py`](../../scripts/run_go_boundary.py)
for the `go-query-error-classification` (legacy alias `go-boundary`) catalog task.

## Purpose

Verifies deterministic error classification and fault-injection plan boundaries in
[`query_faults/`](query_faults/) against the selected Gonka checkout's
`inference-chain` Go module without modifying `<gonka>/inference-chain/go.mod` or
`<gonka>/inference-chain/go.sum`.

## Execution Model

1. [`scripts/run_go_boundary.py`](../../scripts/run_go_boundary.py) records the
   SHA-256 of `<gonka>/inference-chain/go.mod` and `go.sum` before execution.
2. It copies `harness/go_boundary` into a temporary workspace outside the Gonka
   source snapshot and adds a local `replace` directive pointing to the
   read-only Gonka checkout.
3. It runs `go test -json -count=1 ./query_faults -run ^TestA8GoBoundary` (inside
   Gonka's pinned Go builder container or host Go when explicitly selected) and
   verifies that `<gonka>/inference-chain/go.mod` and `go.sum` remain unchanged
   after the run.
4. It writes `report.json`, `raw/go-test.json`, `raw/exit-code`, and `build.log`.

Proof level: `GO_BOUNDARY` (never classified as native chain E2E). See
[`docs/evidence.md`](../../docs/evidence.md) and [`docs/coverage.md`](../../docs/coverage.md).
