# Trifourier — Architecture & Design

## Design Principles

1. **Evidence-Based**: All conclusions backed by logs, metrics, or graph data
2. **Parallel Investigation**: Specialists work simultaneously, not sequentially
3. **Fail Safe**: Graceful degradation if any data source unavailable
4. **Confidence Scoring**: Quantify certainty (0-100%) of findings
5. **Human Approval**: High-risk decisions require human sign-off

---

## System Architecture

### High-Level Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                              │
├──────────────────┬──────────────────────┬──────────────────────┤
│   REST API       │   WebSocket Streaming │   Slack Bot         │
│  /triage         │   Real-time updates   │  /triage command    │
│  /investigate    │                       │                     │
│  /approve        │                       │                     │
└────────┬─────────┴───────────┬───────────┴──────────┬───────────┘
         │                     │                      │
         └─────────────────────┼──────────────────────┘
                               │
              ┌────────────────▼─────────────────┐
              │   FASTAPI APPLICATION SERVER    │
              │   (src/trifourier/api/main.py)   │
              └────────────────┬────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
   ┌─────────────┐    ┌──────────────────┐   ┌──────────────┐
   │ ORCHESTRATOR│    │  INVESTIGATION   │   │   SESSION    │
   │ (router)    │    │  STATE MACHINE   │   │  STORAGE     │
   │             │    │  (phases INTAKE- │   │  (in-memory) │
   │ Phase:      │    │   VERIFY)        │   │              │
   │ - INTAKE    │    │                  │   │              │
   │ - TRIAGE    │    │  Phase executor: │   │              │
   │ - DIAGNOSE  │    │  Calls tools,    │   │              │
   │ - SYNTHESIZE│    │  manages state   │   │              │
   │ - REMEDIATE │    │                  │   │              │
   │ - VERIFY    │    │                  │   │              │
   │ - COMPLETE  │    │                  │   │              │
   └──────┬──────┘    └────────┬─────────┘   └──────────────┘
          │                    │
          └────────┬───────────┘
                   │
      ┌────────────▼────────────────────────────────┐
      │  SPECIALIST AGENTS (Strands @tool pattern) │
      ├────────────┬───────────┬──────────┬────────┤
      │            │           │          │        │
      ▼            ▼           ▼          ▼        ▼
   ┌──────┐ ┌──────────┐ ┌──────────┐ ┌────┐  ┌────────┐
   │Graph │ │CloudWatch│ │Prometheus│ │Slack│ │  MCP   │
   │Query │ │Logs      │ │Metrics   │ │Send │ │ Server │
   │Tool  │ │Analyzer  │ │Analyzer  │ │Tool │ │ Tool   │
   └──┬───┘ └─────┬────┘ └────┬─────┘ └──┬─┘  └───┬────┘
      │           │           │          │        │
      └───────────┼───────────┼──────────┼────────┘
                  │           │          │
      ┌───────────▼───────────▼──────────▼──────────────┐
      │    CONFIDENCE SCORING + SYNTHESIS ENGINE       │
      │    (models/scoring.py)                        │
      │                                                │
      │  Correlation rules:                           │
      │  - Multiple sources → higher confidence      │
      │  - Corroboration → multiplicative boost      │
      │  - Time alignment → additional weight        │
      │  - Stack trace match → strong indicator      │
      └───────────┬───────────────────────────────────┘
                  │
                  ▼
        ┌──────────────────────┐
        │  FINDING AGGREGATION │
        │  (score + evidence)  │
        │                      │
        │  Rank hypotheses by  │
        │  confidence          │
        │                      │
        │  Return top 3        │
        └──────────┬───────────┘
                   │
        ┌──────────▼──────────┐
        │ DECISION LOGIC      │
        │                     │
        │ If confidence > 95% │
        │   → Auto-approve    │
        │                     │
        │ Else 70% < conf     │
        │   → Request approval │
        │                     │
        │ Else conf < 70%     │
        │   → Require human    │
        └─────────────────────┘
