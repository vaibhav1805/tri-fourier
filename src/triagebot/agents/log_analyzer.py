"""Log analyzer specialist agent with CloudWatch Logs integration.

Implements real CloudWatch Logs Insights queries, stack trace parsing,
and error pattern detection. Falls back gracefully when CloudWatch
is unavailable.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from triagebot.config.settings import get_settings
from triagebot.models.findings import DiagnosticFinding, Severity

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Time range parsing
# ---------------------------------------------------------------------------

_TIME_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_time_range(time_range: str) -> int:
    """Convert a human-readable time range (e.g., '15m', '1h') to seconds."""
    match = re.match(r"^(\d+)([smhd])$", time_range.strip())
    if not match:
        return 900  # default 15 minutes
    value, unit = int(match.group(1)), match.group(2)
    return value * _TIME_MULTIPLIERS[unit]


# ---------------------------------------------------------------------------
# CloudWatch Logs client
# ---------------------------------------------------------------------------


def _get_cloudwatch_client() -> Any:
    """Get a boto3 CloudWatch Logs client. Returns None if unavailable."""
    try:
        import boto3

        return boto3.client("logs")
    except Exception as e:
        logger.warning("cloudwatch.client_unavailable", error=str(e))
        return None


def search_cloudwatch(
    log_group: str,
    query: str,
    time_range: str = "15m",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search CloudWatch Logs Insights for matching log entries.

    Args:
        log_group: CloudWatch log group name (e.g., '/aws/eks/production/checkout-api').
        query: CloudWatch Logs Insights query string.
        time_range: How far back to search (e.g., '15m', '1h', '6h').
        limit: Maximum number of results to return.

    Returns:
        List of log entry dicts with 'timestamp' and 'message' fields.
        Returns empty list if CloudWatch is unavailable.
    """
    client = _get_cloudwatch_client()
    if client is None:
        logger.info("cloudwatch.skipped", reason="client_unavailable")
        return []

    range_seconds = _parse_time_range(time_range)
    now = int(time.time())
    start_time = now - range_seconds

    try:
        response = client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=now,
            queryString=query,
            limit=limit,
        )
        query_id = response["queryId"]

        # Poll for results (CloudWatch Insights is async)
        results: list[dict[str, Any]] = []
        for _ in range(30):  # max 30 seconds
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "")

            if status in ("Complete", "Failed", "Cancelled", "Timeout"):
                if status == "Complete":
                    results = _parse_query_results(result.get("results", []))
                else:
                    logger.warning("cloudwatch.query_status", status=status, query_id=query_id)
                break
            time.sleep(1)

        logger.info(
            "cloudwatch.search_complete",
            log_group=log_group,
            results_count=len(results),
        )
        return results

    except Exception as e:
        logger.error("cloudwatch.search_failed", log_group=log_group, error=str(e))
        return []


def _parse_query_results(raw_results: list[list[dict[str, str]]]) -> list[dict[str, Any]]:
    """Parse CloudWatch Logs Insights results into flat dicts."""
    parsed = []
    for row in raw_results:
        entry: dict[str, Any] = {}
        for field in row:
            key = field.get("field", "")
            value = field.get("value", "")
            if key.startswith("@"):
                key = key[1:]  # strip @ prefix
            entry[key] = value
        parsed.append(entry)
    return parsed


# ---------------------------------------------------------------------------
# Stack trace parsing
# ---------------------------------------------------------------------------

_JAVA_STACKTRACE_RE = re.compile(
    r"(?P<exception>[\w.]+(?:Exception|Error|Throwable)):\s*(?P<message>[^\n]+)"
    r"(?:\n\s+at\s+[\w.$<>]+\([\w.]+:\d+\))+",
    re.MULTILINE,
)

_PYTHON_TRACEBACK_RE = re.compile(
    r'Traceback \(most recent call last\):\n'
    r'(?:\s+File "[^"]+", line \d+[^\n]*\n(?:\s+[^\n]+\n)?)*'
    r'(?P<exception>\w+(?:Error|Exception|Warning)):\s*(?P<message>[^\n]+)',
    re.MULTILINE,
)


def parse_stack_traces(log_text: str) -> list[dict[str, Any]]:
    """Extract stack traces from log text.

    Supports Java and Python stack trace formats.

    Returns:
        List of dicts with 'exception', 'message', and 'language' keys.
        Returns empty list if no stack traces found.
    """
    traces = []

    for match in _JAVA_STACKTRACE_RE.finditer(log_text):
        traces.append({
            "exception": match.group("exception"),
            "message": match.group("message").strip(),
            "language": "java",
            "raw": match.group(0)[:500],
        })

    for match in _PYTHON_TRACEBACK_RE.finditer(log_text):
        traces.append({
            "exception": match.group("exception"),
            "message": match.group("message").strip(),
            "language": "python",
            "raw": match.group(0)[:500],
        })

    return traces


