"""Metrics analyzer specialist agent with Prometheus and CloudWatch Metrics integration.

Implements real Prometheus PromQL queries and CloudWatch Metrics queries
for pod-level metrics: CPU, memory, latency, error rates.
Falls back gracefully when data sources are unavailable.
"""

from __future__ import annotations

import re
import time
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
        return 900
    value, unit = int(match.group(1)), match.group(2)
    return value * _TIME_MULTIPLIERS[unit]


# ---------------------------------------------------------------------------
# Prometheus client
# ---------------------------------------------------------------------------


def query_prometheus(
    query: str,
    time_range: str = "15m",
    step: str = "60s",
) -> dict[str, Any]:
    """Execute a PromQL range query against Prometheus.

    Args:
        query: PromQL query string.
        time_range: How far back to query (e.g., '15m', '1h').
        step: Query resolution step (e.g., '60s', '5m').

    Returns:
        Prometheus API response dict with 'status' and 'data' keys.
        Returns empty result if Prometheus is unavailable.
    """
    settings = get_settings()
    prometheus_url = getattr(settings, "prometheus_url", "http://localhost:9090")

    range_seconds = _parse_time_range(time_range)
    now = int(time.time())
    start_time = now - range_seconds

    try:
        import httpx

        response = httpx.get(
            f"{prometheus_url}/api/v1/query_range",
            params={
                "query": query,
                "start": start_time,
                "end": now,
                "step": step,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

        logger.info(
            "prometheus.query_complete",
            query=query[:100],
            results_count=len(data.get("data", {}).get("result", [])),
        )
        return data

    except Exception as e:
        logger.warning("prometheus.query_failed", query=query[:100], error=str(e))
        return {"status": "error", "data": {"resultType": "matrix", "result": []}}


# ---------------------------------------------------------------------------
# CloudWatch Metrics client
# ---------------------------------------------------------------------------


def query_cloudwatch_metrics(
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    time_range: str = "15m",
    period: int = 60,
    stat: str = "Average",
) -> list[dict[str, Any]]:
    """Query CloudWatch Metrics for time series data.

    Args:
        namespace: CloudWatch namespace (e.g., 'AWS/EKS', 'ContainerInsights').
        metric_name: Metric name (e.g., 'CPUUtilization', 'MemoryUtilization').
        dimensions: List of dimension dicts with 'Name' and 'Value'.
        time_range: How far back to query (e.g., '15m', '1h').
        period: Data point interval in seconds.
        stat: Statistic type ('Average', 'Maximum', 'Sum', 'p99', etc.).

    Returns:
        List of datapoint dicts with 'Timestamp' and value fields.
        Returns empty list if CloudWatch is unavailable.
    """
    try:
        import boto3
        from datetime import datetime, timezone, timedelta

        client = boto3.client("cloudwatch")
        range_seconds = _parse_time_range(time_range)
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(seconds=range_seconds)

        # Handle extended statistics (percentiles)
        kwargs: dict[str, Any] = {
            "Namespace": namespace,
            "MetricName": metric_name,
            "Dimensions": [{"Name": d["Name"], "Value": d["Value"]} for d in dimensions],
            "StartTime": start_time,
            "EndTime": end_time,
            "Period": period,
        }

        if stat.startswith("p"):
            kwargs["ExtendedStatistics"] = [stat]
        else:
            kwargs["Statistics"] = [stat]

        response = client.get_metric_statistics(**kwargs)
        datapoints = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])

        logger.info(
            "cloudwatch_metrics.query_complete",
            namespace=namespace,
            metric=metric_name,
            datapoints_count=len(datapoints),
        )
        return datapoints

    except Exception as e:
        logger.warning(
            "cloudwatch_metrics.query_failed",
            namespace=namespace,
            metric=metric_name,
            error=str(e),
        )
        return []


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def detect_anomalies(
    values: list[tuple[float, str]],
    stddev_threshold: float = 3.0,
) -> list[dict[str, Any]]:
    """Detect anomalies in a time series using z-score method.

    Args:
        values: List of (timestamp, value_str) tuples from Prometheus.
        stddev_threshold: Number of standard deviations to flag (default 3.0).

    Returns:
        List of anomaly dicts with timestamp, value, deviation info.
    """
    if len(values) < 3:
        return []

    floats = []
    for ts, val in values:
        try:
            floats.append((float(ts), float(val)))
        except (ValueError, TypeError):
            continue

    if len(floats) < 3:
        return []

    vals = [v for _, v in floats]
    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    stddev = variance ** 0.5

    if stddev == 0:
        return []

    anomalies = []
    for ts, val in floats:
        z_score = abs(val - mean) / stddev
        if z_score >= stddev_threshold:
            anomalies.append({
                "timestamp": ts,
                "value": val,
                "mean": round(mean, 4),
                "stddev": round(stddev, 4),
                "z_score": round(z_score, 2),
                "type": "spike" if val > mean else "drop",
            })

    return anomalies


