# Network Compose Fragments and Genesis Provisioner (`harness/network`)

Runner-owned Docker Compose fragments and the genesis provisioner for coverage
id `B3` of `foreign-native-preservation`. `prepare_network_root` in
[`scripts/external_harness.py`](../../scripts/external_harness.py) copies them
into the network work directory (`GONKA_REPO_ROOT = <work>/network-root`) for
live Testermint tasks; they never share a path with an upstream Gonka file.

All files in this directory are hashed into `lock.network.config_hashes` and
`lock.external_tests.trees` (`NETWORK_FILES` and `EXTERNAL_TEST_DIRS` in
[`forward_e2e/execution/planner.py`](../../forward_e2e/execution/planner.py)),
so editing any of them invalidates existing locks.

## Contents

The destination column is the path inside `<work>/network-root`
(`GENERATED_NETWORK_FILES`); the last column says when the file is copied.

| File | Destination | Role | Copied |
|---|---|---|---|
| [`ownership.yml`](ownership.yml) | `local-test-net/ownership.yml` | Attaches the run ownership label `io.gonka.a8.run-id=${E2E_RUN_ID}` (the only ownership label; `OWNERSHIP_LABEL` in `scripts/external_harness.py` and `harness/container_control.py`) to every service of the pair's base compose set, so container control and cleanup act only on containers that provably belong to this run. | always, for every pair |
| [`nats.yml`](nats.yml) | `local-test-net/nats.yml` | Binds the API's NATS state to its own per-pair directory `prod-local/nats/${KEY_NAME}` and sets a bounded `restart: on-failure:3` policy, replacing what the removed upstream `DockerGroup.kt` patch used to write at run time. | always, for every pair |
| [`foreign-native-genesis.yml`](foreign-native-genesis.yml) | `local-test-net/foreign-native-genesis.yml` | Runs the provisioner as the genesis `chain-node` command from the separate mount `./a8/genesis:/a8-provision:ro` (relative to `--project-directory`, i.e. the network root) and passes the fixed `B3` fixture as `A8_B3_FOREIGN_*` environment. Upstream `/root/init-docker-genesis.sh` inside the image is left untouched. | `B3` only, genesis pair only |
| [`genesis/foreign-native-genesis-provision.sh`](genesis/foreign-native-genesis-provision.sh) | `a8/genesis/foreign-native-genesis-provision.sh` | Replays the upstream `inference-chain/scripts/init-docker-genesis.sh` sequence step by step and adds `12345ua8b3foreign` to the fixed `B3` account with one extra `inferenced genesis add-genesis-account`. It saves `genesis-before-b3.json`, `genesis-after-b3.json`, `genesis-final.json` (each with a `.sha256`) and `b3-provision.json` (schema `a8.b3-provision/1`, with the exact command and the hashes) under `$STATE_DIR/a8-provision`, which is `prod-local/genesis/a8-provision` on the host (`PROVISION_DIR_IN_PROD_LOCAL`). | `B3` only |

The `B3` fixture values (`gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny`,
`ua8b3foreign`, `12345`) are repeated in `foreign-native-genesis.yml`, in
`scripts/external_harness.py` (`B3_FOREIGN_*`) and in the Kotlin constants of
`MarketplaceContractAcceptanceTests`; they must stay equal.

## How the provisioner is pinned and checked

- **Pinned upstream script.** The provisioner is derived from
  `init-docker-genesis.sh` at Gonka `c33c9eaa5bc40c53b564159b5e1534bbfdab8a08`
  (sha256 `02355d2c…dd1e`). `prepare_network_root` calls
  `check_genesis_provisioner_compatible` before copying anything for a `B3`
  run; it hashes the selected Gonka's copy of that script and refuses with
  `GENESIS_PROVISIONER_INCOMPATIBLE` if it differs. The provisioner is never
  auto-adapted, because a changed upstream script could silently skip or
  reorder a step. A generated file whose destination coincides with an
  upstream path is refused as well (`NETWORK_GENERATED_COLLISION`).
- **Collected into evidence.** After the run, `collect_b3_genesis_evidence`
  copies the four files from the provision directory into the task's
  `evidence/genesis/` and writes `b3-genesis-verification.json` next to them.
  A missing file is a finding (`B3_GENESIS_EVIDENCE_MISSING`), never a
  skipped check; the hashes recorded in `b3-provision.json` must match the
  copied bytes and its `derived_from_sha256` must equal the pinned upstream
  hash.
- **Exact delta.** `verify_b3_genesis_delta` requires that the before/after
  pair differs by exactly the fixture coin on exactly the fixture address and
  nothing else; `verify_b3_final_genesis` requires that the final genesis
  still carries it (otherwise `B3_FINAL_SUPPLY_NOT_EXACT` /
  `B3_FINAL_ACCOUNT_MISSING`).

See [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/evidence.md`](../../docs/evidence.md) for how
`network/network-manifest.json` and the `genesis/*.json` files are verified in
the suite.
