"""Core data models for diagnostic findings and investigation state."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Phase(StrEnum):
    """Orchestrator workflow phases (ARCHITECTURE.md Section 6.1)."""

    INTAKE = "intake"
    TRIAGE = "triage"
    DIAGNOSE = "diagnose"
    SYNTHESIZE = "synthesize"
    REMEDIATE = "remediate"
    VERIFY = "verify"
    COMPLETE = "complete"
    REPORT = "report"


# Valid phase transitions per the architecture graph pattern
PHASE_TRANSITIONS: dict[Phase, list[Phase]] = {
    Phase.INTAKE: [Phase.TRIAGE],
    Phase.TRIAGE: [Phase.DIAGNOSE],
    Phase.DIAGNOSE: [Phase.SYNTHESIZE],
    Phase.SYNTHESIZE: [Phase.REMEDIATE, Phase.REPORT],
    Phase.REMEDIATE: [Phase.VERIFY],
    Phase.VERIFY: [Phase.COMPLETE],
    Phase.REPORT: [Phase.COMPLETE],
    Phase.COMPLETE: [],
}


class InvalidTransition(Exception):
    """Raised when an invalid phase transition is attempted."""

    def __init__(self, current: Phase, attempted: Phase) -> None:
        super().__init__(f"Invalid transition: {current} -> {attempted}")
        self.current = current
        self.attempted = attempted


def validate_transition(current: Phase, target: Phase) -> None:
    """Raise InvalidTransition if the transition is not allowed."""
    if target not in PHASE_TRANSITIONS.get(current, []):
        raise InvalidTransition(current, target)


class Severity(StrEnum):
    """Finding severity levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ConfidenceLevel(StrEnum):
    """Remediation gating classification (ARCHITECTURE.md Section 6.2)."""

    AUTO_REMEDIATE = "auto_remediate"
    APPROVAL_REQUIRED = "approval_required"
    HUMAN_APPROVAL = "human_approval"
    REPORT_ONLY = "report_only"


class InvestigationStatus(StrEnum):
    """Top-level status of an investigation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class DiagnosticFinding(BaseModel):
    """Structured result from a specialist sub-agent (ARCHITECTURE.md Section 6.3)."""

    source: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    evidence: list[str]
    affected_services: list[str]
    suggested_remediation: str | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


class RemediationAction(BaseModel):
    """A proposed remediation action."""

    action_type: str  # e.g., "restart_pod", "scale_deployment", "rollback"
    target: str  # e.g., "checkout-api"
    namespace: str = "default"
    parameters: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_approval: bool = True
    dry_run: bool = True


class InvestigationResult(BaseModel):
    """Complete result of a triage investigation."""

    investigation_id: str
    symptom: str
    status: InvestigationStatus = InvestigationStatus.PENDING
    phase: Phase = Phase.INTAKE
    findings: list[DiagnosticFinding] = Field(default_factory=list)
    root_cause: str | None = None
    aggregate_confidence: float = 0.0
    confidence_level: ConfidenceLevel = ConfidenceLevel.REPORT_ONLY
    remediation: RemediationAction | None = None
    affected_services: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
