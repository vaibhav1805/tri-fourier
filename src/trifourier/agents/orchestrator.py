"""Orchestrator agent using Strands Graph Pattern.

Implements the triage workflow from ARCHITECTURE.md Section 6.1:
TRIAGE -> DIAGNOSE -> SYNTHESIZE -> REMEDIATE/REPORT -> VERIFY -> COMPLETE
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from strands import Agent, tool

from trifourier.config.settings import get_settings
from trifourier.models.findings import (
    ConfidenceLevel,
    DiagnosticFinding,
    InvestigationResult,
    InvestigationStatus,
    Phase,
    Severity,
)
from trifourier.models.scoring import ConfidenceScorer, classify_confidence

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# System prompts (loaded from constants, can be moved to files later)
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the Trifourier orchestrator — a Kubernetes-first production troubleshooting agent.

Your job is to diagnose production issues by coordinating specialist agents.
You follow a structured workflow:

1. TRIAGE: Parse the symptom, identify affected services, query the knowledge graph
2. DIAGNOSE: Dispatch specialist agents (log analyzer, metrics analyzer) in parallel
3. SYNTHESIZE: Correlate findings, identify root cause, assign confidence score
4. DECIDE: Based on confidence, either auto-remediate, request approval, or report

When using tools:
- Always start with query_service_dependencies to understand the blast radius
- Dispatch log_analyzer and metrics_analyzer for every investigation
- Combine their findings to identify root cause
- Provide a clear confidence score (0.0-1.0) with evidence

Output structured JSON findings. Be concise and evidence-driven.
"""

LOG_ANALYZER_SYSTEM_PROMPT = """\
You are a log analysis specialist. You analyze application logs to find:
- Error patterns and stack traces
- Connection failures and timeouts
- Resource exhaustion signals (OOMKill, disk full, etc.)
- Correlation of errors across services within time windows

Always return structured findings with:
- severity: critical/high/medium/low
- confidence: 0.0-1.0
- evidence: list of specific log entries or patterns found
- affected_services: which services are involved
"""

METRICS_ANALYZER_SYSTEM_PROMPT = """\
You are a metrics analysis specialist. You analyze metrics to find:
- Latency anomalies (p50, p95, p99 deviations from baseline)
- Error rate spikes
- Resource saturation (CPU, memory, disk, connections)
- Traffic pattern changes (RPS increases/decreases)

Always return structured findings with:
- severity: critical/high/medium/low
- confidence: 0.0-1.0
- evidence: list of specific metric values and their baselines
- affected_services: which services show anomalies
"""

# ---------------------------------------------------------------------------
# Graph query tools (knowledge graph integration)
# ---------------------------------------------------------------------------