# ---------------------------------------------------------------------------
# Error pattern detection
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = [
    {
        "type": "connection_pool_exhaustion",
        "patterns": [
            re.compile(r"connection pool exhausted", re.IGNORECASE),
            re.compile(r"no available connections", re.IGNORECASE),
            re.compile(r"pool.*timeout", re.IGNORECASE),
            re.compile(r"active connections.*limit", re.IGNORECASE),
        ],
        "severity": "critical",
    },
    {
        "type": "oom_killed",
        "patterns": [
            re.compile(r"OOMKill", re.IGNORECASE),
            re.compile(r"out of memory", re.IGNORECASE),
            re.compile(r"exit code 137"),
            re.compile(r"memory.*exceeded", re.IGNORECASE),
        ],
        "severity": "critical",
    },
    {
        "type": "connection_refused",
        "patterns": [
            re.compile(r"connection refused", re.IGNORECASE),
            re.compile(r"ECONNREFUSED"),
            re.compile(r"connect\(\) failed", re.IGNORECASE),
        ],
        "severity": "high",
    },
    {
        "type": "timeout",
        "patterns": [
            re.compile(r"timeout", re.IGNORECASE),
            re.compile(r"deadline exceeded", re.IGNORECASE),
            re.compile(r"timed out", re.IGNORECASE),
        ],
        "severity": "high",
    },
    {
        "type": "disk_pressure",
        "patterns": [
            re.compile(r"disk.*full", re.IGNORECASE),
            re.compile(r"no space left", re.IGNORECASE),
            re.compile(r"DiskPressure"),
        ],
        "severity": "critical",
    },
    {
        "type": "certificate_error",
        "patterns": [
            re.compile(r"certificate.*expired", re.IGNORECASE),
            re.compile(r"SSL.*error", re.IGNORECASE),
            re.compile(r"TLS.*handshake", re.IGNORECASE),
        ],
        "severity": "high",
    },
]


