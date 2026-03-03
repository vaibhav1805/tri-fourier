"""Confidence scoring system (ARCHITECTURE.md Section 6.2)."""

from __future__ import annotations

from trifourier.models.findings import ConfidenceLevel, DiagnosticFinding


def classify_confidence(score: float) -> ConfidenceLevel:
    """Classify a confidence score into a remediation gating level.

    Thresholds (from ARCHITECTURE.md):
        > 0.9  -> auto_remediate
        0.7-0.9 -> approval_required
        0.5-0.7 -> human_approval
        < 0.5  -> report_only
    """
    if score > 0.9:
        return ConfidenceLevel.AUTO_REMEDIATE
    if score >= 0.7:
        return ConfidenceLevel.APPROVAL_REQUIRED
    if score >= 0.5:
        return ConfidenceLevel.HUMAN_APPROVAL
    return ConfidenceLevel.REPORT_ONLY


def aggregate_findings(findings: list[DiagnosticFinding]) -> float:
    """Aggregate confidence scores from multiple findings.

    Uses a weighted combination: highest single score boosted by
    corroborating evidence from other findings on the same services.
    """
    if not findings:
        return 0.0
    if len(findings) == 1:
        return findings[0].confidence

    sorted_findings = sorted(findings, key=lambda f: f.confidence, reverse=True)
    base = sorted_findings[0].confidence

    # Corroboration boost: each additional finding on overlapping services
    # adds a diminishing boost (max total boost: 0.09 to stay under 1.0)
    boost = 0.0
    primary_services = set(sorted_findings[0].affected_services)
    for f in sorted_findings[1:]:
        overlap = primary_services & set(f.affected_services)
        if overlap:
            boost += f.confidence * 0.05
        else:
            boost += f.confidence * 0.01

    return min(base + boost, 1.0)


class ConfidenceScorer:
    """Stateful scorer that tracks findings and produces aggregate scores."""

    def __init__(self) -> None:
        self._findings: list[DiagnosticFinding] = []

    def add_finding(self, finding: DiagnosticFinding) -> None:
        self._findings.append(finding)

    @property
    def findings(self) -> list[DiagnosticFinding]:
        return list(self._findings)

    def score(self) -> float:
        return aggregate_findings(self._findings)

    def classify(self) -> ConfidenceLevel:
        return classify_confidence(self.score())

    def reset(self) -> None:
        self._findings.clear()
