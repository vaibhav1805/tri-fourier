# TriageBot — Kubernetes-first Production Troubleshooting Agent

AI-powered agent for diagnosing production incidents in Kubernetes environments. Uses LLM-powered specialist agents to analyze logs, metrics, and infrastructure state, correlates findings, and can auto-remediate with safety controls.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest -m unit

# Start development server
uvicorn src.triagebot.api.server:app --reload --port 8000
```

## Architecture

See `/research/autotriage/ARCHITECTURE.md` for full design.

- **Orchestrator**: Strands Agent with Graph Pattern for phase routing
- **Specialists**: Log Analyzer, Metrics Analyzer, K8s Inspector, DB Query, AWS Inspector
- **Graph**: Graphiti + FalkorDBLite for knowledge graph (embedded)
- **API**: FastAPI with WebSocket streaming + Slack integration

## Implementation Status

Phase 2 MVP in progress. See `CLAUDE.md` for detailed status.

## License

Apache 2.0