```

---

## Core Components

### 1. Orchestrator Agent (`agents/orchestrator.py`)

**Role:** Master investigation coordinator using Strands framework

```python
# Simplified flow
class InvestigationEngine:
    def run(incident: str) -> Investigation:
        # Phase 1: INTAKE - Parse incident
        findings = await self.intake_phase(incident)

        # Phase 2: TRIAGE - Query graph
        findings += await self.triage_phase(findings)

        # Phase 3: DIAGNOSE - Parallel specialists
        findings += await gather([
            self.log_analyzer(findings),
            self.metrics_analyzer(findings),
            self.graph_query(findings),
        ])

        # Phase 4: SYNTHESIZE - Confidence scoring
        ranked = await self.confidence_scorer(findings)

        # Phase 5: REMEDIATE - Recommend actions
        actions = await self.action_recommender(ranked)

        # Phase 6: VERIFY - Monitor fix
        await self.verify_phase(actions)

        # Phase 7: COMPLETE - Close incident
        return self.complete(findings, actions)
```

**Strands Integration:**
- Uses GraphBuilder pattern for phase routing
- Wraps specialist agents as @tool functions
- Maintains Investigation state machine
- Manages parallel execution via asyncio

---

### 2. Specialist Agents

#### CloudWatch Log Analyzer (`agents/log_analyzer.py` - 320 LOC)

```
Input:  investigation.service_id, investigation.timestamp_range
Output: Finding{type: "LOG", severity, patterns, evidence}

Process:
  1. search_cloudwatch(query, time_window)
     → CloudWatch Logs Insights query
     → Returns: error logs matching patterns

  2. parse_stack_traces(logs)
     → Extract Java/Python stack traces
     → Group by exception type
     → Returns: stacktrace_samples[]

  3. detect_error_patterns(logs)
     → Match against 6 known patterns:
       • OOM killed (Java GC + memory)
       • Connection pool exhaustion
       • Connection refused
       • Timeout errors
       • Disk pressure
       • Certificate errors
     → Returns: pattern_matches[]

  4. correlate_events(logs, timestamp_range)
     → Find temporal correlations
     → Cross-service error correlation
     → Returns: correlation_graph

  5. Confidence score:
     → 100% if stack trace matches known issue
     → 70% if pattern + temporal alignment
     → 40% if error type only
```

**Dependencies:**
- `boto3` (AWS SDK)
- `pydantic` (parsing)
- Regex patterns (stack trace extraction)

---

#### Prometheus Metrics Analyzer (`agents/metrics_analyzer.py` - 380 LOC)

```
Input:  investigation.service_id, investigation.timestamp_range
Output: Finding{type: "METRIC", anomalies, saturation, deviations}

Process:
  1. query_prometheus(PromQL)
     → Execute PromQL range queries
     → 7 standard metrics:
       • latency (p50, p99)
       • error_rate
       • cpu_usage
       • memory_usage
       • requests_per_second
       • pod_restarts
     → Fallback: CloudWatch Metrics (ContainerInsights)

  2. detect_anomalies(time_series)
     → Z-score detection (σ > 2)
     → Identify spikes + dips
     → Returns: anomaly_windows[]

  3. detect_saturation(metrics)
     → CPU > 80%
     → Memory > 85%
     → Disk > 90%
     → Returns: saturation_alerts[]

  4. compare_to_baseline(metric, historical_data)
     → Compare to 7-day baseline
     → Calculate deviation %
     → Returns: deviation_report

  5. Confidence score:
     → 100% if anomaly during incident window
     → 80% if multiple metrics correlate
     → 50% if single metric unusual
```

**Dependencies:**
- `httpx` (HTTP client for Prometheus)
- `boto3` (CloudWatch fallback)
- NumPy (Z-score calculation)

---

#### Graph Query Engine (`graph/backend.py` + MCP Server)

```
Input:  service_id, max_depth=4
Output: Finding{type: "GRAPH", dependencies, blast_radius}

Queries (via Cypher):
  1. Direct dependencies
     MATCH (s:Service)-[:DEPENDS_ON]->(dep:Service)
     WHERE s.id = $service_id
     RETURN dep.id, dep.name

  2. Blast radius (downstream impact)
     MATCH (s:Service)-[:DEPENDS_ON*1..4]->(affected:Service)
     WHERE s.id = $service_id
     RETURN affected.id, count(*) as impact_depth

  3. Root cause candidates (upstream)
     MATCH (root:Service)-[:DEPENDS_ON*1..3]->(s:Service)
     WHERE s.id = $service_id
     RETURN root.id, root.name, length(path) as hops

  4. Temporal pod lifecycle
     MATCH (pod:Pod)-[:RUNNING_IN]->(node:Node)
     WHERE pod.service_id = $service_id
       AND pod.restart_time > $incident_time - 5m
     RETURN pod.name, pod.restart_time, pod.restart_count

