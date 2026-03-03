# KubeTriage — Investigation Workflow & Phases

## Overview: The 7-Phase Investigation Process

When an incident is reported, KubeTriage runs through 7 structured phases to investigate, diagnose, and recommend remediation:

```
┌──────────────┐
│ 1. INTAKE    │  Extract incident details
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 2. TRIAGE    │  Query dependencies, calculate blast radius
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 3. DIAGNOSE  │  Parallel: logs + metrics + graph analysis
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 4. SYNTHESIZE│  Score confidence, rank root causes
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 5. REMEDIATE │  Recommend or execute actions
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 6. VERIFY    │  Monitor metrics, confirm fix
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 7. COMPLETE  │  Close incident, log learnings
└──────────────┘
```

---

## Phase 1: INTAKE (50ms)

**Goal:** Extract and validate incident details

**Process:**
1. Parse incident description
2. Extract mentioned service names
3. Determine incident severity (high/medium/low)
4. Set initial timestamp
5. Initialize investigation state

**Input:**
```json
{
  "issue": "checkout service is slow and returning 500 errors"
}
```

**Output (Investigation):**
```json
{
  "id": "inv_12345",
  "incident_report": "checkout service is slow and returning 500 errors",
  "status": "INTAKE_DONE",
  "primary_service": "checkout",
  "severity": "high",
  "created_at": "2026-03-03T12:00:00Z",
  "findings": []
}
```

**Example Code:**
```python
async def intake_phase(incident: str) -> Investigation:
    # 1. Parse using LLM
    parsed = await llm.extract_incident_details(incident)

    # 2. Validate service names exist
    services = await k8s.list_services()
    primary = find_best_match(parsed.service, services)

    # 3. Create investigation
    inv = Investigation(
        incident_report=incident,
        primary_service=primary.id,
        severity=parsed.severity,
        status="INTAKE_DONE"
    )

    return inv
```

**Success Criteria:**
- ✅ Service name identified
- ✅ Severity determined
- ✅ Investigation object created

---

## Phase 2: TRIAGE (100ms)

**Goal:** Query knowledge graph for dependencies and blast radius

**Process:**
1. Query service dependencies (direct)
2. Query transitive dependencies (up to 4 hops)
3. Calculate blast radius (which services depend on primary?)
4. Identify critical path services
5. Determine investigation scope

**Cypher Queries:**

**Query 1: Direct Dependencies**
```cypher
MATCH (primary:Service {id: $service_id})
       -[:DEPENDS_ON]->(dep:Service)
RETURN dep.id, dep.name, dep.owner, dep.tier
LIMIT 100
```

**Query 2: Upstream (What could affect this service?)**
```cypher
MATCH (root:Service)
       -[:DEPENDS_ON*1..4]->(primary:Service {id: $service_id})
RETURN root.id, root.name, length(path) as hops
ORDER BY hops ASC
LIMIT 20
```

**Query 3: Downstream (Blast Radius)**
```cypher
MATCH (primary:Service {id: $service_id})
       -[:DEPENDS_ON*1..3]->(affected:Service)
RETURN affected.id, affected.name,
       length(path) as impact_depth,
       count(*) as path_count
ORDER BY impact_depth ASC
```

**Output:**
```json
{
  "primary_service": "checkout",
  "direct_dependencies": [
    "payment-service",
    "inventory-db",
    "cache-layer"
  ],
  "upstream_risk": [
    "api-gateway",
    "auth-service"
  ],
  "blast_radius": {
    "affected_count": 3,
    "services": [
      "order-processing",
      "reporting-dashboard",
      "customer-api"
    ]
  },
  "findings": [
    {
      "type": "GRAPH",
      "summary": "Checkout depends on payment-service, inventory-db, cache-layer",
      "confidence": 100
    }
  ]
}
```

**Performance:**
- Direct query: 0.98ms
- 3-hop query: 0.63ms
- Total phase: <100ms

---

## Phase 3: DIAGNOSE (800ms - Parallel Execution)

**Goal:** Analyze logs, metrics, and graph to identify root causes

This phase runs 3 specialist agents in parallel:

### Specialist 1: CloudWatch Log Analyzer

