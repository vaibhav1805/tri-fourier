"""
Confidence scoring system for diagnostic findings.

Implements the scoring logic from ARCHITECTURE.md Section 6.2:
- Base confidence values for evidence types
- Aggregation of multiple correlated findings
- Threshold classification for remediation gating
"""

from __future__ import annotations

from typing import Any

# Evidence type base confidence values (ARCHITECTURE.md Section 6.2)
EVIDENCE_BASE_CONFIDENCE: dict[str, float] = {
    "error_log_stacktrace": 0.9,
    "metric_anomaly_correlated": 0.8,
    "recent_deployment": 0.7,
    "resource_saturation": 0.7,
    "dependency_health_degradation": 0.6,
    "similar_past_incident": 0.5,
}

# Remediation thresholds
THRESHOLD_AUTO_REMEDIATE = 0.9
THRESHOLD_APPROVAL_REQUIRED = 0.7
THRESHOLD_HUMAN_APPROVAL = 0.5


def score_finding(finding: dict[str, Any]) -> float:
    """Score a single diagnostic finding based on its evidence.

    Returns a float in [0.0, 1.0].
    """
    evidence = finding.get("evidence", [])
    if not evidence:
        return 0.0

    raw_confidence = finding.get("confidence", 0.0)
    return max(0.0, min(1.0, float(raw_confidence)))


def aggregate_findings(findings: list[dict[str, Any]]) -> float:
    """Aggregate confidence across multiple diagnostic findings.

    Uses a noisy-OR model: combined confidence increases when multiple
    independent sources agree, but contradictory evidence dampens it.
    """
    if not findings:
        return 0.0

    if len(findings) == 1:
        return score_finding(findings[0])

    scores = [score_finding(f) for f in findings]

    # Check for contradictions: findings on different services with
    # conflicting severity reduce aggregate confidence
    services_seen: dict[str, list[float]] = {}
    for f in findings:
        for svc in f.get("affected_services", []):
            services_seen.setdefault(svc, []).append(score_finding(f))

    # Noisy-OR: P(at least one correct) = 1 - product(1 - p_i)
    complement_product = 1.0
    for s in scores:
        complement_product *= (1.0 - s)
    combined = 1.0 - complement_product

    return max(0.0, min(1.0, combined))


def classify_confidence(confidence: float) -> str:
    """Classify confidence level into a remediation action category.

    Thresholds (from ARCHITECTURE.md Section 6.2):
    - > 0.9:  auto_remediate (with notification)
    - 0.7-0.9: approval_required (auto with approval request)
    - 0.5-0.7: human_approval (suggest, require human)
    - < 0.5:  report_only (findings only)
    """
    if confidence > THRESHOLD_AUTO_REMEDIATE:
        return "auto_remediate"
    elif confidence >= THRESHOLD_APPROVAL_REQUIRED:
        return "approval_required"
    elif confidence >= THRESHOLD_HUMAN_APPROVAL:
        return "human_approval"
    else:
        return "report_only"