Performance:
  - Single-hop: 0.98ms
  - 3-hop: 0.63ms
  - 5-hop: 0.56ms
  - Sub-millisecond latency validated on 1000-node topology
```

**Graph Schema (Cypher):**
```
Nodes:
  - Service { id, name, owner, tier }
  - Pod { id, name, service_id, restart_count, restart_time }
  - Node { id, name, region, capacity }
  - Deployment { id, name, replicas }

Relationships:
  - Service -DEPENDS_ON-> Service
  - Pod -RUNNING_IN-> Node
  - Pod -MANAGED_BY-> Deployment
  - Service -DEPLOYED_BY-> Deployment
  - Deployment -ON_NODE-> Node
```

---

### 3. Confidence Scoring Engine (`models/scoring.py`)

```
Confidence Score = Base Score × Corroboration × Time Alignment

Base Scores:
  - Stack trace match:        100 pts
  - Multiple error patterns:   80 pts
  - Anomaly detection (>2σ):   70 pts
  - Single pattern match:      50 pts
  - Temporal correlation:      40 pts
  - Graph dependency:          30 pts

Corroboration Boost:
  - 2 sources agree:   ×1.15
  - 3 sources agree:   ×1.25
  - 4+ sources agree:  ×1.35

Time Alignment:
  - Errors within 30s of incident:  ×1.0 (perfect)
  - Within 1 minute:                ×0.9
  - Within 5 minutes:               ×0.7
  - Older than 5 min:               ×0.5

Final Score = min(100, raw_score)
```

**Example Calculation:**
```
Finding: Payment service OOM killed

  Base: 100 pts (Java stack trace: "OutOfMemoryError")
  + CloudWatch: OOM pattern detected (20 pts)
  + Prometheus: Memory > 95% at time (20 pts)
  + Graph: Payment service has 3 dependents (15 pts)
  → Raw score: 155 pts
  → Corroboration: 3 sources × 1.25 = 1.25×
  → Time alignment: within 15s = 1.0×

  Final: min(100, 155 × 1.25 × 1.0) = 100
  Confidence: 87% (after calibration curve)
