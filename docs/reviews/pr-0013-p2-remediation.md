# PR #13 — Evidence for Remediation of P2 Findings

Date: 2026-09-07. Scope: Python A9 deployment/verification tooling. Contracts,
economics, pinned dependencies, and native Gonka were not changed.

## Decision and Invariants

1. Factory instantiation always contains exactly one `--no-admin` flag and never
   contains `--admin`. This matches `parseInstantiateArgs` in official wasmd
   `v0.54.2`: absence of both options or selecting both simultaneously is an
   error. Following deployment, actual `ContractInfo.admin=None` is still
   verified on-chain.
2. Standalone `verify-deal` first invokes shared `verify-deployment` with
   config/manifest/receipt paths. Therefore, Deal-specific checks cannot be
   reached prior to verifying local artifacts, all hash bindings, chain ID,
   Factory, and CW20 decimals. Subsequently, validations of Deal
   checksum/code/admin, immutable terms/config, pinned Gonka SHA, and exact
   Factory `(Host, E)` index are preserved.
3. `verify-deal` remains strictly read-only: regression helpers independently
   assert the absence of any `tx wasm` invocations and the absence of a
   successful `factory_indexed=true` result across all negative scenarios.

## Specific Regression Tests

- `test_prepare_is_read_only` — generated instantiate command contains exactly
  one `--no-admin`, contains no `--admin`, and dry-run submits no transactions.
- `test_fake_cli_rejects_missing_or_conflicting_admin_choice` — fake CLI
  rejects absence of both flags as well as their simultaneous presence.
- `test_deploy_stores_both_codes_instantiates_without_admin_and_writes_verified_receipt`
  — complete successful fixture deployment passes the new CLI guard and
  post-deploy verification.
- `test_verify_deal_cli_success_runs_common_checks_and_is_read_only` — success
  executes via node status, shared deployment evidence, and Deal checks without
  transactions.
- `test_verify_deal_cli_rejects_wrong_node_chain_id`.
- `test_verify_deal_cli_rejects_tampered_local_wasm`.
- `test_verify_deal_cli_rejects_stale_manifest_or_config_hash_binding`.
- `test_verify_deal_cli_rejects_receipt_from_other_deployment_config_or_manifest`.
- `test_verify_deal_cli_rejects_wrong_factory_checksum_code_admin_or_config`.
- `test_verify_deal_cli_rejects_wrong_cw20_decimals`.
- `test_verify_deal_cli_rejects_wrong_deal_code_admin_terms_or_factory_index`.

All negative CLI cases require a non-zero exit code, an empty transaction call
set, and the absence of a successful verification result.

## Model Boundary

Fixtures verify orchestration, fail-closed ordering, argv formatting, and
expected JSON shapes. They do not prove compatibility with compiled Gonka
forks, keyring/RPC, real event encoding, transaction semantics, or golden
claim/streamvesting E2E. Real deployment was not executed for this remediation.
These external gates remain under workstream B and are not converted into
production-readiness evidence.
