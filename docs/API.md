# KubeTriage — REST API & WebSocket Reference

## Base URL

```
http://localhost:8000
```

---

## REST Endpoints

### 1. POST /api/triage

**Create a new investigation**

```bash
curl -X POST http://localhost:8000/api/triage \
  -H "Content-Type: application/json" \
  -d '{
    "issue": "checkout service is slow and returning 500 errors"
  }'
```

**Request:**
```json
{
  "issue": "checkout service is slow and returning 500 errors"
}
```

**Response (202 Accepted):**
```json
{
  "id": "inv_1704287100123",
  "incident_report": "checkout service is slow and returning 500 errors",
  "status": "INTAKE_DONE",
  "created_at": "2026-03-03T12:00:00Z",
  "updated_at": "2026-03-03T12:00:00Z"
}
```

**Status Codes:**
- `202` Accepted — Investigation created and queued
- `400` Bad Request — Invalid input (issue too short/long)
- `429` Too Many Requests — Rate limited

---

### 2. GET /api/investigation/{id}

**Get investigation details**

```bash
curl http://localhost:8000/api/investigation/inv_1704287100123
```

**Response:**
```json
{
  "id": "inv_1704287100123",
  "incident_report": "checkout service is slow and returning 500 errors",
  "status": "SYNTHESIZED",
  "findings": [
    {
      "id": "f_1",
      "type": "LOG",
      "severity": "CRITICAL",
      "summary": "Payment service OOM detected",
      "confidence": 100,
      "evidence": "Java OutOfMemoryError in transaction processing"
    },
    {
      "id": "f_2",
      "type": "METRIC",
      "severity": "CRITICAL",
      "summary": "Memory usage 700% above baseline",
      "confidence": 95,
      "evidence": "Prometheus: 4GB vs 512MB baseline"
    }
  ],
  "hypotheses": [
    {
      "rank": 1,
      "root_cause": "Payment service OOM due to memory leak",
      "confidence": 87,
      "affected_services": ["checkout", "order-processing", "reporting"]
    }
  ],
  "recommendations": [
    {
      "action": "restart_pods",
      "description": "Restart payment-service pods",
      "risk_level": "low",
      "requires_approval": false
    }
  ],
  "created_at": "2026-03-03T12:00:00Z",
  "completed_at": null
}
```

**Status Values:**
- `INTAKE_DONE` — Incident parsed
- `TRIAGED` — Dependencies queried
- `DIAGNOSED` — Logs/metrics analyzed
- `SYNTHESIZED` — Root causes ranked
- `PENDING_APPROVAL` — Awaiting human approval
- `REMEDIATE_READY` — Ready to execute
- `VERIFYING` — Monitoring fix
- `COMPLETE` — Incident resolved

---

### 3. POST /api/investigation/{id}/approve

**Approve a recommendation**

```bash
curl -X POST http://localhost:8000/api/investigation/inv_1704287100123/approve \
  -H "Content-Type: application/json" \
  -d '{
    "action_id": "action_restart_pods",
    "approved_by": "engineer@example.com"
  }'
```

**Request:**
```json
{
  "action_id": "action_restart_pods",
  "approved_by": "engineer@example.com"
}
```

**Response (200 OK):**
```json
{
  "id": "inv_1704287100123",
  "status": "REMEDIATE_READY",
  "action_executing": true,
  "message": "Restarting payment-service pods..."
}
```

---

### 4. POST /api/investigation/{id}/deny

**Deny a recommendation**

```bash
curl -X POST http://localhost:8000/api/investigation/inv_1704287100123/deny \
  -H "Content-Type: application/json" \
  -d '{
    "reason": "Don'\''t restart now, investigating separately",
    "denied_by": "engineer@example.com"
  }'
```

**Response (200 OK):**
```json
{
  "id": "inv_1704287100123",
  "status": "INVESTIGATE_ALTERNATIVE",
  "message": "Denied. Proceeding with alternative hypotheses."
}
```

---

### 5. GET /api/investigations

**List recent investigations**

```bash
curl 'http://localhost:8000/api/investigations?status=COMPLETE&limit=10'
```

