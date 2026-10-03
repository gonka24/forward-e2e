// Go boundary probes -- TEMPLATE module file.
//
// This module lives in the runner, never in Gonka. It is completed at build
// time, inside the throw-away Docker build of scripts/run_go_boundary.py,
// from the SELECTED Gonka's inference-chain/go.mod:
//
//   * entire module graph     <- copied from Gonka's go.mod
//   * module name             <- changed to this runner-owned harness
//   * require + replace       github.com/productscience/inference => ../inference-chain
//                             (Gonka's read-only sources in the build image)
//   * every replace directive <- copied verbatim from Gonka's go.mod (replace
//                             directives are not inherited from dependencies,
//                             so without them the probe would link different
//                             cosmos-sdk/store code than the node)
//   * go.sum                  <- copied from Gonka's go.sum
//
// Go's minimal version selection starts from Gonka's exact requirements, so the
// probe uses Gonka's dependency versions; the
// script records `go list -m all` for both modules and fails on any version
// difference. Gonka's go.mod/go.sum are hashed before and after and are never
// written: `go mod edit` and `go test -mod=mod` run on this module only.
module github.com/gonka24/forward-e2e-go-boundary

go 1.24
