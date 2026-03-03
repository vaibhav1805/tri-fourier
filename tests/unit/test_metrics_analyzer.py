"""
Unit tests for the metrics analyzer agent.

Tests metric analysis capabilities:
- Anomaly detection in time series
- Saturation identification
- Baseline comparison
- Prometheus/CloudWatch query functions
"""

from unittest.mock import MagicMock, patch

import pytest

from trifourier.agents.metrics_analyzer import (
    analyze_metrics,
    compare_to_baseline,
    detect_anomalies,
    detect_saturation,
    query_cloudwatch_metrics,
    query_prometheus,
)


@pytest.mark.unit
class TestAnomalyDetection:
    """Test anomaly detection in metric time series."""

    def test_detect_latency_spike(self):
        """Should detect sudden latency increase as anomaly."""
        # Normal values around 0.2s, with a massive spike to trigger z>3
        values = [
            (1709100000, "0.200"),
            (1709100060, "0.200"),
            (1709100120, "0.200"),
            (1709100180, "0.200"),
            (1709100240, "0.200"),
            (1709100300, "0.200"),
            (1709100360, "0.200"),
            (1709100420, "0.200"),
            (1709100480, "0.200"),
            (1709100540, "5.000"),  # Massive latency spike
        ]
        anomalies = detect_anomalies(values)
        assert len(anomalies) > 0
        assert anomalies[0]["type"] == "spike"
        assert anomalies[0]["value"] == 5.0

    def test_detect_error_rate_spike(self):
        """Should detect error rate increase as anomaly."""
        values = [
            (1709100000, "0.01"),
            (1709100060, "0.01"),
            (1709100120, "0.01"),
            (1709100180, "0.01"),
            (1709100240, "0.01"),
            (1709100300, "0.01"),
            (1709100360, "0.01"),
            (1709100420, "0.01"),
            (1709100480, "0.01"),
            (1709100540, "0.50"),  # Error rate spike to 50%
        ]
        anomalies = detect_anomalies(values, stddev_threshold=2.0)
        assert len(anomalies) > 0
        spike = max(anomalies, key=lambda a: a["value"])
        assert spike["value"] == 0.5

    def test_no_anomaly_in_normal_metrics(self):
        """Normal baseline metrics should not trigger anomaly detection."""
        values = [
            (1709100000, "0.200"),
            (1709100060, "0.210"),
            (1709100120, "0.195"),
            (1709100180, "0.205"),
            (1709100240, "0.200"),
        ]
        anomalies = detect_anomalies(values)
        assert len(anomalies) == 0

    def test_empty_values_returns_no_anomalies(self):
        """Empty or too-short series should return no anomalies."""
        assert detect_anomalies([]) == []
        assert detect_anomalies([(1, "0.5")]) == []
        assert detect_anomalies([(1, "0.5"), (2, "0.5")]) == []


@pytest.mark.unit
class TestSaturationDetection:
    """Test resource saturation identification."""

    def test_detect_cpu_saturation(self):
        """CPU usage > 90% should be flagged as saturation."""
        values = [
            (1709100000, "0.45"),
            (1709100060, "0.60"),
            (1709100120, "0.92"),  # Saturated
        ]
        result = detect_saturation(values, threshold=0.9)
        assert result["saturated"] is True
        assert result["peak_value"] >= 0.9
        assert result["percentage"] >= 90.0

    def test_normal_cpu_not_saturated(self):
        """CPU usage under threshold should not be flagged."""
        values = [
            (1709100000, "0.30"),
            (1709100060, "0.40"),
            (1709100120, "0.35"),
        ]
        result = detect_saturation(values, threshold=0.9)
        assert result["saturated"] is False

    def test_saturation_returns_percentage_and_timestamp(self):
        """Saturation result should include peak value and when it occurred."""
        values = [
            (1709100000, "0.50"),
            (1709100060, "0.95"),
            (1709100120, "0.80"),
        ]
        result = detect_saturation(values, threshold=0.9)
        assert result["saturated"] is True
        assert result["peak_timestamp"] == 1709100060
        assert result["percentage"] == 95.0

    def test_empty_values_not_saturated(self):
        """Empty values should return not saturated."""
        result = detect_saturation([])
        assert result["saturated"] is False


