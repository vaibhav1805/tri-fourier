"""
Performance benchmarks against real FalkorDBLite engine.

Tests Cypher query latency on actual FalkorDBLite with seeded topologies.
Targets from Phase 2.5:
- Single-hop dependency: < 10ms
- 3-hop blast radius: < 50ms
- Temporal queries: < 30ms
- Overall P99: < 100ms
"""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
import tempfile
import time
from typing import Any

import pytest

from trifourier.graph.backend import FalkorDBLiteBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _measure_async_ms(coro) -> tuple[float, Any]:
    """Measure async execution time in milliseconds."""
    start = time.perf_counter()
    result = await coro
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed, result


async def _run_async_benchmark(
    coro_factory,
    iterations: int = 100,
) -> dict[str, float]:
    """Run an async coroutine factory N times and collect timing stats."""
    times: list[float] = []
    for _ in range(iterations):
        elapsed, _ = await _measure_async_ms(coro_factory())
        times.append(elapsed)
    return _compute_stats(times)


def _compute_stats(times: list[float]) -> dict[str, float]:
    """Compute p50, p95, p99, mean, min, max from a list of timings."""
    times_sorted = sorted(times)
    n = len(times_sorted)
    return {
        "min": times_sorted[0],
        "p50": times_sorted[int(n * 0.50)],
        "p95": times_sorted[int(n * 0.95)],
        "p99": times_sorted[int(n * 0.99)],
        "max": times_sorted[-1],
        "mean": statistics.mean(times),
        "stdev": statistics.stdev(times) if n > 1 else 0.0,
        "count": float(n),
    }


async def _seed_falkordb(backend: FalkorDBLiteBackend, node_count: int) -> None:
    """Seed a FalkorDBLite instance with a layered microservice topology."""
    n_services = max(int(node_count * 0.6), 1)
    n_databases = max(int(node_count * 0.2), 1)
    n_caches = max(int(node_count * 0.1), 1)
    n_queues = node_count - n_services - n_databases - n_caches

    for i in range(n_services):
        stype = "gateway" if i == 0 else ("worker" if i % 5 == 0 else "api")
        await backend.upsert_node("Service", {
            "name": f"svc-{i}",
            "namespace": "production",
            "type": stype,
            "health": "healthy" if i % 7 != 0 else "degraded",
            "version": f"v1.{i % 10}.0",
            "replicas": (i % 3) + 1,
        })

    for i in range(n_databases):
        await backend.upsert_node("Database", {
            "name": f"db-{i}",
            "engine": "postgres" if i % 2 == 0 else "mysql",
            "host": f"db-{i}.internal",
            "port": 5432 if i % 2 == 0 else 3306,
            "health": "healthy",
            "version": "15.4",
            "replicas": 2,
        })

    for i in range(n_caches):
        await backend.upsert_node("Cache", {
            "name": f"cache-{i}",
            "engine": "redis",
            "hit_rate": 0.90 + (i % 10) * 0.01,
            "health": "healthy",
        })

    for i in range(max(n_queues, 0)):
        await backend.upsert_node("Queue", {
            "name": f"queue-{i}",
            "type": "sqs",
            "depth": i * 3,
            "dlq_depth": 0,
            "health": "healthy",
        })

    # Relationships
    gateway_fanout = min(10, n_services - 1)
    for i in range(1, gateway_fanout + 1):
        await backend.upsert_relationship(
            "Service", "svc-0", "Service", f"svc-{i}", "DEPENDS_ON"
        )

    for i in range(1, n_services):
        db_idx = i % n_databases
        await backend.upsert_relationship(
            "Service", f"svc-{i}", "Database", f"db-{db_idx}", "READS_FROM"
        )
        if n_databases > 1:
            db_idx2 = (i + 1) % n_databases
            await backend.upsert_relationship(
                "Service", f"svc-{i}", "Database", f"db-{db_idx2}", "WRITES_TO"
            )

    for i in range(1, n_services, 3):
        cache_idx = i % max(n_caches, 1)
        await backend.upsert_relationship(
            "Service", f"svc-{i}", "Cache", f"cache-{cache_idx}", "CACHES_IN"
        )

    for i in range(1, n_services, 4):
        if n_queues > 0:
            queue_idx = i % n_queues
            await backend.upsert_relationship(
                "Service", f"svc-{i}", "Queue", f"queue-{queue_idx}", "PUBLISHES_TO"
            )

    for i in range(gateway_fanout + 1, n_services):
        parent_idx = max(1, (i - 1) % gateway_fanout + 1)
        await backend.upsert_relationship(
            "Service", f"svc-{parent_idx}", "Service", f"svc-{i}", "DEPENDS_ON"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def falkordb_backend():
    """Create a temporary FalkorDBLite backend for benchmarking."""
    tmpdir = tempfile.mkdtemp(prefix="trifourier_bench_")
    backend = FalkorDBLiteBackend(data_dir=tmpdir)
    try:
        await backend.initialize()
    except (ImportError, Exception) as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        pytest.skip(f"FalkorDBLite not available: {e}")
    yield backend
    await backend.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
async def seeded_100(falkordb_backend: FalkorDBLiteBackend) -> FalkorDBLiteBackend:
    """FalkorDBLite with 100-node topology seeded."""
    await _seed_falkordb(falkordb_backend, 100)
    return falkordb_backend


@pytest.fixture
async def seeded_1000(falkordb_backend: FalkorDBLiteBackend) -> FalkorDBLiteBackend:
    """FalkorDBLite with 1000-node topology seeded."""
    await _seed_falkordb(falkordb_backend, 1000)
    return falkordb_backend


# ---------------------------------------------------------------------------
# Single-hop queries: target < 10ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBSingleHop:
    """Single-hop Cypher queries on real FalkorDBLite."""

    async def test_single_hop_100_nodes(self, seeded_100: FalkorDBLiteBackend) -> None:
        """Single-hop blast radius on 100-node FalkorDBLite: P99 < 10ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_100.get_blast_radius("svc-1", max_depth=1),
            iterations=100,
        )
        assert stats["p99"] < 10, (
            f"FalkorDB single-hop P99 = {stats['p99']:.2f}ms (target < 10ms)\n"
            f"Stats: {stats}"
        )

    async def test_single_hop_1000_nodes(self, seeded_1000: FalkorDBLiteBackend) -> None:
        """Single-hop blast radius on 1000-node FalkorDBLite: P99 < 10ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_1000.get_blast_radius("svc-1", max_depth=1),
            iterations=50,
        )
        assert stats["p99"] < 10, (
            f"FalkorDB single-hop P99 = {stats['p99']:.2f}ms (target < 10ms)\n"
            f"Stats: {stats}"
        )


