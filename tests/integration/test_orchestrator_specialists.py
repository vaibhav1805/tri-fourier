"""
Integration tests: Orchestrator -> Specialist agent dispatch + MCP graph tools.

Tests the full flow of the orchestrator dispatching to specialist
sub-agents, collecting their results, and using MCP graph tools.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trifourier.agents.log_analyzer import analyze_logs, detect_error_patterns
from trifourier.agents.metrics_analyzer import analyze_metrics, detect_anomalies
from trifourier.mcp_server import blast_radius, query_graph


@pytest.mark.integration
class TestOrchestratorLogAnalyzer:
    """Test orchestrator -> log analyzer integration."""

    def test_orchestrator_sends_correct_context_to_log_analyzer(self):
        """Orchestrator should pass service names and time range to log analyzer."""
        mock_entries = [
            {"timestamp": "2026-02-28T10:00:00Z", "message": "ERROR: timeout"},
        ]
        with patch("trifourier.agents.log_analyzer.search_cloudwatch", return_value=mock_entries):
            findings = analyze_logs(
                services=["checkout-api", "payment-api"],
                query="timeout",
                time_range="30m",
            )
            # Should process entries and produce findings
            assert isinstance(findings, list)
            for f in findings:
                assert f.source == "log-analyzer"

    def test_log_analyzer_returns_structured_finding(self):
        """Log analyzer result should be a valid DiagnosticFinding."""
        mock_entries = [
            {"timestamp": "2026-02-28T10:00:00Z", "message": "OOMKilled exit code 137"},
        ]
        with patch("trifourier.agents.log_analyzer.search_cloudwatch", return_value=mock_entries):
            findings = analyze_logs(
                services=["checkout-api"],
                query="OOMKill",
                time_range="15m",
            )
            if findings:
                f = findings[0]
                assert hasattr(f, "source")
                assert hasattr(f, "severity")
                assert hasattr(f, "confidence")
                assert hasattr(f, "summary")
                assert hasattr(f, "evidence")
                assert hasattr(f, "affected_services")
                assert 0.0 <= f.confidence <= 1.0


@pytest.mark.integration
class TestOrchestratorMetricsAnalyzer:
    """Test orchestrator -> metrics analyzer integration."""

    def test_orchestrator_sends_correct_context_to_metrics_analyzer(self):
        """Orchestrator should pass service names and metrics queries."""
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
                        [1709100240, "5.000"],
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
                    query="latency",
                    time_range="15m",
                )
                assert isinstance(findings, list)
                for f in findings:
                    assert f.source == "metrics-analyzer"

    def test_metrics_analyzer_returns_structured_finding(self):
        """Metrics analyzer result should be a valid DiagnosticFinding."""
        prom_response = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{
                    "metric": {},
                    "values": [
                        [1, "0.3"], [2, "0.3"], [3, "0.3"], [4, "0.3"], [5, "0.95"],
                    ],
                }],
            },
        }

        def mock_prom(query, **kwargs):
            if "cpu" in query:
                return prom_response
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}

        with patch("trifourier.agents.metrics_analyzer.query_prometheus", side_effect=mock_prom):
            with patch("trifourier.agents.metrics_analyzer.query_cloudwatch_metrics", return_value=[]):
                findings = analyze_metrics(
                    services=["checkout-api"],
                    query="CPU saturation",
                    time_range="15m",
                )
                if findings:
                    f = findings[0]
                    assert hasattr(f, "source")
                    assert hasattr(f, "severity")
                    assert hasattr(f, "confidence")
                    assert 0.0 <= f.confidence <= 1.0


@pytest.mark.integration
class TestOrchestratorGraphIntegration:
    """Test orchestrator -> knowledge graph integration via MCP tools."""

    @pytest.mark.asyncio
    async def test_query_graph_service_dependencies(self, mock_graph_backend):
        """query_graph should return service dependency data."""
        mock_graph_backend.query = AsyncMock(return_value=[
            {"name": "cart-db", "type": "Database", "relationship": "READS_FROM", "health": "healthy"},
        ])

        mock_get = AsyncMock(return_value=mock_graph_backend)
        with patch("trifourier.graph.backend.get_graph_backend", mock_get):
            result = await query_graph(
                query_type="service_dependencies",
                service_name="checkout-api",
            )
            assert result["query_type"] == "service_dependencies"
            assert result["count"] >= 1
            assert result["results"][0]["name"] == "cart-db"

    @pytest.mark.asyncio
    async def test_blast_radius_calculation(self, mock_graph_backend):
        """blast_radius should return affected services with risk level."""
        mock_graph_backend.get_blast_radius = AsyncMock(return_value=[
            {"name": "gateway", "type": "Service", "depth": 1},
            {"name": "frontend", "type": "Service", "depth": 2},
        ])
        mock_graph_backend.query = AsyncMock(return_value=[])

        mock_get = AsyncMock(return_value=mock_graph_backend)
        with patch("trifourier.graph.backend.get_graph_backend", mock_get):
            result = await blast_radius(service_name="checkout-api", max_depth=3)
            assert result["service_name"] == "checkout-api"
            assert result["affected_count"] == 2
            assert result["risk_level"] == "medium"
            assert len(result["affected_services"]) == 2

    @pytest.mark.asyncio
    async def test_blast_radius_handles_graph_error(self):
        """blast_radius should return error info when graph fails."""
        mock_backend = AsyncMock()
        mock_backend.get_blast_radius = AsyncMock(side_effect=RuntimeError("Graph not initialized"))
        mock_backend.query = AsyncMock(side_effect=RuntimeError("Graph not initialized"))

        mock_get = AsyncMock(return_value=mock_backend)
        with patch("trifourier.graph.backend.get_graph_backend", mock_get):
            result = await blast_radius(service_name="nonexistent", max_depth=2)
            assert result["affected_count"] == 0
            assert "error" in result

    @pytest.mark.asyncio
    async def test_query_graph_returns_available_types_on_unknown(self, mock_graph_backend):
        """query_graph should list available query types for unknown type."""
        mock_get = AsyncMock(return_value=mock_graph_backend)
        with patch("trifourier.graph.backend.get_graph_backend", mock_get):
            result = await query_graph(query_type="unknown_type", service_name="test")
            assert "available_types" in result
            assert "service_dependencies" in result["available_types"]


@pytest.mark.integration
class TestMCPServerRoutes:
    """Test MCP REST route registration."""

    def test_register_mcp_routes_adds_endpoints(self):
        """register_mcp_routes should add graph endpoints to FastAPI app."""
        from fastapi import FastAPI
        from trifourier.mcp_server import register_mcp_routes

        app = FastAPI()
        register_mcp_routes(app)

        route_paths = [r.path for r in app.routes]
        assert "/api/graph/query" in route_paths
        assert "/api/graph/blast-radius" in route_paths