**Query Parameters:**
- `status` — Filter by status (PENDING_APPROVAL, COMPLETE, etc)
- `limit` — Max results (default: 20, max: 100)
- `offset` — Pagination offset (default: 0)

**Response:**
```json
{
  "total": 247,
  "investigations": [
    {
      "id": "inv_1704287100123",
      "incident_report": "checkout service slow",
      "status": "COMPLETE",
      "root_cause": "Payment OOM",
      "confidence": 87,
      "duration_minutes": 7,
      "created_at": "2026-03-03T12:00:00Z",
      "completed_at": "2026-03-03T12:07:00Z"
    }
  ]
}
```

---

### 6. GET /health

**Health check endpoint**

```bash
curl http://localhost:8000/health
```

**Response (200 OK):**
```json
{
  "status": "healthy",
  "components": {
    "graph": "connected",
    "cloudwatch": "connected",
    "prometheus": "connected",
    "slack": "disconnected"
  },
  "uptime_seconds": 3600
}
```

---

## WebSocket Endpoint

### ws://localhost:8000/ws/investigation/{id}

**Real-time investigation updates**

```javascript
// Connect to WebSocket
const ws = new WebSocket('ws://localhost:8000/ws/investigation/inv_1704287100123');

// Listen for messages
ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log(`Phase: ${update.phase}, Status: ${update.status}`);
};

// Close connection
ws.close();
```

**Message Format:**

```json
{
  "investigation_id": "inv_1704287100123",
  "phase": "DIAGNOSE",
  "status": "in_progress",
  "progress_percent": 45,
  "current_task": "Analyzing CloudWatch logs...",
  "timestamp": "2026-03-03T12:00:05.123Z"
}
```

**Phase Progression:**
```
INTAKE → TRIAGE → DIAGNOSE → SYNTHESIZE → REMEDIATE → VERIFY → COMPLETE
```

**Message Examples:**

```json
// Phase started
{
  "phase": "DIAGNOSE",
  "status": "in_progress",
  "message": "Starting parallel diagnostics..."
}

// Findings received
{
  "phase": "DIAGNOSE",
  "status": "in_progress",
  "finding": {
    "type": "LOG",
    "summary": "Payment service OOM detected",
    "confidence": 100
  }
}

// Phase complete
{
  "phase": "DIAGNOSE",
  "status": "complete",
  "findings_count": 12,
  "next_phase": "SYNTHESIZE"
}
```

---

## MCP Tools

KubeTriage exposes 2 tools via the Model Context Protocol for agent integration:

### 1. /query_graph

**Execute Cypher queries on the knowledge graph**

```python
from mcp_client import MCPClient

client = MCPClient("http://localhost:8000")

result = await client.call_tool("query_graph", {
    "query_type": "direct_dependencies",
    "service_id": "checkout"
})

# Returns:
[
  {"node_id": "payment-service", "node_type": "Service"},
  {"node_id": "inventory-db", "node_type": "Service"},
  {"node_id": "cache-layer", "node_type": "Service"}
]
```

**Supported Query Types:**
- `direct_dependencies` — Services this depends on
- `transitive_dependencies` — All reachable services (up to 4 hops)
- `blast_radius` — Services that depend on this
- `upstream_risk` — What could affect this service
- `critical_path` — Services on the critical path
- `pod_lifecycle` — Pod restart history

---

### 2. /blast_radius

**Calculate impact radius and risk assessment**

```python
result = await client.call_tool("blast_radius", {
    "service_id": "payment-service",
    "max_depth": 4
})

# Returns:
{
  "service_id": "payment-service",
  "affected_count": 3,
  "affected_services": [
    "checkout",
    "order-processing",
    "reporting-dashboard"
  ],
  "risk_level": "high",
  "impact_depth": [1, 1, 1]  // number of hops for each
}
```

---

## Error Responses

All error responses follow this format:

```json
{
  "error": "error_code",
  "message": "Human-readable error message",
  "details": {
    "field": "issue",
    "reason": "too short (min 10 chars)"
  }
}
```

**Common Error Codes:**

