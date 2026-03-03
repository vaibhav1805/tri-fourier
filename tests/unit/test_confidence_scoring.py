"""
Unit tests for the confidence scoring system.

Tests the scoring logic defined in ARCHITECTURE.md Section 6.2.
"""

import pytest

from src.agents.scoring import aggregate_findings, classify_confidence, score_finding


@pytest.mark.unit
class TestBaseConfidenceScoring:
    """Test individual finding confidence values."""

    def test_error_log_with_stacktrace_scores_high(self, high_confidence_finding):
        score = score_finding(high_confidence_finding)
        assert score >= 0.9

    def test_metric_anomaly_scores_medium_high(self, medium_confidence_finding):
        score = score_finding(medium_confidence_finding)
        assert 0.5 <= score <= 0.8

    def test_low_evidence_scores_below_threshold(self, low_confidence_finding):
        score = score_finding(low_confidence_finding)
        assert score < 0.5

    def test_score_finding_returns_float_0_to_1(self, high_confidence_finding):
        score = score_finding(high_confidence_finding)
        assert 0.0 <= score <= 1.0

    def test_score_finding_with_empty_evidence_returns_zero(self):
        finding = {"source": "log-analyzer", "evidence": [], "confidence": 0.0}
        assert score_finding(finding) == 0.0

    def test_score_finding_clamps_above_1(self):
        finding = {"source": "test", "evidence": ["x"], "confidence": 1.5}
        assert score_finding(finding) == 1.0

    def test_score_finding_clamps_below_0(self):
        finding = {"source": "test", "evidence": ["x"], "confidence": -0.5}
        assert score_finding(finding) == 0.0


@pytest.mark.unit
class TestConfidenceAggregation:
    """Test aggregation of multiple diagnostic findings."""

    def test_multiple_findings_boost_score(self, high_confidence_finding, medium_confidence_finding):
        findings = [high_confidence_finding, medium_confidence_finding]
        aggregate = aggregate_findings(findings)
        individual_max = max(score_finding(f) for f in findings)
        assert aggregate > individual_max

    def test_single_finding_returns_its_own_score(self, high_confidence_finding):
        aggregate = aggregate_findings([high_confidence_finding])
        assert aggregate == score_finding(high_confidence_finding)

    def test_empty_findings_returns_zero(self):
        assert aggregate_findings([]) == 0.0

    def test_aggregate_never_exceeds_1(self, high_confidence_finding):
        findings = [high_confidence_finding] * 10
        assert aggregate_findings(findings) <= 1.0


@pytest.mark.unit
class TestConfidenceThresholds:
    """Test threshold classification for remediation gating."""

    def test_above_09_is_auto_remediate(self):
        assert classify_confidence(0.95) == "auto_remediate"

    def test_07_to_09_is_approval_required(self):
        assert classify_confidence(0.85) == "approval_required"

    def test_05_to_07_is_human_approval(self):
        assert classify_confidence(0.65) == "human_approval"

    def test_below_05_is_report_only(self):
        assert classify_confidence(0.35) == "report_only"

    def test_boundary_07_is_approval_required(self):
        assert classify_confidence(0.7) == "approval_required"

    def test_boundary_09_is_approval_required(self):
        assert classify_confidence(0.9) == "approval_required"

    def test_boundary_05_is_human_approval(self):
        assert classify_confidence(0.5) == "human_approval"

    def test_zero_is_report_only(self):
        assert classify_confidence(0.0) == "report_only"

    def test_one_is_auto_remediate(self):
        assert classify_confidence(1.0) == "auto_remediate"
