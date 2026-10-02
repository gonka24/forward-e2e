# Gonka24 Forward E2E

Independent acceptance runner for the [Forward contracts](https://github.com/gonka24/forward-contracts) and the Gonka blockchain. It builds and tests explicitly selected full commit SHAs without modifying either source snapshot.

The suite contains 24 checks: 19 native Testermint scenarios and 5 contract-policy, Go and compiled-Wasm boundary checks. Boundary checks retain their actual proof level; they are not native-chain evidence.

## Getting started

Clone this repository separately from the contracts repository:

```bash
git clone https://github.com/gonka24/forward-e2e.git
cd forward-e2e
./ops/e2e/build-runner.sh --runner-sha <RUNNER_FULL_40_HEX_SHA>
./ops/e2e/run-e2e.sh list
./ops/e2e/run-e2e.sh run \
  --gonka-repo https://github.com/gonka-ai/gonka \
  --gonka-sha <GONKA_FULL_40_HEX_SHA> \
  --contracts-repo https://github.com/gonka24/forward-contracts \
  --contracts-sha <CONTRACTS_FULL_40_HEX_SHA> \
  --profile smoke \
  --output ./out/e2e
```

On Windows, build with `ops/e2e/Build-Runner.ps1 -RunnerSha <RUNNER_FULL_40_HEX_SHA>` and use `ops/e2e/Run-E2E.ps1` for run arguments. Remote runs require Docker with Compose; local-source runs also require Git. For a sibling contracts checkout use `--contracts-path ../forward-contracts`. The runner repository itself is not a contracts source.

The image is built from the selected committed runner sources. Its baked Git
commit and tree SHAs are recorded in every new plan, together with its immutable
image ID and asset hashes. Runner, contracts and Gonka are pinned independently.

Read the [operational guide](ops/e2e/README.md) for planning, full runs, replay, reporting and recovery, and the [verification runbook](ops/e2e/RUNBOOK-immutable-sources.md) for validation on a specific source pair. The image includes target build toolchains and uses a private inner Docker daemon for live runs.

## Repository boundary

| Location | Responsibility |
| --- | --- |
| `ops/a8/` | Catalog, orchestration, build provenance, evidence collection and verification |
| `ops/a8/harness/` | External Kotlin tests, network templates, container controller, Go and Wasm probes |
| `ops/e2e/` | Linux/macOS and Windows host wrappers and runbooks |
| `scripts/` | Runner-owned acceptance and boundary helpers |
| `scripts/a9_release.py` | Pinned copy of the reproducible release helper; canonical maintenance belongs to `forward-contracts` |
| `docs/reviews/` | Preserved review context and recorded fixtures; historical results do not certify new commits |

Production contracts, API/protobuf packages, Rust unit/property/`cw-multi-test` tests, release/deployment tooling and the three small Cargo test-contract fixtures remain in `forward-contracts`. The runner builds those fixtures from the selected contracts commit.

## Development checks

The core runner requires Linux (`fcntl` and POSIX file permissions). Run its offline suites on Linux, or in a Linux tools container, with Python 3.11 and Git:

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py' -v
python3 -B -m unittest discover -s ops/a8/tests -p 'test_*.py' -v
python3 -B -m unittest discover -s ops/a8/integration_tests -p 'test_*.py' -v
```

Windows wrapper checks have a separate CI job. CI does not start a live chain. Passing offline tests does not prove a full E2E run or production readiness.

Read [AGENTS.md](AGENTS.md) and the [architecture guide](ops/a8/README.md) before changing the runner. New images and changed runner hashes require new plans; old image-bound locks are not rebound to this repository.

## Extraction provenance

This repository was extracted from `forward-contracts` commit `d637eea5432506d60c90c1d8436c67b93802d829` after [PR #1](https://github.com/gonka24/forward-contracts/pull/1). [EXTRACTION.json](EXTRACTION.json) records each source file's original SHA-256. The original development history remains in that repository. At extraction, runner executable assets and recorded evidence retained their original bytes; subsequent runner fixes are tracked in this repository's Git history. The extraction hashes remain a record of the original source bytes.

The copy of `scripts/a9_release.py` is intentionally runner-owned and hashed into every run lock. Updating the canonical helper does not silently update this copy: import a reviewed version, update its source record, run its offline tests, rebuild the runner and create a new plan.

## License

Original Gonka24 material retains [BUSL-1.1](LICENSE) and its existing publication dates; extraction does not restart them. See [licensing policy](docs/licensing.md) and [third-party scope](THIRD_PARTY.md). Upstream-derived material retains its applicable terms and notices.

## Authors

- Mikita Anikiyevich
- Nikolay Tverdokhlebov
- Hleb Dapkiunas
