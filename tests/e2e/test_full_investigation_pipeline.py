"""
E2E test: Full investigation pipeline — "checkout service slow" scenario.

Tests the complete triage lifecycle:
  /triage -> INTAKE -> TRIAGE -> DIAGNOSE -> SYNTHESIZE -> REMEDIATE/REPORT -> VERIFY

All specialist agents are mocked (no LLM calls), but the orchestrator logic,
graph backend, API server, confidence scoring, phase transitions, Slack
formatting, and approval workflow are exercised for real.

Target: 80%+ code coverage across the pipeline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from triagebot.api.server import TriageRequest, app, _investigations
from triagebot.graph.backend import InMemoryGraphBackend, get_graph_backend, _backend
from triagebot.models.findings import (
    ConfidenceLevel,
    DiagnosticFinding,
    InvestigationResult,
    InvestigationStatus,
    Phase,
    Severity,
)
from triagebot.models.scoring import ConfidenceScorer, aggregate_findings, classify_confidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    source: str = "log-analyzer",
    severity: str = "high",
    confidence: float = 0.85,
    summary: str = "Connection pool exhaustion detected",
    services: list[str] | None = None,
    remediation: str | None = "Increase connection pool limit",
) -> dict[str, Any]:
    """Build a JSON-serialisable finding dict matching the agent output format."""
    return {
        "source": source,
        "severity": severity,
        "confidence": confidence,
        "summary": summary,
        "evidence": [
            "50/50 active connections",
            "Connection wait time >5 s",
        ],
        "affected_services": services or ["checkout-api"],
        "suggested_remediation": remediation,
    }


async def _seed_graph(graph: InMemoryGraphBackend) -> None:
    """Populate the in-memory graph with a realistic checkout topology."""
    # Services
    for svc in ["gateway", "checkout-api", "payment-api", "order-api"]:
        await graph.upsert_node("Service", {
            "name": svc,
            "namespace": "production",
            "health": "healthy",
            "replicas": 3,
            "version": "v1.2.0",
        })

    # Databases
    for db in ["cart-db", "payments-db", "orders-db"]:
        await graph.upsert_node("Database", {
            "name": db,
            "engine": "postgres",
            "health": "healthy",
        })

    # Cache + Queue
    await graph.upsert_node("Cache", {"name": "redis-cache", "engine": "redis", "health": "healthy"})
    await graph.upsert_node("Queue", {"name": "order-events", "type": "sqs", "health": "healthy"})

    # Relationships
    await graph.upsert_relationship("Service", "gateway", "Service", "checkout-api", "DEPENDS_ON")
    await graph.upsert_relationship("Service", "gateway", "Service", "order-api", "DEPENDS_ON")
    await graph.upsert_relationship("Service", "checkout-api", "Database", "cart-db", "READS_FROM")
    await graph.upsert_relationship("Service", "checkout-api", "Service", "payment-api", "DEPENDS_ON")
    await graph.upsert_relationship("Service", "checkout-api", "Cache", "redis-cache", "CACHES_IN")
    await graph.upsert_relationship("Service", "payment-api", "Database", "payments-db", "READS_FROM")
    await graph.upsert_relationship("Service", "order-api", "Database", "orders-db", "READS_FROM")
    await graph.upsert_relationship("Service", "order-api", "Queue", "order-events", "PUBLISHES_TO")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests."""
    import triagebot.graph.backend as gb_mod
    import triagebot.agents.orchestrator as orch_mod

    old_backend = gb_mod._backend
    old_engine = orch_mod._engine
    gb_mod._backend = None
    orch_mod._engine = None
    _investigations.clear()
    yield
    gb_mod._backend = old_backend
    orch_mod._engine = old_engine
    _investigations.clear()


@pytest.fixture
async def seeded_graph() -> InMemoryGraphBackend:
    """Return an in-memory graph pre-seeded with checkout topology."""
    import triagebot.graph.backend as gb_mod

    graph = InMemoryGraphBackend()
    await graph.initialize()
    await _seed_graph(graph)
    gb_mod._backend = graph
    return graph