**Timeline:**
```
T=0ms    → Start query
T=150ms  → Fetch logs (concurrent window: -30m to +5m from incident)
T=200ms  → Parse stack traces
T=250ms  → Detect error patterns
T=300ms  → Cross-service correlation
T=800ms  → Complete
```

**Sample Finding:**
```json
{
  "type": "LOG",
  "specialist": "cloudwatch",
  "severity": "CRITICAL",
  "summary": "Payment service: OutOfMemoryError in JVM",
  "evidence": {
    "stack_trace": [
      "java.lang.OutOfMemoryError: Java heap space",
      "  at com.payment.ledger.balance() [payment-service.jar]",
      "  at com.payment.transaction.process() [payment-service.jar]",
      "  at java.util.concurrent.ThreadPoolExecutor$Worker.run()"
    ],
    "error_count": 145,
    "first_occurrence": "2026-03-03T11:55:00Z",
    "pattern_matched": "OOM_KILLER"
  },
  "confidence": 100
}
```

### Specialist 2: Prometheus Metrics Analyzer

**Timeline:**
```
T=0ms    → Start PromQL queries
T=100ms  → Fetch metric timeseries (7 metrics × 5min window)
T=150ms  → Calculate z-scores
T=200ms  → Detect anomalies
T=250ms  → Compare to baseline
T=800ms  → Complete
```

**Metrics Queried:**
```promql
# Latency (p50, p99)
histogram_quantile(0.5, rate(http_request_duration_ms[5m]))
histogram_quantile(0.99, rate(http_request_duration_ms[5m]))

# Error rate
rate(http_errors_total[5m])

# Resource usage
rate(process_resident_memory_bytes[5m])
rate(process_cpu_seconds_total[5m])

# Pod restarts
increase(kube_pod_container_status_restarts_total[1h])
```

**Sample Finding:**
```json
{
  "type": "METRIC",
  "specialist": "prometheus",
  "severity": "CRITICAL",
  "summary": "Payment service memory spike + CPU maxout",
  "evidence": {
    "memory_usage": {
      "normal_baseline": "512MB",
      "current": "4096MB",
      "deviation": "+700%",
      "anomaly_score": 4.2  // z-score > 2
    },
    "cpu_usage": {
      "normal_baseline": "20%",
      "current": "99.8%",
      "saturation": true
    },
    "pod_restarts": {
      "count": 12,
      "last_restart": "2026-03-03T11:57:00Z",
      "reason": "OOMKilled"
    }
  },
  "confidence": 95
}
```

### Specialist 3: Graph Correlation Analysis

**Timeline:**
```
T=0ms    → Start correlation queries
T=50ms   → Query pod lifecycle (restarts, timing)
T=100ms  → Cross-service calls (traffic flow)
T=150ms  → Temporal alignment (did events correlate?)
T=800ms  → Complete
```

**Sample Finding:**
```json
{
  "type": "GRAPH",
  "specialist": "graph_correlation",
  "severity": "HIGH",
  "summary": "Payment service OOM cascades to checkout, orders, reporting",
  "evidence": {
    "pod_restarts": [
      {
        "pod": "payment-service-xyz",
        "restart_time": "2026-03-03T11:57:00Z",
        "restart_reason": "OOMKilled",
        "restart_count": 12
      }
    ],
    "cascade_analysis": {
      "upstream": ["api-gateway"],
      "downstream": ["checkout", "order-processing", "reporting"],
      "critical_path_affected": 3
    }
  },
  "confidence": 85
}
```

**Parallelization:**
```
TIME ────────────────────────────────────────→

│ CloudWatch Analyzer    (────────────────────────)
│ Prometheus Analyzer    (────────────────────────)
│ Graph Analyzer         (────────────────────────)
└→ All complete at T=800ms, proceed to SYNTHESIZE
```

---

## Phase 4: SYNTHESIZE (150ms)

**Goal:** Score confidence and rank root cause hypotheses

**Process:**
1. Collect all findings from specialists
2. Apply confidence scoring algorithm
3. Rank by confidence (descending)
4. Identify supporting/contradicting evidence
5. Generate top 3 hypotheses

**Scoring Logic:**

