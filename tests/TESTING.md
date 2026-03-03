# TriageBot Testing Conventions

## Directory Structure

```
tests/
  conftest.py                 -- Shared fixtures, factories, markers
  pytest.ini                  -- Pytest configuration
  requirements-test.txt       -- Test-only dependencies
  TESTING.md                  -- This file
  fixtures/                   -- JSON test data
    sample_logs.json          -- CloudWatch log scenarios
    sample_metrics.json       -- Prometheus metric scenarios
    sample_k8s.json           -- Kubernetes resource states
  unit/                       -- Fast, isolated tests (no external deps)
    test_confidence_scoring.py
    test_graph_queries.py
    test_log_analyzer.py
    test_metrics_analyzer.py
    test_orchestrator.py
  integration/                -- Tests crossing module boundaries
    test_orchestrator_specialists.py
    test_graph_tools.py
  e2e/                        -- Full workflow tests
    test_log_spike_investigation.py
    test_slack_workflow.py
  performance/                -- Latency and throughput benchmarks
    test_response_times.py
  security/                   -- Security validation
    test_input_validation.py
```

## Running Tests

```bash
# All tests (unit + integration)
pytest

# Unit tests only
pytest -m unit

# Integration tests only
pytest -m integration

# E2E tests
pytest -m e2e

# Performance benchmarks
pytest -m performance

# Security tests
pytest -m security

# With coverage
pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# Parallel execution
pytest -n auto
```

## Test Markers

| Marker | Purpose | Speed |
|--------|---------|-------|
| `@pytest.mark.unit` | Isolated unit tests, no external deps | Fast (< 1s each) |
| `@pytest.mark.integration` | Cross-module tests, may use embedded graph | Medium (< 5s each) |
| `@pytest.mark.e2e` | Full workflow tests with mocked externals | Slow (< 30s each) |
| `@pytest.mark.performance` | Latency/throughput benchmarks | Variable |
| `@pytest.mark.security` | Security validation tests | Fast-Medium |
| `@pytest.mark.slow` | Any test taking > 5 seconds | Slow |

## Fixture Conventions

1. **Node factories** (`make_service`, `make_database`, `make_queue`): create graph node dicts
2. **Finding factory** (`DiagnosticFindingFactory`): create diagnostic findings with `.build()`
3. **Mock fixtures** (`mock_cloudwatch`, `mock_prometheus`, etc.): pre-configured mocks for external systems
4. **File fixtures** (`load_fixture`): load JSON from `fixtures/` directory

## SLAs Under Test

| Metric | Target | Test |
|--------|--------|------|
| Agent response (typical investigation) | < 30s | `test_typical_investigation_under_30s` |
| Graph query (dependency lookup) | < 100ms | `test_single_hop_dependency_query_under_100ms` |
| Graph query (blast radius) | < 100ms | `test_multi_hop_blast_radius_under_100ms` |
| API health check | < 50ms | `test_health_check_under_50ms` |
| Code coverage | >= 80% | pytest-cov |

## Security Test Checklist

- [ ] Cypher injection prevention
- [ ] SQL injection prevention
- [ ] Command injection prevention
- [ ] XSS in Slack output prevention
- [ ] Oversized input rejection
- [ ] Remediation approval gate enforcement
- [ ] Confidence threshold enforcement
- [ ] Protected namespace enforcement
- [ ] Blast radius limit enforcement
- [ ] Credential scrubbing (AWS keys, DB passwords, K8s secrets, API keys)

## Writing New Tests

1. Place test in the correct directory based on scope (unit/integration/e2e/etc.)
2. Add appropriate marker (`@pytest.mark.unit`, etc.)
3. Use existing fixtures from `conftest.py` when possible
4. For external system calls, always use mocks (never call real AWS/K8s/Prometheus)
5. Name tests descriptively: `test_<what>_<condition>_<expected_outcome>`
6. Keep unit tests fast (< 1 second each)
7. Skip tests that depend on unimplemented code with `@pytest.mark.skip(reason="Awaiting X implementation")`