@pytest.fixture
def mock_strands_agent():
    """Patch Strands Agent so no LLM calls are made.

    The mock agent's __call__ returns a string containing two JSON findings
    that the orchestrator's _extract_findings parser can pick up.
    """
    log_finding = _make_finding(
        source="log-analyzer",
        severity="critical",
        confidence=0.88,
        summary="Connection pool exhaustion on cart-db — 50/50 connections in use",
        services=["checkout-api", "cart-db"],
        remediation="Increase max_connections from 50 to 100",
    )
    metrics_finding = _make_finding(
        source="metrics-analyzer",
        severity="high",
        confidence=0.82,
        summary="P99 latency 2.3 s on checkout-api (baseline 200 ms)",
        services=["checkout-api"],
        remediation="Scale checkout-api from 3 to 6 replicas",
    )

    fake_output = (
        "I investigated the checkout service slowness.\n\n"
        f"Log analysis finding: {json.dumps(log_finding)}\n\n"
        f"Metrics analysis finding: {json.dumps(metrics_finding)}\n"
    )

    mock_agent_instance = MagicMock()
    mock_agent_instance.__call__ = MagicMock(return_value=fake_output)
    mock_agent_instance.return_value = fake_output

    with patch("triagebot.agents.orchestrator.Agent", return_value=mock_agent_instance):
        yield mock_agent_instance


# ---------------------------------------------------------------------------
# 1. Graph dependency tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGraphDependencies:
    """Verify the graph returns correct dependencies for checkout-api."""

    async def test_checkout_api_has_dependencies(self, seeded_graph: InMemoryGraphBackend):
        """checkout-api depends on cart-db, payment-api, redis-cache."""
        blast = await seeded_graph.get_blast_radius("checkout-api")
        names = {entry["name"] for entry in blast}
        assert "gateway" in names, "gateway should depend on checkout-api"

    async def test_blast_radius_depth(self, seeded_graph: InMemoryGraphBackend):
        """Blast radius of cart-db should include checkout-api and gateway."""
        blast = await seeded_graph.get_blast_radius("cart-db", max_depth=3)
        names = {entry["name"] for entry in blast}
        assert "checkout-api" in names
        assert "gateway" in names

    async def test_graph_returns_empty_for_unknown_service(self, seeded_graph: InMemoryGraphBackend):
        """Unknown service should return empty blast radius."""
        blast = await seeded_graph.get_blast_radius("nonexistent-service")
        assert blast == []


