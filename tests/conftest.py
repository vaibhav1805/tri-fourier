"""
Root conftest.py for TriageBot test suite.

Provides shared fixtures, mock factories, and test configuration
used across unit, integration, e2e, performance, and security tests.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------


def make_service(
    name: str = "checkout-api",
    namespace: str = "production",
    service_type: str = "api",
    health: str = "healthy",
    version: str = "v1.0.0",
    replicas: int = 3,
) -> dict[str, Any]:
    """Create a sample Service node for knowledge graph tests."""
    return {
        "name": name,
        "namespace": namespace,
        "type": service_type,
        "health": health,
        "version": version,
        "replicas": replicas,
        "owner_team": "platform-team",
        "last_deploy": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
    }


def make_database(
    name: str = "cart-db",
    engine: str = "postgres",
    health: str = "healthy",
) -> dict[str, Any]:
    """Create a sample Database node for knowledge graph tests."""
    return {
        "name": name,
        "engine": engine,
        "host": f"{name}.internal",
        "port": 5432,
        "health": health,
        "version": "15.4",
        "replicas": 2,
        "metadata": {},
    }


def make_queue(
    name: str = "order-events",
    queue_type: str = "sqs",
    depth: int = 0,
    health: str = "healthy",
) -> dict[str, Any]:
    """Create a sample Queue node for knowledge graph tests."""
    return {
        "name": name,
        "type": queue_type,
        "depth": depth,
        "dlq_depth": 0,
        "health": health,
        "metadata": {},
    }


@dataclass
class DiagnosticFindingFactory:
    """Factory for creating test DiagnosticFinding instances."""

    source: str = "log-analyzer"
    severity: str = "high"
    confidence: float = 0.85
    summary: str = "Connection pool exhaustion detected"
    evidence: list[str] = field(default_factory=lambda: ["50/50 active connections"])
    affected_services: list[str] = field(default_factory=lambda: ["checkout-api"])
    suggested_remediation: str | None = "Increase connection pool limit"
    raw_data: dict[str, Any] = field(default_factory=dict)

    def build(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "severity": self.severity,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence": self.evidence,
            "affected_services": self.affected_services,
            "suggested_remediation": self.suggested_remediation,
            "raw_data": self.raw_data,
        }


# ---------------------------------------------------------------------------
# Fixtures -- Knowledge Graph
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_service():
    """A healthy checkout-api service."""
    return make_service()


@pytest.fixture
def sample_database():
    """A healthy cart-db postgres database."""
    return make_database()


@pytest.fixture
def sample_queue():
    """A healthy order-events SQS queue."""
    return make_queue()


@pytest.fixture
def sample_dependency_graph():
    """A small dependency graph for testing blast radius and traversal.

    Topology:
        gateway -> checkout-api -> cart-db
                                -> payment-api -> payments-db
                                -> redis-cache
                -> order-api    -> order-events (queue)
                                -> orders-db
    """
    return {
        "services": [
            make_service("gateway", service_type="gateway"),
            make_service("checkout-api"),
            make_service("payment-api"),
            make_service("order-api"),
        ],
        "databases": [
            make_database("cart-db"),
            make_database("payments-db"),
            make_database("orders-db"),
        ],
        "caches": [
            {
                "name": "redis-cache",
                "engine": "redis",
                "hit_rate": 0.95,
                "health": "healthy",
                "metadata": {},
            }
        ],
        "queues": [
            make_queue("order-events"),
        ],
        "relationships": [
            {"from": "gateway", "to": "checkout-api", "type": "DEPENDS_ON"},
            {"from": "gateway", "to": "order-api", "type": "DEPENDS_ON"},
            {"from": "checkout-api", "to": "cart-db", "type": "READS_FROM"},
            {"from": "checkout-api", "to": "payment-api", "type": "DEPENDS_ON"},
            {"from": "checkout-api", "to": "redis-cache", "type": "CACHES_IN"},
            {"from": "payment-api", "to": "payments-db", "type": "READS_FROM"},
            {"from": "order-api", "to": "orders-db", "type": "READS_FROM"},
            {"from": "order-api", "to": "order-events", "type": "PUBLISHES_TO"},
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures -- Diagnostic Findings
# ---------------------------------------------------------------------------


@pytest.fixture
def finding_factory():
    """Factory for building diagnostic findings with custom fields."""
    return DiagnosticFindingFactory


@pytest.fixture
def high_confidence_finding():
    """A finding with confidence > 0.9 (auto-remediate threshold)."""
    f = DiagnosticFindingFactory(
        source="log-analyzer",
        severity="critical",
        confidence=0.92,
        summary="OOMKilled: checkout-api pod exceeded memory limit",
        evidence=[
            "kubectl logs: OOMKilled exit code 137",
            "Memory usage 98% at time of crash",
            "3 pod restarts in last 10 minutes",
        ],
        affected_services=["checkout-api"],
        suggested_remediation="Increase memory limit from 512Mi to 1Gi",
    )
    return f.build()


@pytest.fixture
def medium_confidence_finding():
    """A finding with confidence 0.5-0.7 (requires human approval)."""
    f = DiagnosticFindingFactory(
        source="metrics-analyzer",
        severity="medium",
        confidence=0.65,
        summary="Elevated latency correlates with increased traffic",
        evidence=[
            "p99 latency 1.2s (baseline 200ms)",
            "RPS increased 3x in last 30 minutes",
        ],
        affected_services=["checkout-api", "payment-api"],
        suggested_remediation="Scale checkout-api from 3 to 6 replicas",
    )
    return f.build()


@pytest.fixture
def low_confidence_finding():
    """A finding with confidence < 0.5 (report only)."""
    f = DiagnosticFindingFactory(
        source="metrics-analyzer",
        severity="low",
        confidence=0.35,
        summary="Minor CPU increase on order-api",
        evidence=["CPU usage 45% (baseline 30%)"],
        affected_services=["order-api"],
        suggested_remediation=None,
    )
    return f.build()


# ---------------------------------------------------------------------------
# Fixtures -- Mock external systems
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cloudwatch():
    """Mock CloudWatch Logs Insights client."""
    mock = MagicMock()
    mock.start_query = MagicMock(return_value={"queryId": "test-query-id"})
    mock.get_query_results = MagicMock(
        return_value={
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "2026-02-28T10:00:00Z"},
                    {"field": "@message", "value": "ERROR: Connection pool exhausted"},
                ]
            ],
        }
    )
    return mock


@pytest.fixture
def mock_prometheus():
    """Mock Prometheus query API responses."""
    mock = AsyncMock()
    mock.query_range = AsyncMock(
        return_value={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "http_request_duration_seconds", "service": "checkout-api"},
                        "values": [
                            [1709100000, "0.200"],
                            [1709100060, "0.250"],
                            [1709100120, "2.300"],  # Latency spike
                        ],
                    }
                ],
            },
        }
    )
    return mock


@pytest.fixture
def mock_kubernetes():
    """Mock Kubernetes client."""
    mock = MagicMock()

    # Mock pod list
    pod = MagicMock()
    pod.metadata.name = "checkout-api-abc123"
    pod.metadata.namespace = "production"
    pod.status.phase = "Running"
    pod.status.container_statuses = [
        MagicMock(
            name="checkout-api",
            ready=True,
            restart_count=0,
            state=MagicMock(running=MagicMock(started_at=datetime.now(timezone.utc))),
        )
    ]

    mock.list_namespaced_pod = MagicMock(return_value=MagicMock(items=[pod]))
    return mock


@pytest.fixture
def mock_graph_backend():
    """Mock graph backend for testing without FalkorDBLite."""
    mock = AsyncMock()
    mock.query = AsyncMock(return_value=[])
    mock.upsert_node = AsyncMock()
    mock.upsert_relationship = AsyncMock()
    mock.get_blast_radius = AsyncMock(
        return_value=[
            {"name": "checkout-api", "depth": 1},
            {"name": "gateway", "depth": 2},
        ]
    )
    return mock


@pytest.fixture
def mock_slack_client():
    """Mock Slack Web API client."""
    mock = AsyncMock()
    mock.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "1234567890.123456", "channel": "C123TEST"}
    )
    mock.chat_update = AsyncMock(return_value={"ok": True})
    mock.reactions_add = AsyncMock(return_value={"ok": True})
    return mock


# ---------------------------------------------------------------------------
# Fixtures -- Fixture files
# ---------------------------------------------------------------------------


@pytest.fixture
def load_fixture():
    """Load a JSON fixture file from the fixtures directory."""

    def _load(filename: str) -> dict[str, Any]:
        filepath = FIXTURES_DIR / filename
        with open(filepath) as f:
            return json.load(f)

    return _load


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: Unit tests (fast, no external deps)")
    config.addinivalue_line("markers", "integration: Integration tests (may use embedded graph)")
    config.addinivalue_line("markers", "e2e: End-to-end workflow tests")
    config.addinivalue_line("markers", "performance: Performance benchmark tests")
    config.addinivalue_line("markers", "security: Security validation tests")
    config.addinivalue_line("markers", "slow: Tests that take > 5 seconds")
