# Documentation Index

## Quick Links

| Document | Purpose | Audience |
|----------|---------|----------|
| **[../README.md](../README.md)** | Project overview with quick start | Everyone |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Technical design & components | Developers |
| **[WORKFLOW.md](WORKFLOW.md)** | Investigation phases walkthrough | Developers, DevOps |
| **[API.md](API.md)** | REST API & WebSocket reference | Developers, Integrators |
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | K8s deployment guide | DevOps, SRE |

---

## Documentation Map

```
KubeTriage/
├── README.md (overview + quick start)
│
├── docs/
│   ├── README.md (this file)
│   ├── ARCHITECTURE.md
│   │   └── System design, components, data models
│   ├── WORKFLOW.md
│   │   └── 7-phase investigation process
│   ├── API.md
│   │   └── REST endpoints + WebSocket + MCP tools
│   └── DEPLOYMENT.md
│       └── K8s manifests + Helm chart setup
│
├── CLAUDE.md (developer instructions)
├── HANDOFF.md (project handoff summary)
│
└── [Code: src/, tests/, deploy/]
```

---

## Reading Paths by Role

### 🚀 **First-Time User**
1. [../README.md](../README.md) — Understand what KubeTriage does
2. [WORKFLOW.md](WORKFLOW.md) — See investigation process
3. [API.md](API.md) — Try API endpoints

### 👨‍💻 **Developer**
1. [../README.md](../README.md) — Project overview
2. [ARCHITECTURE.md](ARCHITECTURE.md) — Design decisions
3. [WORKFLOW.md](WORKFLOW.md) — Phase execution
4. [../CLAUDE.md](../CLAUDE.md) — Dev instructions

### 🔧 **DevOps/SRE**
1. [../README.md](../README.md) — Quick start
2. [DEPLOYMENT.md](DEPLOYMENT.md) — K8s setup
3. [API.md](API.md) — Integration points
4. [WORKFLOW.md](WORKFLOW.md) — Investigation walkthrough

### 📊 **API Integrator**
1. [API.md](API.md) — REST endpoints
2. [WORKFLOW.md](WORKFLOW.md) — Investigation states
3. [ARCHITECTURE.md](ARCHITECTURE.md) — Data models

---

## Key Concepts

### Investigation Phases
```
INTAKE → TRIAGE → DIAGNOSE → SYNTHESIZE → REMEDIATE → VERIFY → COMPLETE
```
See [WORKFLOW.md](WORKFLOW.md) for detailed phase walkthrough.

### Specialist Agents
- **CloudWatch Log Analyzer** — Error patterns, stack traces (320 LOC)
- **Prometheus Metrics Analyzer** — Anomalies, saturation (380 LOC)
- **Graph Query Engine** — Dependencies, blast radius (FalkorDBLite)
- **Confidence Scorer** — Multi-source correlation (models/scoring.py)

See [ARCHITECTURE.md](ARCHITECTURE.md) for component details.

### Performance
| Operation | Target | Actual | Status |
|-----------|--------|--------|--------|
| Single-hop query | <10ms | 0.98ms | ✅ 10x |
| Full investigation | <5s | 0.5-2s | ✅ 2.5-10x |

See [ARCHITECTURE.md](ARCHITECTURE.md) for benchmarks.

---

## Documentation Standards

All docs follow these conventions:

1. **ASCII Diagrams** — Visual flow for complex concepts
2. **Code Examples** — Real, runnable snippets
3. **Timeline Diagrams** — Sequence of events
4. **Performance Tables** — Measurable metrics
5. **Error Codes** — Comprehensive error reference

---

## Contributing to Docs

When adding new features:
1. Update relevant doc section
2. Add ASCII diagram if > 3 steps
3. Include code example
4. Update this index

---

## Version History

- **Phase 1:** Research & Architecture (HANDOFF.md)
- **Phase 2:** MVP Implementation (CLAUDE.md)
- **Phase 2.5:** FalkorDBLite Integration (HANDOFF_PHASE2.5.md)
- **Phase 3:** Specialist APIs (HANDOFF_PHASE3.md)
- **Phase 3.1:** Documentation (this set)

Next: Phase 4 (Production Deployment)

