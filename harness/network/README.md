# Network Compose Fragments and Genesis Provisioner (`harness/network`)

Runner-owned Docker Compose fragments and the B3 genesis provisioner copied into
`<work>/network-root/local-test-net/` during live Testermint tasks.

All files in this directory are hashed into `lock.network.config_hashes` and
`lock.external_tests.trees` (`NETWORK_FILES` and `EXTERNAL_TEST_DIRS` in
[`forward_e2e/execution/planner.py`](../../forward_e2e/execution/planner.py)).

## Contents

| File | Role |
|---|---|
| [`ownership.yml`](ownership.yml) | Attaches the run ownership label `io.gonka.a8.run-id=${E2E_RUN_ID}` (the only ownership label; `OWNERSHIP_LABEL` in `scripts/external_harness.py` and `harness/container_control.py`) to every service of the pair's base compose set, so container control and cleanup act only on containers that provably belong to this run. |
| [`nats.yml`](nats.yml) | Configures NATS / join-stack readiness and bounded restart policies outside upstream `DockerGroup.kt`. |
| [`foreign-native-genesis.yml`](foreign-native-genesis.yml) | Mounts the external genesis provisioner at `/a8-provision` for the `foreign-native-preservation` (`b3-foreign-native`) scenario only. |
| [`genesis/foreign-native-genesis-provision.sh`](genesis/foreign-native-genesis-provision.sh) | Replays the upstream `init-docker-genesis.sh` sequence (verified by SHA-256) and adds `12345ua8b3foreign` to the fixed B3 test account via `inferenced genesis add-genesis-account`, recording `genesis-before.json` and `genesis-after.json`. |

See [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/evidence.md`](../../docs/evidence.md) for how `network/network-manifest.json`
and `genesis/*.json` are verified.
