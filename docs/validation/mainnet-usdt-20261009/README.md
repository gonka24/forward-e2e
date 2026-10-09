# Native mainnet USDT compatibility validation

All six selected native scenarios passed using the exact historical Gonka mainnet USDT Wasm. The complete exported run was regraded by the pinned runner, and a separate receipt/arithmetic audit passed. This validates local compatibility for the pinned versions below; acceptance remains `NOT_REVIEWED`.

## Pinned inputs

| Input | Identity |
|---|---|
| Gonka | `136041c81ea8ff38e7620d76af66a7c7fe7eec50` |
| Forward Contracts | `4092b665c97b87f937d4c4c230870cebb038a4d8` |
| Executed runner | `06fb0bec12ff23d12c0189805ddaf82fdf26bd6b`, version 3.1.1 |
| Runner image | `sha256:fc87be23ef32678eb260fa167a863c8bfe57bdcce199f1e0d24c813dd51aa075` |
| Run | `e2e-usdt-exact-r2-20261009` |
| Lock SHA256 | `c98f98a3af0a3f3da9a0f263a7401f37d2ea3c1354e7a48019eeb67ab37e96b0` |
| Settlement Wasm SHA256 | `5833f840d5fde0f5eb179b09a08cc8b7c2cff8da86df647a92f63917f1f31aca` |

The USDT fixture was downloaded from mainnet code 114 at `gonka15ggwj9un6qrmu4nj5ev6l7kpdcr00td03ff2mmj4cyhl8u8vjd2qnl3hgk`. Its identity is historical and must be checked again before a pilot. The selected Gonka source's committed wrapped-token artifact has a different checksum; source-to-binary equivalence is not claimed.

The validation finished on 2026-10-09 at 15:36 UTC. This evidence records the executed runner commit above. The later documentation commit does not claim another execution.

## Results

| Scenario | Execution | Evidence | Cleanup |
|---|---|---|---|
| `network-unconfirmed` | PASSED | COMPLETE | CLEANED |
| `claim-expiry-positive` | PASSED | COMPLETE | CLEANED |
| `terminal-release-repeat` | PASSED | COMPLETE | CLEANED |
| `lock-exact-e` | PASSED | COMPLETE | CLEANED |
| `refund-boundary-and-vesting-addition` | PASSED | COMPLETE | CLEANED |
| `native-release-rollback-retry` | PASSED | COMPLETE | CLEANED |

Each scenario has successful JUnit evidence, an immutable-source receipt and included native transactions. The independent audit checked 221 indexed artifacts, six live contexts, 12 rejected funding attempts, two settlements, five positive role payments, two unsuccessful-deal refunds and a native Bank rollback/retry. Each task removed its 19 owned containers, three volumes and network.

The three-positive-payment scenario distributed a local 100 USDT budget as 93.353023 USDT to Host, 1.421619 USDT to the fee recipient and 5.225358 USDT back to Buyer. All three repeated role withdrawals were rejected without changing obligations or balances. The unsuccessful-deal refunds each returned exactly 10 local USDT.

Offline validation passed: runner 1398 tests with 12 skips, harness 235 tests, local source acquisition 4 tests, and focused checksum tests 21 tests. Skipped tests are not executed proof. The full native run and read-only regrade both returned exit 0 with no findings.

## Evidence package

- [Evidence release](https://github.com/gonka24/forward-e2e/releases/tag/evidence-mainnet-usdt-20261009)
- [Full archive](https://github.com/gonka24/forward-e2e/releases/download/evidence-mainnet-usdt-20261009/native-usdt-evidence.tar.gz)
- [Independent audit](native-audit.json)
- [Full-package regrade](offline-regrade.json)
- [Archive integrity receipt](archive-verification.json)

Archive: 1,264,872,818 bytes, 9,587 files including the complete original run package's 9,559 files. SHA256: `22cf6a59831bfd0b2f875e019f9e7e213f08b9382f4af07512cc02997fa93513`.

The full archive is published as a validation-evidence prerelease at the executed runner commit. The compact receipts support review of the recorded outcome; download the full package for independent regrading.

Every archive member was compared with the input file's size and SHA256, and the copied archive's SHA256 matched. The archive includes raw receipts, JUnit, manifests, immutable lock, source bundles, separate review code/results, runner build provenance and offline test logs. These live receipts are validation records, not synthetic test fixtures.

## Scope and limits

Fresh local balances, a local token-state admin, no minter and no Wasm migration admin differ from mainnet initialization. Testermint uses accelerated epochs and local participants. No user mainnet funds were used.

Ethereum bridge operations, bridge validators/reserves, mainnet migration/minter governance and a real-host pilot were not covered. The production USDT binary has no test-only transfer-failure hook; outgoing-USDT fault injection is not claimed. Native GNK Bank rollback and failed funding Send rollback were verified. The previously identified Gonka advisory CWA2025-007 remains unresolved.

The first attempt was cancelled after the harness incorrectly read the CodeInfo checksum field. Runner 3.1.1 uses the actual `checksum` field, retains the exact-Wasm checks and produces the successful R2 evidence above. The cancelled attempt is retained separately and is not passing proof.