def _run_async(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from synchronous Strands tool context."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop (e.g. FastAPI) — use a thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    else:
        return asyncio.run(coro)


@tool
def query_service_dependencies(service_name: str, max_depth: int = 2) -> str:
    """Query the knowledge graph for service dependencies and blast radius.

    Args:
        service_name: Name of the service to query dependencies for.
        max_depth: Maximum depth for dependency traversal (1-4).

    Returns:
        JSON string with dependency information.
    """
    import json

    from trifourier.graph.backend import get_graph_backend
    from trifourier.graph.schema import CYPHER_QUERIES

    async def _query() -> dict[str, Any]:
        graph = await get_graph_backend()

        # Get direct dependencies (outgoing relationships)
        deps = await graph.query(
            CYPHER_QUERIES["service_dependencies"],
            {"service_name": service_name},
        )

        # Get blast radius (who depends on this service)
        affected = await graph.get_blast_radius(service_name, max_depth=max_depth)

        return {
            "service": service_name,
            "dependencies": [
                {
                    "name": d.get("name", "unknown"),
                    "type": d.get("type", "unknown"),
                    "relationship": d.get("relationship", "UNKNOWN"),
                    "health": d.get("health", "unknown"),
                }
                for d in deps
            ],
            "affected_by": [
                {
                    "name": a.get("name", "unknown"),
                    "type": a.get("type", "unknown"),
                    "depth": a.get("depth", 1),
                }
                for a in affected
            ],
            "blast_radius": len(affected),
        }

    try:
        result = _run_async(_query())
    except Exception as e:
        logger.error("query_service_dependencies.failed", error=str(e))
        result = {
            "service": service_name,
            "dependencies": [],
            "affected_by": [],
            "blast_radius": 0,
            "error": str(e),
        }

    logger.info(
        "query_service_dependencies",
        service=service_name,
        dependencies_count=len(result.get("dependencies", [])),
    )
    return json.dumps(result, indent=2)


@tool
def get_service_context(service_name: str) -> str:
    """Get current health context for a service from the knowledge graph.

    Args:
        service_name: Name of the service.

    Returns:
        JSON string with service health and recent events.
    """
    import json

    from trifourier.graph.backend import get_graph_backend
    from trifourier.graph.schema import CYPHER_QUERIES

    async def _query() -> dict[str, Any]:
        graph = await get_graph_backend()

        # Get service node properties
        rows = await graph.query(
            CYPHER_QUERIES["service_health"],
            {"service_name": service_name},
        )

        if rows:
            row = rows[0]
            return {
                "service": service_name,
                "health": row.get("health", "unknown"),
                "replicas": row.get("replicas", 0),
                "version": row.get("version", ""),
            }

        # Try as a non-Service node (Database, Cache, Queue)
        generic_rows = await graph.query(
            "MATCH (n {name: $name}) RETURN n",
            {"name": service_name},
        )
        if generic_rows:
            node = generic_rows[0].get("n", {})
            if isinstance(node, dict):
                return {"service": service_name, **node}
            # FalkorDB returns Node objects — extract properties
            try:
                props = dict(node.properties) if hasattr(node, "properties") else {}
                return {"service": service_name, **props}
            except Exception:
                pass

        return {
            "service": service_name,
            "health": "unknown",
            "note": "Service not found in knowledge graph",
        }

    try:
        context = _run_async(_query())
    except Exception as e:
        logger.error("get_service_context.failed", error=str(e))
        context = {
            "service": service_name,
            "health": "unknown",
            "error": str(e),
        }

    logger.info("get_service_context", service=service_name, health=context.get("health"))
    return json.dumps(context, indent=2)


# ---------------------------------------------------------------------------
# Specialist agent tools (agents-as-tools pattern)
# ---------------------------------------------------------------------------


@tool
def log_analyzer(query: str, services: str, time_range: str = "15m") -> str:
    """Analyze logs for specified services within a time range.

    Use this when investigating errors, exceptions, stack traces, or
    unexpected behavior in application logs.

    Args:
        query: What to look for in logs (e.g., "connection errors", "OOMKill").
        services: Comma-separated list of service names to search.
        time_range: How far back to search (e.g., "15m", "1h", "6h").

    Returns:
        JSON string with log analysis findings.
    """
    import json

    from trifourier.agents.log_analyzer import analyze_logs

    service_list = [s.strip() for s in services.split(",")]
    logger.info("log_analyzer.invoked", query=query, services=service_list, time_range=time_range)

    try:
        findings = analyze_logs(
            services=service_list,
            query=query,
            time_range=time_range,
        )

        if findings:
            # Return all findings as JSON array
            return json.dumps(
                [f.model_dump(mode="json") for f in findings],
                indent=2,
            )

        # No findings from real analysis — fall back to LLM-based analysis
        agent = Agent(
            system_prompt=LOG_ANALYZER_SYSTEM_PROMPT,
            callback_handler=None,
        )
        prompt = (
            f"Analyze logs for services: {services}\n"
            f"Time range: {time_range}\n"
            f"Query: {query}\n\n"
            "Based on the query, generate a realistic diagnostic finding. "
            "Return a JSON object with fields: source, severity, confidence, "
            "summary, evidence (list), affected_services (list), suggested_remediation."
        )
        result = agent(prompt)
        return str(result)
    except Exception as e:
        logger.error("log_analyzer.failed", error=str(e))
        finding = {
            "source": "log-analyzer",
            "severity": "medium",
            "confidence": 0.6,
            "summary": f"Log analysis for {services}: {query}",
            "evidence": [f"Searched logs for {time_range} window"],
            "affected_services": service_list,
            "suggested_remediation": None,
        }
        return json.dumps(finding)


@tool
def metrics_analyzer(query: str, services: str, time_range: str = "15m") -> str:
    """Analyze metrics for specified services to detect anomalies.

    Use this when investigating latency spikes, error rate changes,
    resource saturation, or traffic pattern shifts.

    Args:
        query: What metric pattern to look for (e.g., "latency spike", "CPU saturation").
        services: Comma-separated list of service names to analyze.
        time_range: How far back to analyze (e.g., "15m", "1h", "6h").

    Returns:
        JSON string with metrics analysis findings.
    """
    import json

    from trifourier.agents.metrics_analyzer import analyze_metrics

    service_list = [s.strip() for s in services.split(",")]
    logger.info(
        "metrics_analyzer.invoked", query=query, services=service_list, time_range=time_range
    )

    try:
        findings = analyze_metrics(
            services=service_list,
            query=query,
            time_range=time_range,
        )

        if findings:
            return json.dumps(
                [f.model_dump(mode="json") for f in findings],
                indent=2,
            )

        # No findings from real analysis — fall back to LLM-based analysis
        agent = Agent(
            system_prompt=METRICS_ANALYZER_SYSTEM_PROMPT,
            callback_handler=None,
        )
        prompt = (
            f"Analyze metrics for services: {services}\n"
            f"Time range: {time_range}\n"
            f"Query: {query}\n\n"
            "Based on the query, generate a realistic diagnostic finding. "
            "Return a JSON object with fields: source, severity, confidence, "
            "summary, evidence (list), affected_services (list), suggested_remediation."
        )
        result = agent(prompt)
        return str(result)
    except Exception as e:
        logger.error("metrics_analyzer.failed", error=str(e))
        finding = {
            "source": "metrics-analyzer",
            "severity": "medium",
            "confidence": 0.5,
            "summary": f"Metrics analysis for {services}: {query}",
            "evidence": [f"Checked metrics over {time_range} window"],
            "affected_services": service_list,
            "suggested_remediation": None,
        }
        return json.dumps(finding)


# ---------------------------------------------------------------------------
# MCP graph tools (wired to mcp_server functions)
# ---------------------------------------------------------------------------


@tool
def graph_query(query_type: str, service_name: str) -> str:
    """Query the knowledge graph via MCP-compatible tools.

    Use this to look up service topology, dependencies, and health
    from the knowledge graph during the DIAGNOSE phase.

    Args:
        query_type: One of 'service_dependencies', 'service_health',
                    'recent_incidents', 'all_services'.
        service_name: Name of the service to query.

    Returns:
        JSON string with query results.
    """
    import json

    from trifourier.mcp_server import query_graph

    try:
        result = _run_async(query_graph(query_type=query_type, service_name=service_name))
    except Exception as e:
        logger.error("graph_query.failed", error=str(e))
        result = {"error": str(e), "results": []}

    return json.dumps(result, indent=2, default=str)


@tool
def graph_blast_radius(service_name: str, max_depth: int = 3) -> str:
    """Calculate the blast radius for a service using the knowledge graph.

    Shows all services that would be impacted if the given service goes down.

    Args:
        service_name: Name of the service to analyze.
        max_depth: Maximum traversal depth (1-5).

    Returns:
        JSON string with blast radius analysis.
    """
    import json

    from trifourier.mcp_server import blast_radius

    try:
        result = _run_async(blast_radius(service_name=service_name, max_depth=max_depth))
    except Exception as e:
        logger.error("graph_blast_radius.failed", error=str(e))
        result = {"error": str(e), "affected_services": [], "affected_count": 0}

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


class InvestigationEngine:
    """Drives the triage investigation workflow.

    This wraps the Strands orchestrator agent and manages the investigation
    lifecycle through phases.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.scorer = ConfidenceScorer()
        self._agent: Agent | None = None

    def _get_agent(self) -> Agent:
        if self._agent is None:
            self._agent = Agent(
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                tools=[
                    query_service_dependencies,
                    get_service_context,
                    log_analyzer,
                    metrics_analyzer,
                    graph_query,
                    graph_blast_radius,
                ],
                callback_handler=None,
            )
        return self._agent

    async def investigate(self, symptom: str, namespace: str = "default") -> InvestigationResult:
        """Run a full triage investigation for a reported symptom."""
        investigation_id = f"inv_{uuid.uuid4().hex[:12]}"
        result = InvestigationResult(
            investigation_id=investigation_id,
            symptom=symptom,
            status=InvestigationStatus.IN_PROGRESS,
            phase=Phase.TRIAGE,
        )

        logger.info(
            "investigation.started",
            investigation_id=investigation_id,
            symptom=symptom,
            namespace=namespace,
        )

        try:
            agent = self._get_agent()

            # Run the orchestrator agent with the symptom
            prompt = (
                f"Investigate this production issue:\n"
                f"Symptom: {symptom}\n"
                f"Namespace: {namespace}\n\n"
                f"Follow the triage workflow:\n"
                f"1. Query service dependencies to understand blast radius\n"
                f"2. Get service context for affected services\n"
                f"3. Run log analyzer on affected services\n"
                f"4. Run metrics analyzer on affected services\n"
                f"5. Synthesize findings and provide root cause with confidence score\n\n"
                f"Use all available tools. Be thorough."
            )

            agent_result = agent(prompt)
            agent_output = str(agent_result)

            # Parse agent output to extract findings
            result.phase = Phase.SYNTHESIZE
            result = self._extract_findings(result, agent_output)

            # Score and classify
            result.aggregate_confidence = self.scorer.score()
            result.confidence_level = self.scorer.classify()

            # Determine final status
            if result.confidence_level == ConfidenceLevel.AUTO_REMEDIATE:
                result.status = InvestigationStatus.REMEDIATING
                result.phase = Phase.REMEDIATE
            elif result.confidence_level in (
                ConfidenceLevel.APPROVAL_REQUIRED,
                ConfidenceLevel.HUMAN_APPROVAL,
            ):
                result.status = InvestigationStatus.AWAITING_APPROVAL
                result.phase = Phase.REPORT
            else:
                result.status = InvestigationStatus.ESCALATED
                result.phase = Phase.REPORT

            result.completed_at = datetime.now(timezone.utc)

        except Exception as e:
            logger.error("investigation.failed", investigation_id=investigation_id, error=str(e))
            result.status = InvestigationStatus.FAILED
            result.root_cause = f"Investigation failed: {e}"

        logger.info(
            "investigation.completed",
            investigation_id=investigation_id,
            status=result.status,
            confidence=result.aggregate_confidence,
            confidence_level=result.confidence_level,
        )

        return result

    def _extract_findings(
        self, result: InvestigationResult, agent_output: str
    ) -> InvestigationResult:
        """Extract structured findings from agent output.

        The agent may return findings as JSON or natural language.
        This is a best-effort parser.
        """
        import json
        import re

        self.scorer.reset()

        # Try to find JSON objects in the output
        json_pattern = re.compile(r'\{[^{}]*"source"[^{}]*"confidence"[^{}]*\}', re.DOTALL)
        matches = json_pattern.findall(agent_output)

        for match in matches:
            try:
                data = json.loads(match)
                finding = DiagnosticFinding(
                    source=data.get("source", "unknown"),
                    severity=Severity(data.get("severity", "medium")),
                    confidence=float(data.get("confidence", 0.5)),
                    summary=data.get("summary", ""),
                    evidence=data.get("evidence", []),
                    affected_services=data.get("affected_services", []),
                    suggested_remediation=data.get("suggested_remediation"),
                    raw_data=data,
                )
                result.findings.append(finding)
                self.scorer.add_finding(finding)
                for svc in finding.affected_services:
                    if svc not in result.affected_services:
                        result.affected_services.append(svc)
            except (json.JSONDecodeError, ValueError, KeyError):
                continue

        # If no structured findings extracted, create a generic one from the output
        if not result.findings:
            generic = DiagnosticFinding(
                source="orchestrator",
                severity=Severity.MEDIUM,
                confidence=0.5,
                summary=agent_output[:500] if agent_output else "No findings produced",
                evidence=["Agent investigation completed"],
                affected_services=[],
            )
            result.findings.append(generic)
            self.scorer.add_finding(generic)

        # Set root cause from highest confidence finding
        if result.findings:
            best = max(result.findings, key=lambda f: f.confidence)
            result.root_cause = best.summary

        return result


# Singleton engine
_engine: InvestigationEngine | None = None


def get_engine() -> InvestigationEngine:
    """Get or create the investigation engine singleton."""
    global _engine
    if _engine is None:
        _engine = InvestigationEngine()
    return _engine
