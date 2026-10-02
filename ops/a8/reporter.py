"""Offline report generator for A8 suites.

Renders and atomically writes derived reports from an already verified
SuiteResult without calling the verifier or modifying raw evidence files:
- summary.md
- coverage.json
- suite-result.json (when created or reconciled)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .catalog import get_task_by_id_or_alias
from .collector import CollectorSecurityError, is_path_safe
from .e2e.file_safety import atomic_write_bytes
from .evidence_model import (
    EVIDENCE_MODEL_E2E,
    EVIDENCE_MODEL_IMMUTABLE,
    PROVENANCE_MODEL_HISTORICAL_PREPARED,
    PROVENANCE_MODEL_IMMUTABLE,
    selected_sha_meaning,
)
from .models import (
    EvidenceStatus,
    ExecutionStatus,
    SuiteResult,
)


def atomic_write_text(target_path: Path, content: str) -> None:
    """Publish a report through an anchored directory without following links."""
    target = Path(target_path)
    if not is_path_safe(target.parent, target):
        raise CollectorSecurityError(f"Unsafe report destination: {target}")
    atomic_write_bytes(
        target,
        content.encode("utf-8"),
        package_root=target.parent,
        what="Report destination",
        mode=0o644,
    )


class OfflineReporter:
    """Renders human-readable and machine-readable reports from verified suite results."""

    def __init__(self, suite_dir: Path):
        self.suite_dir = Path(suite_dir).resolve()
        if not self.suite_dir.is_dir():
            raise FileNotFoundError(f"Suite directory not found: {self.suite_dir}")
        self.plan_file = self.suite_dir / "suite-plan.json"
        self.result_file = self.suite_dir / "suite-result.json"
        self.events_file = self.suite_dir / "events.jsonl"
        self.index_file = self.suite_dir / "artifact-index.json"
        self.summary_md = self.suite_dir / "summary.md"
        self.coverage_json = self.suite_dir / "coverage.json"
        self.e2e_context_file = self.suite_dir / "e2e-context.json"
        self.e2e_evidence_expected = True

    def build_summary_markdown(
        self,
        result: SuiteResult,
        integrity_errors: Optional[Sequence[str]] = None,
    ) -> str:
        identity = result.source_identity
        historical = identity.is_historical
        model = EVIDENCE_MODEL_E2E if historical else EVIDENCE_MODEL_IMMUTABLE
        provenance = PROVENANCE_MODEL_HISTORICAL_PREPARED if historical else PROVENANCE_MODEL_IMMUTABLE
        lines = [
            f"# A8 Test Suite Summary: {result.suite_id}",
            "",
            "> [!IMPORTANT]",
            "> **Acceptance Authority Notice**",
            "> Automated test execution does NOT constitute manual acceptance reviewer approval.",
            "> All tasks are marked `acceptance_status: NOT_REVIEWED`. Final sign-off requires independent review.",
            "",
            f"- **Suite ID**: `{result.suite_id}`",
            f"- **Overall Status**: **{result.overall_status.value}**",
            f"- **Marketplace SHA**: `{identity.marketplace_commit_sha}`",
            f"- **Marketplace tree**: `{identity.marketplace_tree_sha or '-'}`",
            # Printed with its meaning, because older reports used the same
            # label for a prepared tree and a reader comparing SHAs by eye has
            # no other way to tell which claim this report makes.
            f"- **Gonka SHA (selected)**: `{identity.gonka_commit_sha}` — {selected_sha_meaning()}",
            f"- **Gonka tree**: `{identity.gonka_tree_sha or '-'}`",
            f"- **Source immutability**: `{identity.source_immutability_verdict or 'NOT_RECORDED'}`",
            f"- **Evidence model**: `{model}`",
            f"- **Provenance model**: `{provenance}`",
        ]
        if historical:
            # A historical identity is shown for what it is. It is never
            # rendered as if it described the selected commit.
            lines.extend([
                "",
                "> [!WARNING]",
                "> **Historical prepared-build evidence.** This suite was produced from a",
                "> prepared (patched) tree, not from the selected commit. It is readable but",
                "> is not proof of an unmodified source tree.",
                f"> Gonka prepared SHA (historical): `{identity.gonka_prepared_sha or '-'}`",
                "",
            ])
        lines.extend([
            f"- **Runner Hash**: `{identity.runner_version_hash[:12]}`",
            f"- **Catalog Hash**: `{identity.catalog_version_hash[:12]}`",
            f"- **Created At**: {result.created_at_utc}",
            f"- **Completed At**: {result.completed_at_utc or 'INTERRUPTED'}",
            "",
            "## Task Execution Overview",
            "",
            "| # | Task ID | Level | Execution | Evidence | Cleanup | Exit | Duration | Details |",
            "|---|---|---|---|---|---|---|---|---|",
        ])

        for t in result.tasks:
            dur = f"{t.duration_seconds:.1f}s" if t.duration_seconds is not None else "-"
            code = str(t.exit_code) if t.exit_code is not None else "-"
            note = t.primary_failure or f"Passed {len(t.observed_passed_cases)} cases"
            if len(note) > 50:
                note = note[:47] + "..."
            raw_execution = t.raw_execution_status or t.execution_status
            lines.append(
                f"| {t.ordinal} | `{t.task_id}` | {t.proof_level.value} | **{raw_execution.value}** | "
                f"{t.evidence_status.value} | {t.cleanup_status.value} | {code} | {dur} | {note} |"
            )

        lines.extend([
            "",
            "## Artifacts and Evidence",
            "",
            f"Evidence files are indexed in [`artifact-index.json`](artifact-index.json).",
            f"Detailed append-only lifecycle journal: [`events.jsonl`](events.jsonl).",
            "",
        ])

        if integrity_errors:
            lines.extend([
                "### Integrity and Corruption Violations",
                "",
                "> [!CAUTION]",
                "> **Artifact Corruption or Consistency Errors Detected**",
                "> The offline integrity check discovered discrepancies between indexed artifacts and disk contents:",
                "",
            ])
            for err in integrity_errors:
                lines.append(f"- :x: {err}")
            lines.append("")

        lines.extend([
            "### Failures and Warnings",
            "",
        ])

        has_failures = False
        for t in result.tasks:
            if t.execution_status != ExecutionStatus.PASSED or t.evidence_status != EvidenceStatus.COMPLETE:
                has_failures = True
                raw_execution = t.raw_execution_status or t.execution_status
                lines.append(f"#### Task `{t.task_id}` ({t.proof_level.value})")
                lines.append(f"- Test execution: `{raw_execution.value}`; evidence: `{t.evidence_status.value}`")
                if raw_execution != t.execution_status:
                    lines.append(f"- Graded suite status: `{t.execution_status.value}`")
                if t.primary_failure:
                    lines.append(f"- Primary Failure: {t.primary_failure}")
                if t.secondary_errors:
                    lines.append(f"- Secondary Errors: {', '.join(t.secondary_errors)}")
                if t.missing_artifacts:
                    lines.append(f"- Missing Artifacts: `{', '.join(t.missing_artifacts)}`")
                if t.missing_cases:
                    lines.append(f"- Missing/Failed Cases: `{', '.join(t.missing_cases[:10])}`")
                lines.append("")

        if not has_failures and not integrity_errors:
            lines.append("No task failures recorded. All executed checks completed successfully.\n")

        return "\n".join(lines) + "\n"

    def build_coverage_json(self, result: SuiteResult) -> Dict[str, Any]:
        covered: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []

        for t in result.tasks:
            catalog_item = get_task_by_id_or_alias(t.task_id)
            cov_ids = catalog_item.coverage_ids if catalog_item else []
            limitations = catalog_item.limitations if catalog_item else []

            item = {
                "task_id": t.task_id,
                "proof_level": t.proof_level.value,
                "coverage_ids": cov_ids,
                "execution_status": t.execution_status.value,
                "evidence_status": t.evidence_status.value,
                "limitations": limitations,
            }

            if t.execution_status == ExecutionStatus.PASSED and t.evidence_status == EvidenceStatus.COMPLETE:
                covered.append(item)
            else:
                item["failure_reason"] = t.primary_failure
                gaps.append(item)

        return {
            "schema_version": "1.0.0",
            "suite_id": result.suite_id,
            "overall_status": result.overall_status.value,
            "evaluated_tasks": len(result.tasks),
            "fully_covered_tasks": len(covered),
            "uncovered_or_failed_tasks": len(gaps),
            "covered": covered,
            "gaps": gaps,
            "historical_only_matrix_rows": [
                {"id": "C1 E+5 Refund / R1", "reason": "historical A/R1 receipt; JUnit FAIL was not rewritten"},
                {"id": "G1 / R6.2", "reason": "historical N17 cw20 fault rollback"},
                {"id": "D2 zero confirmed", "reason": "CT-only; native claimed=true,total=0 not claimed proven"},
            ],
        }

    def write_reports(
        self,
        result: SuiteResult,
        integrity_errors: Optional[Sequence[str]] = None,
        *,
        write_suite_result: bool = False,
    ) -> SuiteResult:
        """Render and atomically write derived suite reports from an already verified result.

        Does not re-run artifact verification or recalculate status. Never writes
        ``suite-result.json`` unless ``write_suite_result=True``, so offline
        report regeneration cannot replace or invent the final evidence record.
        """
        md_content = self.build_summary_markdown(result, integrity_errors=integrity_errors)
        atomic_write_text(self.summary_md, md_content)

        cov_content = self.build_coverage_json(result)
        atomic_write_text(self.coverage_json, json.dumps(cov_content, indent=2) + "\n")

        if write_suite_result:
            atomic_write_text(self.result_file, json.dumps(result.to_dict(), indent=2) + "\n")
        return result


if __name__ == "__main__":
    import sys
    from .verifier import verify_and_recalculate_suite

    if len(sys.argv) < 2:
        print("Usage: python3 -m ops.a8.reporter SUITE_DIR", file=sys.stderr)
        sys.exit(2)

    suite_p = Path(sys.argv[1]).resolve()
    try:
        verification = verify_and_recalculate_suite(suite_p)
        reporter = OfflineReporter(suite_p)
        res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
            write_suite_result=False,
        )
        print(f"Report generated for {res.suite_id}: status={res.overall_status.value}")
        sys.exit(0 if res.overall_status == ExecutionStatus.PASSED else 1)
    except Exception as exc:
        print(f"OfflineReporter error: {exc}", file=sys.stderr)
        sys.exit(1)