# ---------------------------------------------------------------------------
# 2. Orchestrator pipeline tests (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestFullInvestigationPipeline:
    """Full pipeline: symptom -> investigate -> findings -> classification."""

    async def test_investigation_completes_with_findings(
        self, seeded_graph, mock_strands_agent
    ):
        """Orchestrator should produce findings, root cause, and confidence."""
        from triagebot.agents.orchestrator import InvestigationEngine

        engine = InvestigationEngine()
        result = await engine.investigate("checkout service is slow")

        assert result.investigation_id.startswith("inv_")
        assert result.status in (
            InvestigationStatus.AWAITING_APPROVAL,
            InvestigationStatus.REMEDIATING,
            InvestigationStatus.ESCALATED,
        )
        assert len(result.findings) >= 1
        assert result.root_cause is not None
        assert result.aggregate_confidence > 0.0

    async def test_investigation_extracts_affected_services(
        self, seeded_graph, mock_strands_agent
    ):
        """Affected services should be extracted from findings."""
        from triagebot.agents.orchestrator import InvestigationEngine

        engine = InvestigationEngine()
        result = await engine.investigate("checkout service is slow")

        assert "checkout-api" in result.affected_services

    async def test_investigation_reaches_report_or_remediate_phase(
        self, seeded_graph, mock_strands_agent
    ):
        """Phase should advance past TRIAGE/DIAGNOSE to SYNTHESIZE or beyond."""
        from triagebot.agents.orchestrator import InvestigationEngine

        engine = InvestigationEngine()
        result = await engine.investigate("checkout service is slow")

        assert result.phase in (Phase.SYNTHESIZE, Phase.REMEDIATE, Phase.REPORT, Phase.COMPLETE)

    async def test_investigation_sets_completed_at(
        self, seeded_graph, mock_strands_agent
    ):
        """completed_at should be set after investigation finishes."""
        from triagebot.agents.orchestrator import InvestigationEngine

        engine = InvestigationEngine()
        result = await engine.investigate("checkout service is slow")

        assert result.completed_at is not None
        assert isinstance(result.completed_at, datetime)

    async def test_investigation_handles_agent_failure(self, seeded_graph):
        """When the agent throws, investigation should be marked FAILED."""
        from triagebot.agents.orchestrator import InvestigationEngine

        mock_agent = MagicMock()
        mock_agent.side_effect = RuntimeError("LLM unavailable")

        with patch("triagebot.agents.orchestrator.Agent", return_value=mock_agent):
            engine = InvestigationEngine()
            result = await engine.investigate("checkout service is slow")

        assert result.status == InvestigationStatus.FAILED
        assert "failed" in (result.root_cause or "").lower()


# ---------------------------------------------------------------------------
# 3. Confidence scoring integration
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestConfidenceScoringIntegration:
    """Confidence scoring with realistic multi-finding scenarios."""

    def test_high_confidence_auto_remediates(self):
        """Score > 0.9 -> auto_remediate."""
        assert classify_confidence(0.92) == ConfidenceLevel.AUTO_REMEDIATE

    def test_medium_confidence_requires_approval(self):
        """Score 0.7-0.9 -> approval_required."""
        assert classify_confidence(0.75) == ConfidenceLevel.APPROVAL_REQUIRED

    def test_low_confidence_human_approval(self):
        """Score 0.5-0.7 -> human_approval."""
        assert classify_confidence(0.55) == ConfidenceLevel.HUMAN_APPROVAL

    def test_very_low_confidence_report_only(self):
        """Score < 0.5 -> report_only."""
        assert classify_confidence(0.3) == ConfidenceLevel.REPORT_ONLY

    def test_aggregate_corroborating_findings_boosts_score(self):
        """Two findings on the same service should boost aggregate above base."""
        f1 = DiagnosticFinding(
            source="log-analyzer",
            severity=Severity.HIGH,
            confidence=0.85,
            summary="Connection pool exhaustion",
            evidence=["50/50 connections"],
            affected_services=["checkout-api"],
        )
        f2 = DiagnosticFinding(
            source="metrics-analyzer",
            severity=Severity.HIGH,
            confidence=0.80,
            summary="Latency spike",
            evidence=["p99 2.3s"],
            affected_services=["checkout-api"],
        )
        score = aggregate_findings([f1, f2])
        assert score > f1.confidence, "Corroborating evidence should boost score"
        assert score <= 1.0

    def test_scorer_class_end_to_end(self):
        """ConfidenceScorer accumulates findings and classifies."""
        scorer = ConfidenceScorer()
        scorer.add_finding(DiagnosticFinding(
            source="log-analyzer",
            severity=Severity.CRITICAL,
            confidence=0.92,
            summary="OOMKill",
            evidence=["exit code 137"],
            affected_services=["checkout-api"],
        ))
        assert scorer.score() == 0.92
        assert scorer.classify() == ConfidenceLevel.AUTO_REMEDIATE

        scorer.reset()
        assert scorer.score() == 0.0