@pytest.mark.unit
class TestBaselineComparison:
    """Test metric comparison against historical baselines."""

    def test_significant_deviation_flagged(self):
        """Values > 3 stddev from baseline should be flagged."""
        current_values = [
            (1, "1.0"),
            (2, "1.1"),
            (3, "0.9"),
        ]
        result = compare_to_baseline(
            current_values,
            baseline_mean=0.2,
            baseline_stddev=0.05,
        )
        assert result["significant"] is True
        assert result["deviation_stddevs"] > 3.0
        assert result["direction"] == "above"

    def test_minor_deviation_not_flagged(self):
        """Values within 1 stddev should not be flagged."""
        current_values = [
            (1, "0.21"),
            (2, "0.19"),
            (3, "0.20"),
        ]
        result = compare_to_baseline(
            current_values,
            baseline_mean=0.2,
            baseline_stddev=0.05,
        )
        assert result["significant"] is False
        assert result["deviation_stddevs"] < 1.0

    def test_zero_stddev_not_flagged(self):
        """Zero baseline stddev should not cause division by zero."""
        result = compare_to_baseline(
            [(1, "0.5")],
            baseline_mean=0.2,
            baseline_stddev=0.0,
        )
        assert result["significant"] is False


@pytest.mark.unit
class TestQueryPrometheus:
    """Test Prometheus query function."""

    def test_query_prometheus_handles_connection_error(self):
        """Should return empty result when Prometheus is unavailable."""
        with patch("trifourier.agents.metrics_analyzer.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(prometheus_url="http://nonexistent:9090")
            result = query_prometheus("up", time_range="5m")
            assert result["status"] == "error"
            assert result["data"]["result"] == []

    def test_query_prometheus_success(self):
        """Should return Prometheus data on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {"service": "test"}, "values": [[1, "0.5"]]}],
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch("trifourier.agents.metrics_analyzer.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(prometheus_url="http://localhost:9090")
            with patch("httpx.get", return_value=mock_response):
                result = query_prometheus('up{service="test"}', time_range="5m")
                assert result["status"] == "success"
                assert len(result["data"]["result"]) == 1


@pytest.mark.unit
class TestAnalyzeMetrics:
    """Test the high-level analyze_metrics function."""

    def test_analyze_metrics_returns_empty_when_sources_unavailable(self):
        """Should return empty findings when all sources fail."""
        with patch("trifourier.agents.metrics_analyzer.query_prometheus") as mock_prom:
            mock_prom.return_value = {"status": "error", "data": {"resultType": "matrix", "result": []}}
            with patch("trifourier.agents.metrics_analyzer.query_cloudwatch_metrics", return_value=[]):
                findings = analyze_metrics(
                    services=["checkout-api"],
                    query="latency",
                    time_range="15m",
                )
                assert findings == []

    def test_analyze_metrics_detects_latency_spike(self):
        """Should detect latency anomaly from Prometheus data."""
        prom_response = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{
                    "metric": {"service": "checkout-api"},
                    "values": [
                        [1709100000, "0.200"],
                        [1709100060, "0.200"],
                        [1709100120, "0.200"],
                        [1709100180, "0.200"],
                        [1709100240, "0.200"],
                        [1709100300, "0.200"],
                        [1709100360, "0.200"],
                        [1709100420, "0.200"],
                        [1709100480, "0.200"],
                        [1709100540, "5.000"],  # massive spike
                    ],
                }],
            },
        }

        def mock_prom(query, **kwargs):
            if "duration" in query:
                return prom_response
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}

        with patch("trifourier.agents.metrics_analyzer.query_prometheus", side_effect=mock_prom):
            with patch("trifourier.agents.metrics_analyzer.query_cloudwatch_metrics", return_value=[]):
                findings = analyze_metrics(
                    services=["checkout-api"],
                    query="latency spike",
                    time_range="15m",
                )
                assert len(findings) > 0
                assert any("latency" in f.summary.lower() or "spike" in f.summary.lower() for f in findings)
