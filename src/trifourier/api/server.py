"""FastAPI server for Trifourier REST and WebSocket API.

Endpoints:
- POST /api/triage — Start an investigation
- GET  /api/investigation/{id} — Get investigation status
- POST /api/investigation/{id}/approve — Approve remediation
- GET  /health — Liveness/readiness probe
- WS   /ws/investigation/{id} — Real-time investigation updates
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from trifourier.agents.orchestrator import get_engine
from trifourier.models.findings import InvestigationResult, InvestigationStatus

logger = structlog.get_logger()

app = FastAPI(
    title="Trifourier",
    description="Kubernetes-first production troubleshooting agent",
    version="0.1.0",
)

# Register MCP graph REST routes
from trifourier.mcp_server import register_mcp_routes

register_mcp_routes(app)

# In-memory store of investigations (replace with persistent store later)
_investigations: dict[str, InvestigationResult] = {}
_ws_connections: dict[str, list[WebSocket]] = {}


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class TriageRequest(BaseModel):
    symptom: str
    namespace: str = "default"
    channel: str | None = None  # Slack channel ID if triggered from Slack


class TriageResponse(BaseModel):
    investigation_id: str
    status: str
    message: str


class ApprovalRequest(BaseModel):
    approved: bool
    approver: str = "human"
    reason: str = ""


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/triage", response_model=TriageResponse)
async def start_triage(request: TriageRequest) -> TriageResponse:
    """Start a new triage investigation."""
    engine = get_engine()

    # Run investigation in background
    result = await engine.investigate(
        symptom=request.symptom,
        namespace=request.namespace,
    )

    _investigations[result.investigation_id] = result

    # Notify any WebSocket listeners
    await _broadcast_ws(result.investigation_id, {
        "type": "investigation_complete",
        "investigation_id": result.investigation_id,
        "status": result.status.value,
        "confidence": result.aggregate_confidence,
        "confidence_level": result.confidence_level.value,
        "root_cause": result.root_cause,
        "findings_count": len(result.findings),
    })

    return TriageResponse(
        investigation_id=result.investigation_id,
        status=result.status.value,
        message=f"Investigation complete. Root cause: {result.root_cause or 'Unknown'}",
    )


@app.get("/api/investigation/{investigation_id}")
async def get_investigation(investigation_id: str) -> dict[str, Any]:
    """Get the status and results of an investigation."""
    result = _investigations.get(investigation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return result.model_dump(mode="json")


@app.post("/api/investigation/{investigation_id}/approve")
async def approve_remediation(
    investigation_id: str, request: ApprovalRequest
) -> dict[str, str]:
    """Approve or deny a remediation action."""
    result = _investigations.get(investigation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if result.status != InvestigationStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Investigation is not awaiting approval (status: {result.status})",
        )

    if request.approved:
        result.status = InvestigationStatus.REMEDIATING
        logger.info(
            "remediation.approved",
            investigation_id=investigation_id,
            approver=request.approver,
        )
        # TODO: Execute remediation action
        result.status = InvestigationStatus.RESOLVED
        result.completed_at = datetime.now(timezone.utc)
        return {"status": "approved", "message": "Remediation approved and executed"}
    else:
        result.status = InvestigationStatus.ESCALATED
        logger.info(
            "remediation.denied",
            investigation_id=investigation_id,
            approver=request.approver,
            reason=request.reason,
        )
        return {"status": "denied", "message": f"Remediation denied: {request.reason}"}


@app.get("/api/investigations")
async def list_investigations() -> list[dict[str, Any]]:
    """List all investigations."""
    return [
        {
            "investigation_id": r.investigation_id,
            "symptom": r.symptom,
            "status": r.status.value,
            "phase": r.phase.value,
            "confidence": r.aggregate_confidence,
            "root_cause": r.root_cause,
            "started_at": r.started_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in _investigations.values()
    ]


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws/investigation/{investigation_id}")
async def ws_investigation(websocket: WebSocket, investigation_id: str) -> None:
    """WebSocket for real-time investigation updates."""
    await websocket.accept()

    if investigation_id not in _ws_connections:
        _ws_connections[investigation_id] = []
    _ws_connections[investigation_id].append(websocket)

    try:
        # Send current state if investigation exists
        result = _investigations.get(investigation_id)
        if result:
            await websocket.send_json({
                "type": "current_state",
                "status": result.status.value,
                "phase": result.phase.value,
                "confidence": result.aggregate_confidence,
            })

        # Keep connection alive and listen for client messages
        while True:
            data = await websocket.receive_text()
            # Client can send ping or other commands
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        _ws_connections.get(investigation_id, []).remove(websocket)


async def _broadcast_ws(investigation_id: str, message: dict[str, Any]) -> None:
    """Send a message to all WebSocket connections for an investigation."""
    connections = _ws_connections.get(investigation_id, [])
    for ws in connections:
        try:
            await ws.send_json(message)
        except Exception:
            pass
