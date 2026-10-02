> This is the inherited target build/version reference from the extraction
> commit in EXTRACTION.json. Production Cargo metadata remains in
> [forward-contracts](https://github.com/gonka24/forward-contracts/blob/main/VERSIONS.md).
> The runner toolchain is pinned in
> [ops/a8/Dockerfile](ops/a8/Dockerfile), and its independent version is in
> [ops/a8/RUNNER_VERSION](ops/a8/RUNNER_VERSION).

# Pinned Development Stack

Decision Date: 2026-09-06

This document is the normative version matrix for the Gonka Forward Marketplace. When creating the Rust workspace, library versions from the table must be transferred into root `Cargo.toml` as exact requirements of the form `=x.y.z`. The generated `Cargo.lock` is committed to the repository.

## Compatibility Source

- Gonka repository: <https://github.com/gonka-ai/gonka>
- Pinned Gonka commit: `379bebced638aeb5e6077bfd51c986f898443832`
- Gonka `wasmd`: `v0.54.2`
- Gonka `wasmvm/v2`: `v2.2.4`
- Reconciled Gonka contract: `inference-chain/contracts/wrapped-token`

The pinned commit serves as the source of truth for protobuf definitions and chain-specific query paths. Updating the commit is a separate architectural decision requiring re-verification of generated bindings and golden E2E tests.

## Rust and Build Toolchain

| Component | Pinned Version | Where Pinned | Rationale |
|---|---:|---|---|
| Rust toolchain | `1.81.0` | `rust-toolchain.toml` | Matches Rust inside optimizer `0.16.1`; local and release builds use the same compiler generation |
| Security tools host Rust | `1.88.0` | local setup/CI | Dedicated minimal toolchain solely for `cargo-audit` and `cargo-deny`; never compiles contract Wasm |
| Rust edition | `2021` | `[workspace.package]` | Stable edition used by Gonka and selected CosmWasm crates |
| Cargo resolver | `2` | `[workspace]` | Proper feature resolution for edition 2021 workspaces |
| Wasm target | `wasm32-unknown-unknown` | `rust-toolchain.toml` | Target format for CosmWasm contracts |
| CosmWasm optimizer | `0.16.1` | release script/CI | Last optimizer on Rust `1.81.0`; `0.17.0` requires CosmWasm 3 runtime |
| Optimizer image digest | `sha256:b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e` | release script/CI | Immutable reference for reproducible amd64 release builds |

Release image:

```text
cosmwasm/optimizer:0.16.1@sha256:b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e
```

`scripts/a9_release.py build` is the normative release command: it explicitly
sets `--platform linux/amd64`, runs this exact digest twice from distinct
directories, compares Wasm SHA-256 hashes, and preserves commands/results in a
versioned build manifest. CI invokes the same command rather than a duplicate copy of build logic.

## Production Dependencies

| Crate | Exact Requirement | Purpose |
|---|---:|---|
| `cosmwasm-std` | `=2.2.2` | Entry points, messages, queries, bank operations, and arithmetic types |
| `cosmwasm-schema` | `=2.2.2` | Public JSON schema and derive macros for API |
| `cw-storage-plus` | `=2.0.0` | Typed persistent storage |
| `cw2` | `=2.0.0` | Records contract name and version in state |
| `cw20` | `=2.0.0` | Standard CW20 receive/transfer/query types |
| `cw-utils` | `=2.0.0` | Common audited utility types; included only when actually used |
| `schemars` | `=0.8.22` | JSON Schema types; uniform version across public API |
| `serde` | `=1.0.219` | Contract messages/state serialization; `default-features = false`, feature `derive` |
| `thiserror` | `=1.0.69` | Typed contract errors without manual boilerplate |
| `prost` | `=0.12.6` | Encode/decode generated Gonka protobuf messages |
| `prost-derive` | `=0.12.6` | Derive `prost::Message` for generated types |

`cosmwasm-std 3.x` is deliberately not used. Pinned Gonka already contains individual contracts on 3.0.1, but CW20 interface crates remain on CosmWasm 2.x. Selecting a single 2.2.2 graph avoids two incompatible sets of `cosmwasm_std` types and aligns with Gonka `wrapped-token`, which already uses CW20 and custom gRPC.

Feature policy for `cosmwasm-std`: include only strictly necessary chain capabilities. For current design, `cosmwasm_2_0` is sufficient; feature `staking` is unneeded. The presence of runtime `2.2.4` does not imply that the contract must enable all `cosmwasm_2_2` APIs.

## Test Dependencies

| Crate | Exact Requirement | Purpose |
|---|---:|---|
| `cw-multi-test` | `=2.5.0` | Factory/Deal and CW20 integration tests; its manifest uses CosmWasm `2.2.2` graph |
| `cw20-base` | `=2.0.0` | Mock CW20 token for tests only, feature `library` |
| `proptest` | `=1.5.0` | Property tests for financial invariants; MSRV `1.65` compatible with Rust `1.81.0` |
| `serde_json` | `=1.0.140` | Test fixtures and JSON interface verification |

For `cw-multi-test`, feature `cosmwasm_2_2` is enabled. This brings the test model closer to the chosen API, but does not turn it into a full Gonka chain: custom gRPC and streamvesting still require real-chain E2E.

Why `cw-multi-test = "2.2.2"` is not used: such a specification is parsed as a semver range, which is why Cargo in Gonka resolves it to the actually published `2.5.0`. Version `2.2.2` of the crate itself does not exist in the registry. We pin the actual version from Gonka's `Cargo.lock`, rather than an ambiguous range from `Cargo.toml`.

## Security and Validation Tools

These CLIs do not enter Wasm and are not contract dependencies, but their versions are pinned for identical behavior locally and in CI.

| Tool | Version | Rationale |
|---|---:|---|
| `cosmwasm-check` | `2.2.2` | Verifies Wasm against CosmWasm 2.2 generation |
| `cargo-audit` | `0.22.2` | RustSec scan supporting current advisory records and CVSS 4.0 |
| `cargo-deny` | `0.20.2` | Advisories/licenses/bans/sources supporting current advisory records and CVSS 4.0 |

## Protobuf Generator Tooling

These crates belong to the separate host-only `tools/proto-gen` workspace and do not
enter the dependency graph or contract Wasm.

| Tool / Crate | Version | Rationale |
|---|---:|---|
| `prost-build` | `0.12.6` | Generates Rust from `.proto` at the same version as runtime `prost` |
| `protoc-bin-vendored` | `3.2.0` | Supplies pinned `protoc` for supported host OS without system install |
| `indexmap` | `2.7.1` | Constrains allowed `prost-build` branch before transitive manifest moved to edition 2024 |
| `tempfile` | `3.14.0` | Preserves host generator compatibility with Cargo/Rust `1.81.0` |

For one-off bootstrap export, Buf CLI `1.72.0` was used; its Windows
x86_64 SHA-256 and exact upstream Buf module commits are recorded in
`packages/gonka-proto/proto/PROVENANCE.toml` and `upstream.buf.lock`. Standard
re-generation does not invoke Buf and does not download upstream `.proto`; on a clean
machine, Cargo fetches generator crates once from a separate lockfile.

These two security CLIs are built with a separate Rust `1.88.0`. They analyze `Cargo.lock` but do not participate in contract compilation and cannot alter contract Wasm. Initially considered `cargo-audit 0.21.2` and `cargo-deny 0.17.0` are no longer suitable: the active RustSec database contains CVSS 4.0 records that their parsers cannot read. Using a stale database snapshot would be far more hazardous than isolating host security tooling from the contract compiler.

## Pinning Policy

1. All direct dependencies are specified via `[workspace.dependencies]` with `=version`.
2. Member crates use `{ workspace = true }` and do not specify their own versions of the same libraries.
3. `Cargo.lock` is committed and all CI/release commands use `--locked`.
4. Git dependencies are prohibited without a separate decision record specifying an exact `rev`.
5. Updates are performed via separate PR: one logical set of versions, changelog/advisory review, full test suite run, and new optimizer hash.
6. If a security patch cannot be obtained without changing major/runtime capabilities, work halts for an explicit risk decision; Cargo never silently updates such dependencies.

### Initial Transitive Lockfile Constraints

Smoke-resolution on 2026-09-06 demonstrated that several formally permissible new transitive releases already require Cargo/Rust supporting edition 2024 or Rust 1.85+. Therefore, upon initial `Cargo.lock` creation, verified versions were additionally retained:

| Transitive Package | Verified Version | Why Fresh Resolution Cannot Be Retained |
|---|---:|---|
| `cosmwasm-core` | `2.2.2` | `2.3.4` exceeds the chosen CosmWasm patch line |
| `cosmwasm-crypto` | `2.2.2` | Must remain in the same patch graph as `cosmwasm-std` |
| `cosmwasm-derive` | `2.2.2` | Must remain in the same patch graph as `cosmwasm-std` |
| `zeroize` | `1.8.1` | Version from verified Gonka lockfile |
| `zeroize_derive` | `1.4.2` | `1.5.0` manifest requires edition 2024-capable Cargo |
| `thiserror` (2.x branch) | `2.0.12` | Fresh `2.0.20` pulls `syn 3`, unavailable in Cargo 1.81 |
| `prost` (0.14.x branch) | `0.14.1` | Fresh `0.14.4` requires Rust 1.85; this branch is introduced via `cw-multi-test` |
| `prost-derive` (0.14.x branch) | `0.14.1` | Must match transitive `prost 0.14.1` |
| `rmp` | `0.8.14` | `0.8.15` manifest requires edition 2024-capable Cargo |
| `tempfile` | `3.14.0` | Fresh `3.27.0` pulls `getrandom 0.4`, whose manifest requires edition 2024-capable Cargo |
| `bech32` | `0.11.0` | Verified version from Gonka lockfile |

These are not new production dependencies. The constraints live in committed `Cargo.lock`; the table explains why automated updates to these rows require repeated smoke-checking.

## What Has Been Specifically Verified

- Pinned Gonka commit indeed contains `wasmd v0.54.2` and `wasmvm/v2 v2.2.4`.
- Gonka `wrapped-token/Cargo.lock` resolves `cosmwasm-std/schema 2.2.2`, `cw-multi-test 2.5.0`, `prost 0.12.6`, and a separate transitive `prost 0.14.1` within the test graph.
- `cw-multi-test 2.5.0` supports feature `cosmwasm_2_2` and directly depends on CosmWasm `2.2.2`.
- Optimizer `0.16.1` uses Rust `1.81.0`; its amd64 digest was retrieved from Docker registry.
- `cargo-audit 0.22.2` and `cargo-deny 0.20.2` successfully read the active RustSec database; a separate Rust `1.88.0` was installed for them.
- The complete smoke graph after pinning the listed transitive versions successfully passed `cargo +1.81.0 check --all-targets --locked`.
- The full workspace suite passed via `cargo +1.81.0 test --workspace --all-features --locked`: `22 passed`, `0 failed`.
- `cargo tree --duplicates` verified: two versions of `prost` are expected (`0.12.6` for Gonka and `0.14.1` solely via `cw-multi-test`); production CosmWasm graph remains on `2.2.2`.
- Release Wasm of both contracts builds on Rust `1.81.0` and passes `cosmwasm-check 2.2.2`.

## Known Chain-Side Release Blocker

The pinned Gonka commit uses `wasmd v0.54.2`. Official
[CWA-2025-007](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2025-007.md)
identifies `wasmd <= v0.54.2` as affected and `v0.54.3` as the patched version of
this branch: reply handlers can recursively generate submessages without proper
depth limits, which could terminate node processes due to stack overflow.

This is not a Marketplace Rust dependency and cannot be fixed by updating
`cosmwasm-std`. Prior to release, a coordinated Gonka patch/backport and
attestation of the running binary are required. Following selection of a new Gonka release SHA,
the version matrix, allowlist, protobuf snapshot, and golden E2E tests must be verified anew.

`v0.54.3` resolves specifically CWA-2025-007, but is not declared a universally
safe production version. Before agreeing on an upgrade target, the full list of newer advisories must be re-checked. For example,
[CWA-2026-001](https://github.com/CosmWasm/advisories/blob/main/CWAs/CWA-2026-001.md)
lists `wasmd v0.54.5`/`wasmvm v2.2.5` as affected and
`v0.54.6`/`v2.2.6` as patched; pinned `v0.54.2`/`v2.2.4` are not listed in its affected list,
hence applicability of that distinct advisory is not asserted here.