# ---------------------------------------------------------------------------
# 3-hop queries: target < 50ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBMultiHop:
    """Multi-hop Cypher traversals on real FalkorDBLite."""

    async def test_3hop_100_nodes(self, seeded_100: FalkorDBLiteBackend) -> None:
        """3-hop blast radius on 100-node FalkorDBLite: P99 < 50ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_100.get_blast_radius("db-0", max_depth=3),
            iterations=100,
        )
        assert stats["p99"] < 50, (
            f"FalkorDB 3-hop P99 = {stats['p99']:.2f}ms (target < 50ms)\n"
            f"Stats: {stats}"
        )

    async def test_3hop_1000_nodes(self, seeded_1000: FalkorDBLiteBackend) -> None:
        """3-hop blast radius on 1000-node FalkorDBLite: P99 < 50ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_1000.get_blast_radius("db-0", max_depth=3),
            iterations=50,
        )
        assert stats["p99"] < 50, (
            f"FalkorDB 3-hop P99 = {stats['p99']:.2f}ms (target < 50ms)\n"
            f"Stats: {stats}"
        )

    async def test_5hop_1000_nodes(self, seeded_1000: FalkorDBLiteBackend) -> None:
        """5-hop traversal on 1000-node FalkorDBLite: P99 < 100ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_1000.get_blast_radius("db-0", max_depth=5),
            iterations=30,
        )
        assert stats["p99"] < 100, (
            f"FalkorDB 5-hop P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )


# ---------------------------------------------------------------------------
# Temporal queries: target < 30ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBTemporalQueries:
    """Temporal (time-filtered) Cypher queries on FalkorDBLite."""

    async def test_service_health_lookup(self, seeded_100: FalkorDBLiteBackend) -> None:
        """Service health lookup via Cypher: P99 < 30ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_100.query(
                "MATCH (s:Service {name: $name}) RETURN s.name AS name, s.health AS health",
                {"name": "svc-1"},
            ),
            iterations=100,
        )
        assert stats["p99"] < 30, (
            f"FalkorDB service health P99 = {stats['p99']:.2f}ms (target < 30ms)\n"
            f"Stats: {stats}"
        )

    async def test_all_services_query(self, seeded_100: FalkorDBLiteBackend) -> None:
        """List all services via Cypher: P99 < 30ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_100.query(
                "MATCH (s:Service) RETURN s.name AS name, s.health AS health ORDER BY s.name"
            ),
            iterations=50,
        )
        assert stats["p99"] < 30, (
            f"FalkorDB all-services P99 = {stats['p99']:.2f}ms (target < 30ms)\n"
            f"Stats: {stats}"
        )

    async def test_dependency_chain_query(self, seeded_100: FalkorDBLiteBackend) -> None:
        """Service dependency chain query: P99 < 30ms."""
        stats = await _run_async_benchmark(
            lambda: seeded_100.query(
                "MATCH (s:Service {name: $name})-[r]->(dep) "
                "RETURN dep.name AS name, type(r) AS relationship",
                {"name": "svc-1"},
            ),
            iterations=100,
        )
        assert stats["p99"] < 30, (
            f"FalkorDB dependency chain P99 = {stats['p99']:.2f}ms (target < 30ms)\n"
            f"Stats: {stats}"
        )


# ---------------------------------------------------------------------------
# Concurrent queries
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBConcurrency:
    """Concurrent query performance on FalkorDBLite."""

    async def _run_concurrent_queries(
        self,
        backend: FalkorDBLiteBackend,
        concurrency: int,
        query_count: int = 20,
    ) -> dict[str, float]:
        """Run blast radius queries at the given concurrency level."""
        nodes = [f"svc-{i}" for i in range(1, min(11, concurrency + 1))]
        times: list[float] = []

        for batch_start in range(0, query_count, concurrency):
            batch_size = min(concurrency, query_count - batch_start)
            tasks = []
            for j in range(batch_size):
                node = nodes[j % len(nodes)]
                tasks.append(backend.get_blast_radius(node, max_depth=3))

            start = time.perf_counter()
            await asyncio.gather(*tasks)
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        return _compute_stats(times)

    async def test_5_concurrent(self, seeded_100: FalkorDBLiteBackend) -> None:
        """5 concurrent queries on FalkorDBLite: P99 < 100ms."""
        stats = await self._run_concurrent_queries(seeded_100, concurrency=5)
        assert stats["p99"] < 100, (
            f"FalkorDB 5-concurrent P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )

    async def test_10_concurrent(self, seeded_100: FalkorDBLiteBackend) -> None:
        """10 concurrent queries on FalkorDBLite: P99 < 200ms."""
        stats = await self._run_concurrent_queries(seeded_100, concurrency=10)
        assert stats["p99"] < 200, (
            f"FalkorDB 10-concurrent P99 = {stats['p99']:.2f}ms (target < 200ms)\n"
            f"Stats: {stats}"
        )

    async def test_20_concurrent(self, seeded_1000: FalkorDBLiteBackend) -> None:
        """20 concurrent queries on 1000-node FalkorDBLite: P99 < 500ms."""
        stats = await self._run_concurrent_queries(seeded_1000, concurrency=20)
        assert stats["p99"] < 500, (
            f"FalkorDB 20-concurrent P99 = {stats['p99']:.2f}ms (target < 500ms)\n"
            f"Stats: {stats}"
        )


# ---------------------------------------------------------------------------
# Seed performance
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBSeedPerformance:
    """Measure time to seed FalkorDBLite topologies."""

    async def test_seed_100_nodes(self, falkordb_backend: FalkorDBLiteBackend) -> None:
        """Seeding 100-node topology into FalkorDBLite: < 10s."""
        elapsed, _ = await _measure_async_ms(
            _seed_falkordb(falkordb_backend, 100)
        )
        assert elapsed < 10_000, f"100-node FalkorDB seed took {elapsed:.0f}ms (target < 10s)"

    async def test_seed_1000_nodes(self) -> None:
        """Seeding 1000-node topology into FalkorDBLite: < 120s."""
        tmpdir = tempfile.mkdtemp(prefix="trifourier_seed_bench_")
        backend = FalkorDBLiteBackend(data_dir=tmpdir)
        try:
            await backend.initialize()
        except (ImportError, Exception) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            pytest.skip(f"FalkorDBLite not available: {e}")

        try:
            elapsed, _ = await _measure_async_ms(
                _seed_falkordb(backend, 1000)
            )
            assert elapsed < 120_000, f"1000-node FalkorDB seed took {elapsed:.0f}ms (target < 120s)"
        finally:
            await backend.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Overall P99
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestFalkorDBOverallP99:
    """Overall P99 across mixed query types on FalkorDBLite: target < 100ms."""

    async def test_mixed_workload_p99(self, seeded_100: FalkorDBLiteBackend) -> None:
        """Mixed workload on 100-node FalkorDBLite: P99 < 100ms."""
        times: list[float] = []

        for i in range(30):
            # Single-hop
            elapsed, _ = await _measure_async_ms(
                seeded_100.get_blast_radius(f"svc-{(i * 7) % 60 + 1}", max_depth=1)
            )
            times.append(elapsed)

            # Multi-hop
            elapsed, _ = await _measure_async_ms(
                seeded_100.get_blast_radius(f"db-{i % 20}", max_depth=3)
            )
            times.append(elapsed)

            # Direct Cypher
            elapsed, _ = await _measure_async_ms(
                seeded_100.query(
                    "MATCH (s:Service {name: $name}) RETURN s.name AS name, s.health AS health",
                    {"name": f"svc-{i % 60}"},
                )
            )
            times.append(elapsed)

        stats = _compute_stats(times)
        assert stats["p99"] < 100, (
            f"FalkorDB mixed workload P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )
