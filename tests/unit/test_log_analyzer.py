"""
Unit tests for the log analyzer agent.

Tests from ARCHITECTURE.md Section 4.2 and Skills Architecture Section 7:
- Log search and filtering
- Stack trace parsing
- Error pattern detection
- Event correlation
"""

from unittest.mock import MagicMock, patch

import pytest

from triagebot.agents.log_analyzer import (
    analyze_logs,
    correlate_events,
    detect_error_patterns,
    parse_stack_traces,
    search_cloudwatch,
)


@pytest.mark.unit
class TestLogSearch:
    """Test log search tool functions."""

    def test_search_cloudwatch_returns_formatted_results(self, mock_cloudwatch):
        """CloudWatch search should return structured log entries."""
        with patch("triagebot.agents.log_analyzer._get_cloudwatch_client", return_value=mock_cloudwatch):
            results = search_cloudwatch(
                log_group="/aws/eks/production/checkout-api",
                query="fields @timestamp, @message | filter @message like /ERROR/",
                time_range="1h",
            )
            assert len(results) > 0
            assert "timestamp" in results[0]
            assert "message" in results[0]

    def test_search_cloudwatch_handles_empty_results(self):
        """Should return empty list when CloudWatch is unavailable."""
        with patch("triagebot.agents.log_analyzer._get_cloudwatch_client", return_value=None):
            results = search_cloudwatch(
                log_group="/aws/eks/production/checkout-api",
                query="fields @timestamp, @message",
                time_range="15m",
            )
            assert results == []

    def test_search_cloudwatch_validates_time_range(self, mock_cloudwatch):
        """Should handle various time range formats."""
        with patch("triagebot.agents.log_analyzer._get_cloudwatch_client", return_value=mock_cloudwatch):
            # Valid formats
            search_cloudwatch("/aws/eks/prod/svc", "fields @message", time_range="15m")
            search_cloudwatch("/aws/eks/prod/svc", "fields @message", time_range="1h")
            search_cloudwatch("/aws/eks/prod/svc", "fields @message", time_range="6h")
            # Invalid format defaults to 15 minutes
            search_cloudwatch("/aws/eks/prod/svc", "fields @message", time_range="invalid")
            assert True  # No exceptions raised


@pytest.mark.unit
class TestStackTraceParsing:
    """Test stack trace extraction and parsing."""

    def test_parse_java_stacktrace(self):
        """Should extract exception class, message, and top frames from Java stacktrace."""
        log_text = """2026-02-28 10:00:00 ERROR [main] com.example.Checkout -
java.sql.SQLException: Connection pool exhausted
\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:188)
\tat com.example.CheckoutService.processOrder(CheckoutService.java:42)"""
        traces = parse_stack_traces(log_text)
        assert len(traces) == 1
        assert traces[0]["exception"] == "java.sql.SQLException"
        assert "Connection pool exhausted" in traces[0]["message"]
        assert traces[0]["language"] == "java"

    def test_parse_python_traceback(self):
        """Should extract exception class, message, and top frames from Python traceback."""
        log_text = """Traceback (most recent call last):
  File "/app/checkout.py", line 42, in process_order
    result = db.execute(query)
  File "/app/db.py", line 88, in execute
    conn = self.pool.get()
ConnectionError: Connection refused by upstream"""
        traces = parse_stack_traces(log_text)
        assert len(traces) == 1
        assert traces[0]["exception"] == "ConnectionError"
        assert "Connection refused" in traces[0]["message"]
        assert traces[0]["language"] == "python"

    def test_parse_no_stacktrace_returns_empty(self):
        """Should return empty list for log entries without stack traces."""
        log_text = "2026-02-28 10:00:00 INFO Request processed successfully in 42ms"
        traces = parse_stack_traces(log_text)
        assert traces == []


@pytest.mark.unit
class TestErrorPatternDetection:
    """Test error pattern identification in log streams."""

    def test_detect_connection_pool_exhaustion(self):
        """Should detect connection pool exhaustion pattern from logs."""
        logs = [
            {"message": "ERROR: Connection pool exhausted, 50/50 active connections"},
            {"message": "WARN: No available connections in pool, waiting..."},
            {"message": "ERROR: Pool timeout after 30s"},
        ]
        patterns = detect_error_patterns(logs)
        assert any(p["type"] == "connection_pool_exhaustion" for p in patterns)
        pool_pattern = next(p for p in patterns if p["type"] == "connection_pool_exhaustion")
        assert pool_pattern["count"] >= 2
        assert pool_pattern["severity"] == "critical"

    def test_detect_oom_kill_pattern(self):
        """Should detect OOMKill pattern from logs."""
        logs = [
            {"message": "container was OOMKilled, exit code 137"},
            {"message": "out of memory: killed process 1234"},
        ]
        patterns = detect_error_patterns(logs)
        assert any(p["type"] == "oom_killed" for p in patterns)
        oom_pattern = next(p for p in patterns if p["type"] == "oom_killed")
        assert oom_pattern["severity"] == "critical"

    def test_no_errors_returns_empty_patterns(self):
        """Should return empty list for healthy logs."""
        logs = [
            {"message": "INFO: Request processed successfully"},
            {"message": "INFO: Health check passed"},
        ]
        patterns = detect_error_patterns(logs)
        assert patterns == []


@pytest.mark.unit
class TestEventCorrelation:
    """Test temporal correlation of log events."""

    def test_correlate_errors_within_time_window(self):
        """Errors in same time window across services should be correlated."""
        entries = [
            {"timestamp": "2026-02-28T10:00:00Z", "message": "ERROR", "service": "checkout-api"},
            {"timestamp": "2026-02-28T10:00:30Z", "message": "ERROR", "service": "payment-api"},
            {"timestamp": "2026-02-28T10:01:00Z", "message": "ERROR", "service": "order-api"},
        ]
        groups = correlate_events(entries, window_seconds=300)
        assert len(groups) >= 1
        assert len(groups[0]["services"]) >= 2

    def test_unrelated_errors_not_correlated(self):
        """Errors far apart in time should not be correlated."""
        entries = [
            {"timestamp": "2026-02-28T10:00:00Z", "message": "ERROR", "service": "checkout-api"},
            {"timestamp": "2026-02-28T11:00:00Z", "message": "ERROR", "service": "payment-api"},
        ]
        groups = correlate_events(entries, window_seconds=300)
        # No groups since errors are 1 hour apart (exceeds 5 min window)
        assert len(groups) == 0


@pytest.mark.unit
class TestAnalyzeLogs:
    """Test the high-level analyze_logs function."""

    def test_analyze_logs_returns_empty_when_cloudwatch_unavailable(self):
        """Should return empty findings when CloudWatch is not available."""
        with patch("triagebot.agents.log_analyzer._get_cloudwatch_client", return_value=None):
            findings = analyze_logs(
                services=["checkout-api"],
                query="errors",
                time_range="15m",
            )
            assert findings == []

    def test_analyze_logs_with_mock_entries(self):
        """Should produce findings from mock log entries."""
        mock_entries = [
            {"timestamp": "2026-02-28T10:00:00Z", "message": "ERROR: Connection pool exhausted"},
            {"timestamp": "2026-02-28T10:00:05Z", "message": "ERROR: No available connections"},
        ]
        with patch("triagebot.agents.log_analyzer.search_cloudwatch", return_value=mock_entries):
            findings = analyze_logs(
                services=["checkout-api"],
                query="connection",
                time_range="15m",
            )
            assert len(findings) > 0
            assert findings[0].source == "log-analyzer"
            assert findings[0].severity.value in ("critical", "high", "medium", "low")