```
Hypothesis 1: "Payment service OOM killed due to memory leak"

Finding 1 (CloudWatch):  Stack trace match → 100 pts
Finding 2 (Prometheus):  Memory spike + OOM restart → 80 pts
Finding 3 (Graph):       Pod restart pattern + cascade → 70 pts

Raw score: 100 + 80 + 70 = 250 pts
Corroboration: 3 sources agree × 1.25 = 312.5 pts
Time alignment: All within 2 min of incident × 1.0 = 312.5 pts

Clamped to 100: Final Confidence = 87%
```

**Output:**
```json
{
  "status": "SYNTHESIZED",
  "hypotheses": [
    {
      "rank": 1,
      "root_cause": "Payment service OOM killed due to memory leak",
      "confidence": 87,
      "supporting_findings": [
        "Stack trace: OutOfMemoryError in payment-service",
        "Prometheus: Memory 700% above baseline",
        "Graph: Pod restart cascade at incident time"
      ],
      "affected_services": {
        "primary": "payment-service",
        "downstream": ["checkout", "order-processing", "reporting"],
        "blast_radius_count": 3
      }
    },
    {
      "rank": 2,
      "root_cause": "Database connection pool exhaustion",
      "confidence": 42,
      "supporting_findings": [
        "Prometheus: Connection wait time increased"
      ]
    },
    {
      "rank": 3,
      "root_cause": "Network partition or DNS failure",
      "confidence": 28,
      "supporting_findings": []
    }
  ]
}
```

**Confidence Interpretation:**
```
> 95%  → Auto-approved, low risk (restart pod)
70-95% → Requires human approval (higher impact)
< 70%  → Requires human investigation (manual mode)
```

---

## Phase 5: REMEDIATE (Variable)

**Goal:** Recommend or execute remediation actions

**Decision Tree:**
```
if confidence > 95%:
    → Recommend low-risk actions
    → Auto-execute with monitoring
    Examples:
      - Restart affected pods
      - Clear cache
      - Scale deployment

elif confidence 70-95%:
    → Recommend actions
    → Send to Slack for approval
    → Wait for human decision
    Examples:
      - Trigger PagerDuty alert
      - Scale deployment
      - Apply hotfix

else (confidence < 70%:
    → Escalate to human
    → Provide evidence for investigation
    → No auto-actions
```

**Output:**
```json
{
  "status": "PENDING_APPROVAL",
  "recommended_actions": [
    {
      "action": "restart_pods",
      "description": "Restart payment-service pods to recover from OOM",
      "risk_level": "low",
      "requires_approval": false
    },
    {
      "action": "scale_deployment",
      "description": "Increase payment-service replicas from 3 to 5",
      "risk_level": "medium",
      "requires_approval": true,
      "expected_impact": "Spreads load, prevents future OOM"
    }
  ],
  "approval_required": true,
  "confidence_threshold_met": false
}
```

---

## Phase 6: VERIFY (5s + ongoing)

**Goal:** Monitor metrics and confirm that the fix worked

**Process:**
1. Execute recommended actions
2. Monitor key metrics (latency, error rate, CPU, memory)
3. Check for pod stability
4. Verify root cause is resolved
5. Set verification criteria

**Verification Criteria:**

```python
def verify_incident_resolution():
    """After executing remediation, check if incident is fixed"""

    # Check metric thresholds
    latency_p99 = query_prometheus("p99_latency")
    error_rate = query_prometheus("error_rate")
    pod_restarts = query_prometheus("pod_restarts")

    success = (
        latency_p99 < baseline_latency * 1.1  # Within 10% of baseline
        and error_rate < 0.01                  # < 1% error rate
        and pod_restarts == 0                  # No new restarts
        and duration > 5_minutes                # Sustained for 5+ min
    )

    return success
```

**Monitoring Timeline:**
```
T=0s     ┐ Remediation actions execute
         │
T=5s     │ Initial metrics check (immediate impact)
         │
T=30s    │ Short-term verification
         │
T=300s   │ Long-term verification (5 minutes)
         │
T=600s   ✓ Incident resolved (if all checks pass)
         │
         └─→ Proceed to COMPLETE
```