| Code | Status | Meaning |
|------|--------|---------|
| `invalid_input` | 400 | Issue description invalid |
| `service_not_found` | 404 | Investigation not found |
| `invalid_status_transition` | 400 | Can't transition to that status |
| `insufficient_confidence` | 400 | Confidence too low for approval |
| `rate_limit_exceeded` | 429 | Too many requests |
| `graph_unavailable` | 503 | Graph DB offline |
| `cloudwatch_unavailable` | 503 | CloudWatch API down |
| `internal_error` | 500 | Unexpected error |

---

## Rate Limiting

KubeTriage enforces rate limits per IP address:

```
- 100 requests/minute for /api/triage
- 1000 requests/minute for /api/investigation
- 10 requests/minute for /api/investigation/{id}/approve
```

When rate limited, receive:

```json
{
  "error": "rate_limit_exceeded",
  "message": "100 requests per minute exceeded",
  "retry_after_seconds": 12
}
```

---

## Authentication (Future)

Currently no authentication required. Future versions will support:

```bash
# Bearer token
curl -H "Authorization: Bearer sk_live_..." \
  http://localhost:8000/api/investigations
```

---

## Example Workflows

### Workflow 1: Auto-Approve Investigation (High Confidence)

```bash
# 1. Create investigation
INVESTIGATION=$(curl -X POST http://localhost:8000/api/triage \
  -d '{"issue": "checkout slow"}' | jq -r .id)

# 2. Wait for completion (poll or WebSocket)
sleep 2

# 3. Get details
curl http://localhost:8000/api/investigation/$INVESTIGATION

# 4. If confidence > 95%, automatically approved and executing
```

### Workflow 2: Manual Approval

```bash
# 1. Create investigation
INVESTIGATION=$(curl -X POST http://localhost:8000/api/triage \
  -d '{"issue": "payment service down"}' | jq -r .id)

# 2. Subscribe to WebSocket for updates
# (frontend shows findings as they arrive)

# 3. Get final recommendation
RESPONSE=$(curl http://localhost:8000/api/investigation/$INVESTIGATION)
CONFIDENCE=$(echo $RESPONSE | jq .hypotheses[0].confidence)

# 4. If confidence 70-95%, wait for human approval
if [ $CONFIDENCE -lt 95 ]; then
  echo "Confidence: $CONFIDENCE% - awaiting approval"
fi

# 5. Approve (via UI or API)
curl -X POST http://localhost:8000/api/investigation/$INVESTIGATION/approve \
  -d '{"action_id": "action_restart_pods", "approved_by": "engineer@example.com"}'

# 6. Monitor verification
sleep 10
curl http://localhost:8000/api/investigation/$INVESTIGATION | jq .status
```

### Workflow 3: Programmatic Integration with Slack

```bash
# KubeTriage automatically posts to Slack when:
# 1. Investigation created
# 2. Findings discovered
# 3. Requires approval (confidence 70-95%)
# 4. Action executed
# 5. Incident resolved

# Example Slack message:
# ---
# 🚨 INCIDENT DETECTED: checkout service slow
#
# 🔍 INVESTIGATING...
# ├─ CloudWatch: Analyzing logs
# ├─ Prometheus: Querying metrics
# └─ Graph: Finding dependencies
#
# [View Investigation]
```

---

## Pagination

List endpoints support cursor-based pagination:

```bash
# First page
curl 'http://localhost:8000/api/investigations?limit=10'

# Next page
curl 'http://localhost:8000/api/investigations?limit=10&offset=10'

# Response
{
  "total": 247,
  "limit": 10,
  "offset": 0,
  "investigations": [...]
}
```

---

## Performance Guidelines

### Request Latencies (P99)

| Endpoint | Latency |
|----------|---------|
| POST /api/triage | 50ms |
| GET /api/investigation/{id} (during INTAKE) | 20ms |
| GET /api/investigation/{id} (during DIAGNOSE) | 500ms |
| WebSocket message | <10ms |
| /health | <5ms |

### Investigation Times

| Status | Time |
|--------|------|
| INTAKE | 50ms |
| TRIAGE | 100ms |
| DIAGNOSE | 800ms |
| SYNTHESIZE | 150ms |
| APPROVE | immediate |
| REMEDIATE | 5-30s (depends on action) |
| VERIFY | 5s+ (depends on metric stabilization) |
| **Total** | **~2-10s** |