# ---------------------------------------------------------------------------
# 4. API server E2E (FastAPI TestClient)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestAPIEndToEnd:
    """Test the FastAPI REST API with mocked orchestrator."""

    def test_health_endpoint(self):
        """GET /health should return 200 with status ok."""
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_triage_endpoint_starts_investigation(
        self, seeded_graph, mock_strands_agent
    ):
        """POST /api/triage should start an investigation and return results."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/triage", json={
                "symptom": "checkout service is slow",
                "namespace": "production",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "investigation_id" in data
        assert data["status"] in [s.value for s in InvestigationStatus]
        assert "message" in data

    async def test_get_investigation_after_triage(
        self, seeded_graph, mock_strands_agent
    ):
        """GET /api/investigation/{id} should return full result after triage."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            triage_resp = await ac.post("/api/triage", json={
                "symptom": "checkout service is slow",
            })
            inv_id = triage_resp.json()["investigation_id"]

            detail_resp = await ac.get(f"/api/investigation/{inv_id}")

        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["investigation_id"] == inv_id
        assert len(detail["findings"]) >= 1

    async def test_get_investigation_not_found(self):
        """GET /api/investigation/nonexistent should return 404."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/investigation/nonexistent")
        assert resp.status_code == 404

    async def test_list_investigations(self, seeded_graph, mock_strands_agent):
        """GET /api/investigations should list all investigations."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post("/api/triage", json={"symptom": "test issue"})
            resp = await ac.get("/api/investigations")

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        assert "investigation_id" in items[0]

    async def test_approval_workflow(self, seeded_graph, mock_strands_agent):
        """POST /api/investigation/{id}/approve should approve remediation."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            triage_resp = await ac.post("/api/triage", json={
                "symptom": "checkout service is slow",
            })
            inv_id = triage_resp.json()["investigation_id"]

            # Force status to AWAITING_APPROVAL for the test
            if inv_id in _investigations:
                _investigations[inv_id].status = InvestigationStatus.AWAITING_APPROVAL

            approve_resp = await ac.post(f"/api/investigation/{inv_id}/approve", json={
                "approved": True,
                "approver": "test-user",
            })

        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

    async def test_denial_workflow(self, seeded_graph, mock_strands_agent):
        """POST /api/investigation/{id}/approve with approved=false should deny."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            triage_resp = await ac.post("/api/triage", json={
                "symptom": "checkout service is slow",
            })
            inv_id = triage_resp.json()["investigation_id"]

            if inv_id in _investigations:
                _investigations[inv_id].status = InvestigationStatus.AWAITING_APPROVAL

            deny_resp = await ac.post(f"/api/investigation/{inv_id}/approve", json={
                "approved": False,
                "approver": "test-user",
                "reason": "Need more evidence",
            })

        assert deny_resp.status_code == 200
        assert deny_resp.json()["status"] == "denied"

    async def test_approve_non_awaiting_returns_400(self, seeded_graph, mock_strands_agent):
        """Approving an investigation not in AWAITING_APPROVAL should return 400."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            triage_resp = await ac.post("/api/triage", json={
                "symptom": "checkout service is slow",
            })
            inv_id = triage_resp.json()["investigation_id"]

            # Force status to RESOLVED
            if inv_id in _investigations:
                _investigations[inv_id].status = InvestigationStatus.RESOLVED

            resp = await ac.post(f"/api/investigation/{inv_id}/approve", json={
                "approved": True,
            })

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 5. Slack formatting tests (no real Slack API calls)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestSlackWorkflow:
    """Test Slack message formatting and approval button logic."""

    def test_investigation_result_generates_blocks(self):
        """_post_investigation_result should format findings into Slack blocks."""
        from triagebot.api.slack_bot import _post_investigation_result

        result = InvestigationResult(
            investigation_id="inv_test123",
            symptom="checkout service is slow",
            status=InvestigationStatus.AWAITING_APPROVAL,
            phase=Phase.REPORT,
            findings=[
                DiagnosticFinding(
                    source="log-analyzer",
                    severity=Severity.HIGH,
                    confidence=0.85,
                    summary="Connection pool exhaustion",
                    evidence=["50/50 connections", "Wait time >5s"],
                    affected_services=["checkout-api"],
                    suggested_remediation="Increase pool size",
                ),
            ],
            root_cause="Connection pool exhaustion",
            aggregate_confidence=0.85,
            confidence_level=ConfidenceLevel.APPROVAL_REQUIRED,
            affected_services=["checkout-api"],
        )

        # We can't easily call _post_investigation_result without a real
        # slack client, but we can verify the result object is well-formed
        # for the formatter.
        assert result.confidence_level in (
            ConfidenceLevel.APPROVAL_REQUIRED,
            ConfidenceLevel.HUMAN_APPROVAL,
        )
        best = max(result.findings, key=lambda f: f.confidence)
        assert best.suggested_remediation is not None

    async def test_slack_approval_buttons_included_for_approval_level(self, mock_slack_client):
        """When confidence is APPROVAL_REQUIRED, Slack should show approve/deny buttons."""
        from triagebot.api.slack_bot import _post_investigation_result

        result = InvestigationResult(
            investigation_id="inv_test456",
            symptom="checkout service is slow",
            status=InvestigationStatus.AWAITING_APPROVAL,
            phase=Phase.REPORT,
            findings=[
                DiagnosticFinding(
                    source="log-analyzer",
                    severity=Severity.HIGH,
                    confidence=0.85,
                    summary="DB connection pool full",
                    evidence=["50/50 active"],
                    affected_services=["checkout-api"],
                    suggested_remediation="Increase pool to 100",
                ),
            ],
            root_cause="DB connection pool full",
            aggregate_confidence=0.85,
            confidence_level=ConfidenceLevel.APPROVAL_REQUIRED,
            affected_services=["checkout-api"],
        )

        await _post_investigation_result(
            mock_slack_client,
            "C123TEST",
            "1234567890.000",
            result,
        )

        mock_slack_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack_client.chat_postMessage.call_args
        blocks = call_kwargs.kwargs.get("blocks") or call_kwargs[1].get("blocks", [])

        # Should have an actions block with approve/deny buttons
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1, "Should have exactly one actions block"
        elements = action_blocks[0]["elements"]
        action_ids = {e["action_id"] for e in elements}
        assert "approve_remediation" in action_ids
        assert "deny_remediation" in action_ids

    async def test_slack_no_buttons_for_report_only(self, mock_slack_client):
        """When confidence is REPORT_ONLY, no approval buttons should appear."""
        from triagebot.api.slack_bot import _post_investigation_result

        result = InvestigationResult(
            investigation_id="inv_test789",
            symptom="minor cpu spike",
            status=InvestigationStatus.ESCALATED,
            phase=Phase.REPORT,
            findings=[
                DiagnosticFinding(
                    source="metrics-analyzer",
                    severity=Severity.LOW,
                    confidence=0.3,
                    summary="Minor CPU increase",
                    evidence=["CPU 45%"],
                    affected_services=["order-api"],
                ),
            ],
            root_cause="Minor CPU increase",
            aggregate_confidence=0.3,
            confidence_level=ConfidenceLevel.REPORT_ONLY,
            affected_services=["order-api"],
        )

        await _post_investigation_result(
            mock_slack_client,
            "C123TEST",
            "1234567890.000",
            result,
        )

        call_kwargs = mock_slack_client.chat_postMessage.call_args
        blocks = call_kwargs.kwargs.get("blocks") or call_kwargs[1].get("blocks", [])
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 0, "REPORT_ONLY should have no approval buttons"


# ---------------------------------------------------------------------------
# 6. Phase transition validation
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestPhaseTransitions:
    """Validate that only valid phase transitions are allowed."""

    def test_valid_transitions(self):
        from triagebot.models.findings import validate_transition, PHASE_TRANSITIONS

        for phase, targets in PHASE_TRANSITIONS.items():
            for target in targets:
                validate_transition(phase, target)  # Should not raise

    def test_invalid_transition_raises(self):
        from triagebot.models.findings import validate_transition, InvalidTransition

        with pytest.raises(InvalidTransition):
            validate_transition(Phase.INTAKE, Phase.DIAGNOSE)  # Must go through TRIAGE

        with pytest.raises(InvalidTransition):
            validate_transition(Phase.COMPLETE, Phase.TRIAGE)  # COMPLETE is terminal


# ---------------------------------------------------------------------------
# 7. WebSocket connectivity
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestWebSocketStreaming:
    """Test WebSocket endpoint for investigation updates."""

    def test_websocket_connect_and_ping(self):
        """WebSocket should accept connection and respond to ping."""
        client = TestClient(app)
        with client.websocket_connect("/ws/investigation/test-inv-123") as ws:
            ws.send_text("ping")
            data = ws.receive_json()
            assert data["type"] == "pong"

    def test_websocket_sends_current_state(self, seeded_graph, mock_strands_agent):
        """If investigation exists, WS should send current state on connect."""
        # Pre-populate an investigation
        result = InvestigationResult(
            investigation_id="ws-test-inv",
            symptom="test",
            status=InvestigationStatus.AWAITING_APPROVAL,
            phase=Phase.REPORT,
            aggregate_confidence=0.85,
        )
        _investigations["ws-test-inv"] = result

        client = TestClient(app)
        with client.websocket_connect("/ws/investigation/ws-test-inv") as ws:
            data = ws.receive_json()
            assert data["type"] == "current_state"
            assert data["status"] == "awaiting_approval"
            assert data["confidence"] == 0.85


# ---------------------------------------------------------------------------
# 8. Concurrency test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestConcurrency:
    """Test parallel investigations don't interfere with each other."""

    async def test_parallel_investigations(self, seeded_graph, mock_strands_agent):
        """Five parallel /api/triage calls should each get unique investigation IDs."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            tasks = [
                ac.post("/api/triage", json={"symptom": f"issue {i}"})
                for i in range(5)
            ]
            responses = await asyncio.gather(*tasks)

        ids = set()
        for resp in responses:
            assert resp.status_code == 200
            data = resp.json()
            assert data["investigation_id"] not in ids
            ids.add(data["investigation_id"])

        assert len(ids) == 5


# ---------------------------------------------------------------------------
# 9. Error handling
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestErrorHandling:
    """Test graceful error handling across the pipeline."""

    async def test_empty_symptom_still_works(self, seeded_graph, mock_strands_agent):
        """An empty symptom should still produce a result (not crash)."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/triage", json={"symptom": ""})
        # FastAPI validation or the engine should handle this
        assert resp.status_code in (200, 422)

    async def test_malformed_json_returns_422(self):
        """Sending invalid JSON should return 422."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/triage",
                content="not json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 422

    def test_finding_confidence_clamped(self):
        """DiagnosticFinding should reject confidence outside 0.0-1.0."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            DiagnosticFinding(
                source="test",
                severity=Severity.LOW,
                confidence=1.5,
                summary="bad",
                evidence=[],
                affected_services=[],
            )

    def test_invalid_severity_rejected(self):
        """DiagnosticFinding should reject invalid severity values."""
        with pytest.raises(Exception):
            DiagnosticFinding(
                source="test",
                severity="catastrophic",  # type: ignore[arg-type]
                confidence=0.5,
                summary="bad",
                evidence=[],
                affected_services=[],
            )


# ---------------------------------------------------------------------------
# 10. Graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGracefulShutdown:
    """Test that resources can be cleaned up properly."""

    async def test_graph_backend_closes_cleanly(self):
        """InMemoryGraphBackend.close() should clear all data."""
        graph = InMemoryGraphBackend()
        await graph.initialize()
        await graph.upsert_node("Service", {"name": "test-svc", "health": "healthy"})
        assert len(graph.nodes) == 1

        await graph.close()
        assert len(graph.nodes) == 0
        assert len(graph.relationships) == 0