**Sample Verification Result:**
```json
{
  "status": "VERIFYING",
  "verification_results": {
    "latency_check": {
      "baseline_p99": 150,
      "current_p99": 160,
      "target_threshold": 165,
      "status": "PASS"
    },
    "error_rate_check": {
      "baseline": 0.001,
      "current": 0.0005,
      "target_threshold": 0.01,
      "status": "PASS"
    },
    "pod_restart_check": {
      "restarts_in_verification_window": 0,
      "threshold": 0,
      "status": "PASS"
    }
  },
  "verification_duration": 300,
  "verdict": "RESOLVED"
}
```

---

## Phase 7: COMPLETE (Close)

**Goal:** Close incident and log learnings

**Process:**
1. Update investigation status to COMPLETE
2. Generate incident summary
3. Store for future ML training
4. Send final Slack notification
5. Archive findings

**Output:**
```json
{
  "id": "inv_12345",
  "status": "COMPLETE",
  "incident_summary": {
    "title": "Payment service OOM (resolved)",
    "duration": "7 minutes",
    "root_cause": "Memory leak in transaction processing",
    "confidence": 87,
    "blast_radius_count": 3,
    "remediation": "Restarted 3 payment-service pods",
    "verification": "PASSED - all metrics normal"
  },
  "timeline": {
    "reported": "2026-03-03T11:55:00Z",
    "intake_complete": "2026-03-03T11:55:05Z",
    "diagnosis_complete": "2026-03-03T11:55:10Z",
    "action_executed": "2026-03-03T11:55:15Z",
    "resolved": "2026-03-03T12:02:00Z"
  },
  "learnings": [
    "Memory spike correlates with high transaction volume",
    "Heap dump shows retained objects in ledger.balance()",
    "Recommend: Add memory limit + GC tuning"
  ]
}
```

**Slack Final Notification:**
```
✅ INCIDENT RESOLVED

Root Cause: Payment service OOM (87% confidence)
Duration: 7 minutes
Action: Restarted 3 pods
Status: Verified stable, metrics normal

Evidence Summary:
  🔴 Stack trace: OutOfMemoryError (payment-service)
  📊 Memory: 4GB (vs 512MB baseline) +700%
  📈 Pod restarts: 12 in 2 minutes
  🔗 Impact: checkout, orders, reporting services

Learnings: Memory leak in transaction processing.
Recommend heap dump analysis + GC tuning.

---

Incident ID: inv_12345
Closed by: KubeTriage (confidence: 87%)
```

---

## Real-Time Updates with WebSocket

Clients can subscribe to real-time investigation progress:

```bash
# Connect to WebSocket
ws://localhost:8000/ws/investigation/inv_12345

# Receives updates as phases complete:
{
  "investigation_id": "inv_12345",
  "phase": "INTAKE",
  "status": "in_progress",
  "timestamp": "2026-03-03T11:55:05.123Z"
}

{
  "investigation_id": "inv_12345",
  "phase": "INTAKE",
  "status": "complete",
  "findings": [
    {"summary": "Service: checkout", "confidence": 100}
  ],
  "timestamp": "2026-03-03T11:55:05.456Z"
}

{
  "investigation_id": "inv_12345",
  "phase": "TRIAGE",
  "status": "in_progress",
  "timestamp": "2026-03-03T11:55:05.500Z"
}

... (continues through DIAGNOSE, SYNTHESIZE, etc)
```

---

## Timeline Example: Real Incident Investigation

```
11:55:00  🚨 Incident: "checkout slow + errors"
11:55:05  📋 INTAKE    Severity=HIGH, primary=checkout
11:55:10  🔎 TRIAGE    Dependencies found: payment, inventory, cache
11:55:15  🔬 DIAGNOSE  (parallel)
          ├─ CloudWatch: OOM stack trace
          ├─ Prometheus: Memory +700%, CPU 99%
          └─ Graph: Pod restart cascade
11:55:20  📊 SYNTHESIZE Root cause: Payment OOM (87% conf)
11:55:22  🎯 REMEDIATE Recommendation: Restart pods (low-risk)
          ✓ Auto-approved + executed
11:55:30  ✅ VERIFY    Metrics normal, cascades resolved
12:02:00  ✓ COMPLETE   Incident closed, 7min investigation

Total time: 7 minutes (vs 30-45 min manual investigation)
Accuracy: 87% confidence (vs 60% manual guessing)
```

