# Pinned Runner Toolchain and Runtime Stack

This document records the pinned toolchains, container digests, and runtime
dependencies owned by the `gonka24/forward-e2e` runner repository.

For the normative Rust workspace crate matrix (`cosmwasm-std`, `cw-storage-plus`,
`cw2`, `cw20`, `prost`, `cw-multi-test`, and transitive lockfile constraints) of
the Marketplace contracts under test, see
[`forward-contracts/VERSIONS.md`](https://github.com/gonka24/forward-contracts/blob/58f41c2e6976002fe25b8ec1548d8599015c07e6/VERSIONS.md)
(linked at the contracts revision this document was last checked against;
`main` moves, so read the file at the contracts SHA your plan selects).

---

## 1. Runner Identity and Base Image

| Component | Pinned Value | Where Pinned | Purpose |
|---|---|---|---|
| Runner version | `forward-e2e-runner/3.0.0` | [`ops/runner/RUNNER_VERSION`](ops/runner/RUNNER_VERSION) | Independent runner version recorded in `run.lock.json` (`lock.runner.runner_version`); `assert_runner_matches_lock` refuses to *execute* a lock recorded under another version. The string does not classify packages: `report` / `recover` grade a package written by `a8-runner/2.0.0` from its documents alone (`classify_provenance_model` in `forward_e2e/execution/outcome.py`), so such a package is historical only if its lock or evidence declares a prepared-build or overlay model; its lock is merely no longer executable here. |
| Plan lock schema | `e2e/run-lock/2` | [`forward_e2e/execution/runlock.py`](forward_e2e/execution/runlock.py) | Immutable-source execution plan schema |
| Evidence model | `a8.evidence/e2e-immutable-source/2` | [`forward_e2e/suite/evidence_model.py`](forward_e2e/suite/evidence_model.py) | Mandatory evidence model for unmodified source checkouts |
| Base OS image | `debian:bookworm-20260918-slim@sha256:f3034a6ec3c1205360777c4aae76234998866ad18806ae62b63a3f84ccad782b` | [`ops/runner/Dockerfile`](ops/runner/Dockerfile) | Immutable Debian 12 `linux/amd64` base layer |
| Debian APT archive snapshot | `20260918T000000Z` | [`ops/runner/Dockerfile`](ops/runner/Dockerfile) | Freezes Debian Bookworm and Bookworm-Security package indexes |

---

## 2. Embedded Runner Toolchain (`ops/runner/Dockerfile`)

Every tool inside the runner container is installed from a checksum-verified
archive or pinned snapshot package:

| Tool | Pinned Version | Verification / SHA-256 | Role in Runner |
|---|---|---|---|
| Python | Debian Bookworm `python3` (`3.11+`) | Debian snapshot `20260918T000000Z` | Executes `forward_e2e.*`, `scripts/*`, and `vendor/contract_release/release.py` |
| Eclipse Temurin JDK / JRE | `21.0.12.1.0+1-0` (`amd64`) | JDK `.deb`: `1c4fd4033deea86e69457366bc08a1a2d0056afad2745248917eafa84bd4e11d`<br>JRE `.deb`: `6c36c5cae76391a558926db5b8df2114dc41ed2ba889edf962370083944588b3` | Compiles upstream Testermint classpath and runs external Kotlin harness |
| Docker CE & CLI | `29.8.1-1~debian.12~bookworm` | `docker-ce`: `3e38704938b78358563164cf419d00598e32a2119efc7cea80bdec2249efaf63`<br>`docker-ce-cli`: `ff812c5853c52ef120ec73132320805d179a376e42785085e2053ce7f2479860` | Private inner `dockerd` and CLI inside the runner container |
| `containerd.io` | `2.3.6-1~debian.12~bookworm` | `3dacccefaa69b13307813a3a2e1bdc65c8c69d5cf9da3a9e28b53e2998d195d7` | Inner container runtime for `dockerd` |
| Docker Buildx plugin | `0.37.1-1~debian.12~bookworm` | `cf36da1a4c31287fef2c0cb5413aa660f56d4ec183b4d24dc47cd83ebef0a2c6` | Builds chain images and contract release artifacts |
| Docker Compose plugin | `5.5.1-1~debian.12~bookworm` | `0455e6a54bcc07b1bcb5d92d9e6e77dedc485433c87c1b75572e6d837a96e459` | Orchestrates Testermint local-test-net clusters |
| Node.js LTS | `22.23.3` (`linux-x64`) | `df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de` | Runs `scripts/test_wasm_query_boundary.mjs` (`wasm-abi-boundary`) |
| Go | `1.26.8` (`linux-amd64`) | `d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b` | Runner Go toolchain. The `go-query-error-classification` task (legacy alias `go-boundary`) does not use it: `scripts/run_go_boundary.py` compiles and runs the Go tests inside a Docker build derived from Gonka's own `inference-chain/Dockerfile` (pinned `golang:1.24.2-alpine3.21` builder). |
| `rustup-init` | `1.28.2` (`x86_64-unknown-linux-gnu`) | `20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c` | Installs pinned Rust `1.81.0` toolchain |
| Rust toolchain | `1.81.0` (`wasm32-unknown-unknown`, `clippy`, `rustfmt`) | Installed via verified `rustup-init` | Compiles contract tests, ABI probe Wasm, and harness fixtures |
| `cosmwasm-check` | `2.2.2` | `cargo install cosmwasm-check --version 2.2.2 --locked` | Validates compiled CosmWasm 2.2 bytecode |

---

## 3. Optimizer and Runtime Support Images

Pinned in [`forward_e2e/execution/compat.py`](forward_e2e/execution/compat.py),
[`vendor/contract_release/release.py`](vendor/contract_release/release.py), and
[`harness/wasm_query_allowlist/build.sh`](harness/wasm_query_allowlist/build.sh):

| Image | Reference and Digest | Used By |
|---|---|---|
| CosmWasm optimizer (`0.16.1`, Rust `1.81.0`) | `cosmwasm/optimizer:0.16.1@sha256:b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e` | Production contract release build (`vendor/contract_release/release.py`) |
| CosmWasm optimizer (`0.17.0`, Rust `1.86.0`) | `cosmwasm/optimizer:0.17.0@sha256:7e0b9229c1a4118d0c9a2af2e7f5d95a91f264c26a2ce5681c779926e74d7f85` | Gonka `wrapped-token` build step and standalone `harness/wasm_query_allowlist` fixture |
| PostgreSQL (`18.1-bookworm`) | `postgres:18.1-bookworm@sha256:cc9f4143a8d2fa8cf3749d0cb4d26ecf2d53a77a2ac807e9ebd67ae22426221a` | Testermint runtime support service (`runtime_external_images.postgres`) |
| CoreDNS (`1.11.1`) | `coredns/coredns:1.11.1@sha256:1eeb4c7316bacb1d4c8ead65571cd92dd21e27359f0d4917f1a5822a73b75db1` | Testermint cluster DNS (`runtime_external_images.test-dns`) |

---

## 4. Compatibility Targets and Verified Baseline Commits

Compatibility adapters in [`forward_e2e/execution/compat.py`](forward_e2e/execution/compat.py)
measure source trees by structural markers and record verified commits when
matched:

| Target Role | Adapter ID | Verified Commits | Expected Runtime / Toolchain |
|---|---|---|---|
| Gonka chain | `gonka-immutable-source-v2` | `e86e4899bd8cf52d1ad4766c811f65230b2f9296`<br>`c33c9eaa5bc40c53b564159b5e1534bbfdab8a08` | `wasmd v0.54.2`, `wasmvm/v2 v2.2.4` (measured dynamically from `<gonka>/inference-chain/go.mod`) |
| Forward Marketplace contracts | `contracts-marketplace-workspace-v1` | `7497304e5dc6bf48accdd8c91549bc22de6997fc` (and any commit matching workspace markers) | Rust `1.81.0`, `cosmwasm-check 2.2.2`, `cosmwasm/optimizer:0.16.1` |

---

## 5. Known Chain-Side Advisory Note

Upstream Gonka commits using `wasmd v0.54.2` are subject to
[CWA-2025-007](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2025-007.md)
(`wasmd <= v0.54.2` affected; `v0.54.3` patched for recursive submessage reply
depth limits). Because `wasmd` is a chain dependency in `gonka-ai/gonka` rather
than a contract crate dependency, remediating it requires selecting a patched
Gonka commit and re-running the E2E validation suite against the new SHA pair.