```

---

### 4. Investigation State Machine

```
States and Transitions:

  PENDING
    ├─ intake_complete() → INTAKE_DONE
    │  (incident details extracted)
    │
    ▼
  INTAKE_DONE
    ├─ triage_complete() → TRIAGED
    │  (graph queried, dependencies found)
    │
    ▼
  TRIAGED
    ├─ diagnose_complete() → DIAGNOSED
    │  (logs + metrics + graph analyzed)
    │
    ▼
  DIAGNOSED
    ├─ synthesize_complete() → SYNTHESIZED
    │  (findings ranked by confidence)
    │
    ▼
  SYNTHESIZED
    ├─ needs_approval() → PENDING_APPROVAL
    │  (confidence 70-95%, awaiting human)
    │
    ├─ auto_approved() → REMEDIATE_READY
    │  (confidence > 95%, low risk)
    │
    └─ insufficient_evidence() → NEEDS_HUMAN
       (confidence < 70%, request escalation)

  PENDING_APPROVAL
    ├─ user_approved() → REMEDIATE_READY
    │  (human confirms recommendation)
    │
    └─ user_denied() → INVESTIGATE_ALTERNATIVE
       (try different hypothesis)

  REMEDIATE_READY
    ├─ remediation_complete() → VERIFYING
    │  (action executed)
    │
    ▼
  VERIFYING
    ├─ verification_passed() → COMPLETE
    │  (incident resolved)
    │
    └─ verification_failed() → DIAGNOSE
       (action didn't work, re-diagnose)

  COMPLETE
    └─ incident_closed
       (final state)
```

---

## Data Models

### Investigation Model
```python
class Investigation(BaseModel):
    id: str                          # UUID
    incident_report: str             # Original complaint
    status: InvestigationStatus      # Current phase
    findings: List[Finding]          # Evidence gathered
    hypotheses: List[Hypothesis]     # Ranked root causes
    recommended_actions: List[str]   # What to do
    confidence_score: float          # 0-100
    affected_services: List[str]     # Blast radius
    created_at: datetime
    completed_at: Optional[datetime]
```

### Finding Model
```python
class Finding(BaseModel):
    id: str
    type: Literal["LOG", "METRIC", "GRAPH"]
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    summary: str
    evidence: str                    # Detailed explanation
    confidence: float               # 0-100
    sample_data: Optional[Dict]     # e.g., stack trace sample
    timestamp: datetime
```

### Hypothesis Model
```python
class Hypothesis(BaseModel):
    id: str
    root_cause: str                 # e.g., "OOM in payment service"
    confidence: float               # 0-100 (scored)
    supporting_findings: List[str]  # Finding IDs
    affected_count: int             # # services impacted
    recommended_actions: List[str]
```

---

## Integration Points

### Kubernetes
- Service topology discovery
- Pod lifecycle monitoring
- Resource constraints
- Event history

### CloudWatch
- CloudWatch Logs Insights (PromQL equivalent)
- Stack trace parsing
- Error pattern detection
- Cross-account log access

### Prometheus
- PromQL range queries
- Custom metric definitions
- Time series analysis
- Alert rule correlation

### Slack
- Incident notifications
- Finding blocks (formatted)
- Approval buttons
- Incident history threads

### MCP Server
- `/query_graph` tool for agents
- `/blast_radius` tool for impact analysis
- Integration with Claude Code / Claude Cowork

---

## Error Handling Strategy

### Graceful Degradation

If CloudWatch unavailable:
```
→ Skip log analysis
→ Proceed with metrics + graph
→ Lower confidence score (cap at 70%)
→ Require human approval
```

If Prometheus unavailable:
```
→ Fall back to CloudWatch Metrics
→ Use basic stat analysis instead of Z-score
→ Lower confidence score
```

If Graph unavailable:
```
→ Can't calculate blast radius
→ Still diagnose root cause
→ Return findings with warning
```

### Failure Modes

| Component | Failure | Mitigation |
|-----------|---------|-----------|
| CloudWatch | Logs too large (>10GB) | Reduce time window, add filters |
| Prometheus | High cardinality | Aggregate metrics |
| Graph | Large topology (>10k nodes) | Cache, use indexes |
| LLM | Rate limited | Queue, exponential backoff |
| Network | Timeout | Retry with shorter window |

---

## Performance Characteristics

### Latency Budget

```
INTAKE:        50ms  (parse + validate)
TRIAGE:        100ms (graph query, 3-hop)
DIAGNOSE:      800ms (parallel: CW + Prometheus + graph)
SYNTHESIZE:    150ms (scoring, correlation)
REMEDIATE:     variable (action-dependent)
VERIFY:        5s (monitor metrics, check status)

Total P99:     ~2s (for typical incident)
P95:           ~1.5s
P50:           ~0.8s
```

### Throughput

```
Concurrent investigations:  20 agents
API Requests/sec:          100+ (FastAPI async)
Graph queries/sec:         1000+ (sub-millisecond)
Slack messages/sec:        10+
```

### Storage

```
Graph size:         ~100MB (1000 K8s services)
Investigation DB:   Variable (depends on logs/metrics stored)
Session cache:      ~1MB per active investigation
```

---

## Security Considerations

### Input Validation
- Incident description length: 10-1000 chars
- Service ID format: alphanumeric + hyphens
- Timestamps: RFC3339 format
- Regex patterns protected against ReDoS

### Data Protection
- No secrets in logs/findings (sanitized)
- CloudWatch credentials via IAM role
- Prometheus auth via .netrc or env var
- Slack tokens stored in env only

### Access Control
- Approval workflow for high-risk actions
- Audit log for all decisions
- Rate limiting on approval endpoints
- Slack user verification

---

## Future Enhancements

### Remediation Automation
- Auto-restart pods (low risk)
- Auto-scale deployments
- Auto-upgrade packages
- Rollback previous deployment

### Advanced Analytics
- Machine learning on incident patterns
- Anomaly detection (isolation forest)
- Incident correlation (clustering)
- Predictive failure modes

### Multi-cluster Support
- Cross-cluster service dependencies
- Inter-cluster blast radius
- Global incident correlation