# ---------------------------------------------------------------------------
# Saturation detection
# ---------------------------------------------------------------------------


def detect_saturation(
    values: list[tuple[float, str]],
    threshold: float = 0.9,
) -> dict[str, Any]:
    """Detect resource saturation in a time series.

    Args:
        values: List of (timestamp, value_str) tuples (values should be 0.0-1.0 ratios).
        threshold: Saturation threshold (default 0.9 = 90%).

    Returns:
        Dict with 'saturated' bool, peak value, and timestamp.
    """
    if not values:
        return {"saturated": False}

    peak_val = 0.0
    peak_ts = 0.0

    for ts, val in values:
        try:
            v = float(val)
            if v > peak_val:
                peak_val = v
                peak_ts = float(ts)
        except (ValueError, TypeError):
            continue

    return {
        "saturated": peak_val >= threshold,
        "peak_value": round(peak_val, 4),
        "peak_timestamp": peak_ts,
        "threshold": threshold,
        "percentage": round(peak_val * 100, 1),
    }


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def compare_to_baseline(
    current_values: list[tuple[float, str]],
    baseline_mean: float,
    baseline_stddev: float,
    deviation_threshold: float = 3.0,
) -> dict[str, Any]:
    """Compare current metric values against a historical baseline.

    Args:
        current_values: Current time series values.
        baseline_mean: Historical mean value.
        baseline_stddev: Historical standard deviation.
        deviation_threshold: Number of stddevs to flag.

    Returns:
        Dict with deviation analysis.
    """
    if not current_values or baseline_stddev == 0:
        return {"significant": False}

    current_vals = []
    for _, val in current_values:
        try:
            current_vals.append(float(val))
        except (ValueError, TypeError):
            continue

    if not current_vals:
        return {"significant": False}

    current_mean = sum(current_vals) / len(current_vals)
    deviation = abs(current_mean - baseline_mean) / baseline_stddev

    return {
        "significant": deviation >= deviation_threshold,
        "current_mean": round(current_mean, 4),
        "baseline_mean": round(baseline_mean, 4),
        "baseline_stddev": round(baseline_stddev, 4),
        "deviation_stddevs": round(deviation, 2),
        "direction": "above" if current_mean > baseline_mean else "below",
    }


# ---------------------------------------------------------------------------
# High-level analyze function (used by orchestrator tool)
# ---------------------------------------------------------------------------

# Standard PromQL queries for common metrics
_PROMQL_QUERIES = {
    "latency_p99": 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[5m])) by (le))',
    "latency_p50": 'histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[5m])) by (le))',
    "error_rate": 'sum(rate(http_requests_total{{service="{service}", status=~"5.."}}[5m])) / sum(rate(http_requests_total{{service="{service}"}}[5m]))',
    "cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{pod=~"{service}-.*"}}[5m])) by (pod)',
    "memory_usage": 'sum(container_memory_working_set_bytes{{pod=~"{service}-.*"}}) by (pod) / sum(container_spec_memory_limit_bytes{{pod=~"{service}-.*"}}) by (pod)',
    "rps": 'sum(rate(http_requests_total{{service="{service}"}}[5m]))',
    "restarts": 'sum(kube_pod_container_status_restarts_total{{pod=~"{service}-.*"}})',
}


def analyze_metrics(
    services: list[str],
    query: str,
    time_range: str = "15m",
    namespace: str = "default",
) -> list[DiagnosticFinding]:
    """Run full metrics analysis pipeline for the given services.

    1. Query Prometheus for latency, errors, CPU, memory per service
    2. Query CloudWatch Metrics as fallback/supplement
    3. Detect anomalies and saturation
    4. Return structured findings

    Falls back to empty findings if all sources are unavailable.
    """
    findings: list[DiagnosticFinding] = []

    for service in services:
        service_findings = _analyze_service_metrics(service, query, time_range, namespace)
        findings.extend(service_findings)

    if not findings:
        logger.info("metrics_analyzer.no_findings", services=services, query=query)

    return findings