def detect_error_patterns(
    log_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect known error patterns in a list of log entries.

    Args:
        log_entries: List of log entry dicts with at least a 'message' field.

    Returns:
        List of detected patterns with type, severity, count, and sample evidence.
    """
    detections: dict[str, dict[str, Any]] = {}

    for entry in log_entries:
        message = entry.get("message", "")
        for pattern_def in _ERROR_PATTERNS:
            pattern_type = pattern_def["type"]
            for regex in pattern_def["patterns"]:
                if regex.search(message):
                    if pattern_type not in detections:
                        detections[pattern_type] = {
                            "type": pattern_type,
                            "severity": pattern_def["severity"],
                            "count": 0,
                            "evidence": [],
                        }
                    detections[pattern_type]["count"] += 1
                    if len(detections[pattern_type]["evidence"]) < 3:
                        detections[pattern_type]["evidence"].append(
                            message[:200]
                        )
                    break  # one match per pattern group per entry

    return list(detections.values())


# ---------------------------------------------------------------------------
# Event correlation
# ---------------------------------------------------------------------------


def correlate_events(
    log_entries: list[dict[str, Any]],
    window_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Correlate error events within a time window across services.

    Groups errors that occur within `window_seconds` of each other
    across different services, indicating a potential shared root cause.

    Args:
        log_entries: Log entries with 'timestamp', 'message', and optionally 'service'.
        window_seconds: Time window for correlation (default 5 minutes).

    Returns:
        List of correlated event groups.
    """
    # Parse timestamps and sort
    timed_entries = []
    for entry in log_entries:
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            timed_entries.append((ts, entry))
        except (ValueError, TypeError):
            continue

    timed_entries.sort(key=lambda x: x[0])

    # Sliding window grouping
    groups: list[dict[str, Any]] = []
    used: set[int] = set()

    for i, (ts_i, entry_i) in enumerate(timed_entries):
        if i in used:
            continue
        group_entries = [entry_i]
        group_services = {entry_i.get("service", "unknown")}
        used.add(i)

        for j in range(i + 1, len(timed_entries)):
            if j in used:
                continue
            ts_j, entry_j = timed_entries[j]
            delta = (ts_j - ts_i).total_seconds()
            if delta > window_seconds:
                break
            svc = entry_j.get("service", "unknown")
            if svc != entry_i.get("service", "unknown"):
                group_entries.append(entry_j)
                group_services.add(svc)
                used.add(j)

        if len(group_services) > 1:
            groups.append({
                "services": sorted(group_services),
                "count": len(group_entries),
                "window_start": timed_entries[i][0].isoformat(),
                "window_end": timed_entries[max(used & set(range(i, len(timed_entries))))][0].isoformat() if used else ts_i.isoformat(),
                "entries": group_entries[:5],
            })

    return groups


# ---------------------------------------------------------------------------
# High-level analyze function (used by orchestrator tool)
# ---------------------------------------------------------------------------


def analyze_logs(
    services: list[str],
    query: str,
    time_range: str = "15m",
    namespace: str = "default",
) -> list[DiagnosticFinding]:
    """Run full log analysis pipeline for the given services.

    1. Search CloudWatch for each service
    2. Parse stack traces
    3. Detect error patterns
    4. Correlate events across services
    5. Return structured findings

    Falls back to empty findings if CloudWatch is unavailable.
    """
    settings = get_settings()
    all_entries: list[dict[str, Any]] = []
    findings: list[DiagnosticFinding] = []

    for service in services:
        log_group = f"/aws/eks/{namespace}/{service}"
        cw_query = (
            f"fields @timestamp, @message, @logStream "
            f"| filter @message like /{query}/ "
            f"| sort @timestamp desc "
            f"| limit 200"
        )
        entries = search_cloudwatch(
            log_group=log_group,
            query=cw_query,
            time_range=time_range,
        )
        # Tag entries with service name
        for entry in entries:
            entry["service"] = service
        all_entries.extend(entries)

    if not all_entries:
        logger.info("log_analyzer.no_entries", services=services, query=query)
        return findings

    # Parse stack traces from all entries
    all_text = "\n".join(e.get("message", "") for e in all_entries)
    traces = parse_stack_traces(all_text)

    # Detect error patterns
    patterns = detect_error_patterns(all_entries)

    # Correlate events
    correlations = correlate_events(all_entries)

    # Build findings from patterns
    for pattern in patterns:
        severity_map = {"critical": Severity.CRITICAL, "high": Severity.HIGH, "medium": Severity.MEDIUM, "low": Severity.LOW}
        sev = severity_map.get(pattern["severity"], Severity.MEDIUM)
        confidence = min(0.95, 0.5 + (pattern["count"] * 0.05))

        evidence = [f"Detected {pattern['count']} occurrences of {pattern['type']}"]
        evidence.extend(pattern["evidence"][:3])

        affected = list({e.get("service", s) for e in all_entries for s in services if pattern["type"] in str(e.get("message", "")).lower() or True}.__and__(set(services)))
        if not affected:
            affected = services

        findings.append(DiagnosticFinding(
            source="log-analyzer",
            severity=sev,
            confidence=confidence,
            summary=f"{pattern['type'].replace('_', ' ').title()} detected in {', '.join(affected)}",
            evidence=evidence,
            affected_services=affected,
            suggested_remediation=_suggest_remediation(pattern["type"]),
            raw_data={"pattern": pattern, "traces": traces[:3]},
        ))

    # If stack traces found but no patterns, add a generic trace finding
    if traces and not patterns:
        findings.append(DiagnosticFinding(
            source="log-analyzer",
            severity=Severity.HIGH,
            confidence=0.7,
            summary=f"Exception detected: {traces[0]['exception']}: {traces[0]['message']}",
            evidence=[t["raw"][:200] for t in traces[:3]],
            affected_services=services,
            suggested_remediation="Investigate the exception and fix the root cause",
            raw_data={"traces": traces},
        ))

    # Add correlation finding if cross-service errors detected
    if correlations:
        corr = correlations[0]
        findings.append(DiagnosticFinding(
            source="log-analyzer",
            severity=Severity.HIGH,
            confidence=0.8,
            summary=f"Correlated errors across {', '.join(corr['services'])} within {corr.get('window_start', 'unknown')}",
            evidence=[f"{corr['count']} correlated events across {len(corr['services'])} services"],
            affected_services=corr["services"],
            raw_data={"correlations": correlations},
        ))

    return findings


def _suggest_remediation(pattern_type: str) -> str | None:
    """Suggest remediation based on detected error pattern."""
    suggestions = {
        "connection_pool_exhaustion": "Increase connection pool size or investigate connection leaks",
        "oom_killed": "Increase memory limits or investigate memory leaks",
        "connection_refused": "Check target service health and network policies",
        "timeout": "Check downstream service latency and adjust timeout settings",
        "disk_pressure": "Clear disk space or increase PVC size",
        "certificate_error": "Renew or rotate TLS certificates",
    }
    return suggestions.get(pattern_type)
