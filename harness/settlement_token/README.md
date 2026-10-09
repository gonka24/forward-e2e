# Mainnet USDT binary fixture

`mainnet-usdt.wasm` is a historical download of Gonka mainnet code 114,
SHA-256 `5833f840d5fde0f5eb179b09a08cc8b7c2cff8da86df647a92f63917f1f31aca`.
It is a production binary fixture, not a receipt of local E2E execution.

Contract: `gonka15ggwj9un6qrmu4nj5ev6l7kpdcr00td03ff2mmj4cyhl8u8vjd2qnl3hgk`.
Saved responses were collected on 2026-10-09 from
`https://node2.gonka.ai:8443/chain-api`; `code-info-node1.json` is the
independent code checksum observation from node1. API routes were
`/cosmwasm/wasm/v1/code/114`, `/cosmwasm/wasm/v1/contract/<address>`, and
`/cosmwasm/wasm/v1/contract/<address>/smart/<base64-query>`.

The mainnet contract can be migrated by governance. These files do not
establish its current identity indefinitely. Recheck mainnet before a pilot.
The wrapped-token artifact committed at the selected Gonka source revision
has a different hash; no source-to-binary equivalence is claimed here.

Local initialization uses fresh test balances and a local token-state admin.
The local instance has no Wasm migration admin and requests no minter at
instantiate. Record the actual token, minter and bridge queries from the
local chain; historical mainnet responses cannot stand in for them.

The binary and observation files are verified by the stdlib-only helper
`forward_e2e/suite/settlement_token.py`. They will be included in the runner's
locked external test assets. Select `E2E_SETTLEMENT_TOKEN=mainnet-usdt` when
planning a native run; see `docs/operations.md`. The existence of this fixture
does not establish a completed native run.