def _analyze_service_metrics(
    service: str,
    query: str,
    time_range: str,
    namespace: str,
) -> list[DiagnosticFinding]:
    """Analyze all key metrics for a single service."""
    findings: list[DiagnosticFinding] = []

    # Query Prometheus for each metric type
    metrics_data: dict[str, Any] = {}
    for metric_name, promql_template in _PROMQL_QUERIES.items():
        promql = promql_template.format(service=service)
        result = query_prometheus(promql, time_range=time_range)
        prom_results = result.get("data", {}).get("result", [])
        if prom_results:
            metrics_data[metric_name] = prom_results[0].get("values", [])

    # If Prometheus data is missing, try CloudWatch
    if not metrics_data:
        cw_data = _query_cloudwatch_for_service(service, namespace, time_range)
        if cw_data:
            metrics_data.update(cw_data)

    if not metrics_data:
        return findings

    # Check latency anomalies
    for latency_key in ("latency_p99", "latency_p50"):
        if latency_key in metrics_data:
            anomalies = detect_anomalies(metrics_data[latency_key])
            if anomalies:
                worst = max(anomalies, key=lambda a: a["z_score"])
                findings.append(DiagnosticFinding(
                    source="metrics-analyzer",
                    severity=Severity.HIGH if worst["z_score"] > 5 else Severity.MEDIUM,
                    confidence=min(0.95, 0.6 + (worst["z_score"] * 0.05)),
                    summary=f"Latency {worst['type']} on {service}: {worst['value']:.3f}s ({latency_key}, {worst['z_score']}x stddev)",
                    evidence=[
                        f"{latency_key} = {worst['value']:.3f}s (mean: {worst['mean']:.3f}s, stddev: {worst['stddev']:.3f}s)",
                        f"{len(anomalies)} anomalous data points detected",
                    ],
                    affected_services=[service],
                    suggested_remediation="Investigate downstream dependencies for latency. Consider scaling or caching.",
                    raw_data={"anomalies": anomalies, "metric": latency_key},
                ))

    # Check error rate
    if "error_rate" in metrics_data:
        anomalies = detect_anomalies(metrics_data["error_rate"], stddev_threshold=2.0)
        if anomalies:
            worst = max(anomalies, key=lambda a: a["value"])
            error_pct = worst["value"] * 100
            sev = Severity.CRITICAL if error_pct > 10 else Severity.HIGH if error_pct > 5 else Severity.MEDIUM
            findings.append(DiagnosticFinding(
                source="metrics-analyzer",
                severity=sev,
                confidence=min(0.95, 0.7 + (error_pct * 0.02)),
                summary=f"Error rate spike on {service}: {error_pct:.1f}%",
                evidence=[
                    f"Error rate: {error_pct:.1f}% (mean: {worst['mean']*100:.1f}%)",
                    f"Z-score: {worst['z_score']}",
                ],
                affected_services=[service],
                suggested_remediation="Check application logs for error details. Possible deployment regression.",
                raw_data={"anomalies": anomalies, "metric": "error_rate"},
            ))

    # Check CPU saturation
    if "cpu_usage" in metrics_data:
        saturation = detect_saturation(metrics_data["cpu_usage"])
        if saturation["saturated"]:
            findings.append(DiagnosticFinding(
                source="metrics-analyzer",
                severity=Severity.CRITICAL,
                confidence=0.9,
                summary=f"CPU saturation on {service}: {saturation['percentage']}%",
                evidence=[
                    f"CPU usage peaked at {saturation['percentage']}% (threshold: {saturation['threshold']*100}%)",
                ],
                affected_services=[service],
                suggested_remediation="Scale horizontally (increase replicas) or optimize CPU-intensive code paths.",
                raw_data={"saturation": saturation, "metric": "cpu_usage"},
            ))

    # Check memory saturation
    if "memory_usage" in metrics_data:
        saturation = detect_saturation(metrics_data["memory_usage"])
        if saturation["saturated"]:
            findings.append(DiagnosticFinding(
                source="metrics-analyzer",
                severity=Severity.CRITICAL,
                confidence=0.9,
                summary=f"Memory saturation on {service}: {saturation['percentage']}%",
                evidence=[
                    f"Memory usage peaked at {saturation['percentage']}% (threshold: {saturation['threshold']*100}%)",
                ],
                affected_services=[service],
                suggested_remediation="Increase memory limits or investigate memory leaks. Check for OOMKill events.",
                raw_data={"saturation": saturation, "metric": "memory_usage"},
            ))

    return findings


def _query_cloudwatch_for_service(
    service: str,
    namespace: str,
    time_range: str,
) -> dict[str, list[tuple[float, str]]]:
    """Query CloudWatch Metrics as fallback when Prometheus is unavailable."""
    data: dict[str, list[tuple[float, str]]] = {}

    cw_metrics = [
        ("ContainerInsights", "pod_cpu_utilization", "cpu_usage"),
        ("ContainerInsights", "pod_memory_utilization", "memory_usage"),
    ]

    for cw_namespace, metric_name, key in cw_metrics:
        datapoints = query_cloudwatch_metrics(
            namespace=cw_namespace,
            metric_name=metric_name,
            dimensions=[
                {"Name": "ClusterName", "Value": namespace},
                {"Name": "PodName", "Value": service},
            ],
            time_range=time_range,
        )
        if datapoints:
            # Convert CloudWatch format to Prometheus-like tuples
            values = []
            for dp in datapoints:
                ts = dp["Timestamp"].timestamp() if hasattr(dp["Timestamp"], "timestamp") else float(dp["Timestamp"])
                val = dp.get("Average", dp.get("Maximum", 0))
                values.append((ts, str(val / 100.0)))  # CW gives percentage, normalize to 0-1
            data[key] = values

    return data
