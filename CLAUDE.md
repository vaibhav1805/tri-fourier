# Trifourier - Kubernetes-first Production Troubleshooting Agent

## Quick Start
```bash
cd projects/trifourier
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m unit
```

## Project Structure
```
src/trifourier/
  agents/       -- Strands Agent definitions (orchestrator, specialists)
  tools/        -- @tool functions (CloudWatch, Prometheus, kubectl, etc.)
  skills/       -- Skill loading (Agent Skills spec / agentskills.io)
  graph/        -- Knowledge graph layer (Graphiti + FalkorDBLite)
  models/       -- Pydantic models (findings, scoring, investigation state)
  config/       -- Settings from environment variables
  api/          -- FastAPI server (REST + WebSocket)
  cli.py        -- CLI entry point
src/api/        -- Legacy API entry (used by Dockerfile)
tests/          -- pytest suite (unit, integration, e2e, performance, security)
deploy/         -- Helm chart + raw K8s manifests
skills/         -- Agent Skills YAML/MD files
```

## Tech Stack
- Python 3.12+ / pip / pyproject.toml (hatchling)
- Strands Agents SDK 1.28+ (agent framework)
- Graphiti + FalkorDBLite (embedded knowledge graph)
- FastAPI + Uvicorn (API server)
- Slack SDK + Slack Bolt (Slack integration)
- Pydantic v2 (data models)
- pytest (testing)
- ruff (linting) / mypy (type checking)

## Key Architecture
- **Orchestrator**: Strands GraphBuilder pattern for phase routing
- **Specialists**: Agents-as-tools pattern (sub-agents wrapped as @tool)
- **Graph**: FalkorDBLite embedded subprocess, Graphiti framework layer
- **Phases**: INTAKE -> TRIAGE -> DIAGNOSE -> SYNTHESIZE -> REMEDIATE/REPORT -> VERIFY -> COMPLETE

## Commands
- Run tests: `pytest -m unit`
- Type check: `mypy src/`
- Lint: `ruff check src/`
- Start API: `uvicorn src.api.main:app --reload`
- Docker: `docker compose up --build`
- CLI: `python -m trifourier.cli investigate "checkout is slow"`

## Implementation Status (Phase 2)
- [x] Project structure and pyproject.toml
- [x] Settings / configuration (pydantic-settings)
- [x] Data models (findings, scoring, investigation state)
- [x] Confidence scoring module
- [x] Dockerfile, docker-compose, K8s manifests, Helm chart, CI/CD
- [x] Test fixtures and stub test suite
- [x] Orchestrator agent (agents/orchestrator.py - InvestigationEngine with Strands agents-as-tools)
- [x] Log analyzer specialist agent (@tool wrapped sub-agent)
- [x] Metrics analyzer specialist agent (@tool wrapped sub-agent)
- [x] Knowledge graph layer (graph/backend.py - InMemoryGraphBackend + FalkorDBLiteBackend)
- [x] Graph schema (graph/schema.py - all node/relationship types from ARCHITECTURE.md)
- [x] FastAPI routes (api/server.py - triage, investigation, approval, list)
- [x] WebSocket streaming (api/server.py - /ws/investigation/{id})
- [x] Slack bot integration (api/slack_bot.py - /triage command, approval buttons)
- [x] Agent Skills (skills/log-analyzer/SKILL.md, skills/metrics-analyzer/SKILL.md)
- [x] Graph seed script (scripts/seed_graph.py)
- [ ] Wire graph tools to real FalkorDBLite (currently using mock data in tools)
- [ ] Wire specialist agents to real CloudWatch/Prometheus APIs
- [ ] Activate skipped unit tests with real implementations

## Reference Docs
- Architecture: /research/trifourier/ARCHITECTURE.md
- Project plan: /research/trifourier/PROJECT.md
- Strands docs: https://strandsagents.com/latest/documentation/docs/
