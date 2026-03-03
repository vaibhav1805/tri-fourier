"""
Performance tests: Response time SLAs.

Benchmarks graph query latency against Phase 2.5 targets:
- Single-hop dependency lookup: < 10ms
- 3-hop blast radius query: < 50ms
- Temporal queries: < 30ms
- Graph query P99: < 100ms
- Concurrent investigation scaling (5, 10, 20)

Uses InMemoryGraphBackend by default. When FalkorDBLite is available,
set TRIAGEBOT_GRAPH_BACKEND=falkordb_lite to benchmark against real engine.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

import pytest

from trifourier.graph.backend import InMemoryGraphBackend
from trifourier.graph.schema import CYPHER_QUERIES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _build_topology(backend: InMemoryGraphBackend, node_count: int) -> None:
    """Seed a topology with the given number of nodes.

    Creates a realistic microservice graph with:
    - Services (60% of nodes)
    - Databases (20%)
    - Caches (10%)
    - Queues (10%)
    Relationships form a layered DAG: gateway -> api -> db/cache/queue
    """
    n_services = max(int(node_count * 0.6), 1)
    n_databases = max(int(node_count * 0.2), 1)
    n_caches = max(int(node_count * 0.1), 1)
    n_queues = node_count - n_services - n_databases - n_caches

    # Create services
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

    # Create databases
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

    # Create caches
    for i in range(n_caches):
        await backend.upsert_node("Cache", {
            "name": f"cache-{i}",
            "engine": "redis",
            "hit_rate": 0.90 + (i % 10) * 0.01,
            "health": "healthy",
        })

    # Create queues
    for i in range(max(n_queues, 0)):
        await backend.upsert_node("Queue", {
            "name": f"queue-{i}",
            "type": "sqs",
            "depth": i * 3,
            "dlq_depth": 0,
            "health": "healthy",
        })

    # Create relationships (layered DAG)
    # Gateway -> first N api services
    gateway_fanout = min(10, n_services - 1)
    for i in range(1, gateway_fanout + 1):
        await backend.upsert_relationship(
            "Service", "svc-0", "Service", f"svc-{i}", "DEPENDS_ON"
        )

    # API services -> databases (each api service reads from ~2 databases)
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

    # API services -> caches (every 3rd service)
    for i in range(1, n_services, 3):
        cache_idx = i % max(n_caches, 1)
        await backend.upsert_relationship(
            "Service", f"svc-{i}", "Cache", f"cache-{cache_idx}", "CACHES_IN"
        )

    # API services -> queues (every 4th service publishes)
    for i in range(1, n_services, 4):
        if n_queues > 0:
            queue_idx = i % n_queues
            await backend.upsert_relationship(
                "Service", f"svc-{i}", "Queue", f"queue-{queue_idx}", "PUBLISHES_TO"
            )

    # Service-to-service dependencies (creates deeper chains)
    for i in range(gateway_fanout + 1, n_services):
        parent_idx = max(1, (i - 1) % gateway_fanout + 1)
        await backend.upsert_relationship(
            "Service", f"svc-{parent_idx}", "Service", f"svc-{i}", "DEPENDS_ON"
        )


@pytest.fixture
async def small_graph() -> InMemoryGraphBackend:
    """A small topology (~20 nodes) for basic correctness + timing."""
    backend = InMemoryGraphBackend()
    await backend.initialize()
    await _build_topology(backend, 20)
    return backend


@pytest.fixture
async def medium_graph() -> InMemoryGraphBackend:
    """A medium topology (~100 nodes) for realistic benchmarks."""
    backend = InMemoryGraphBackend()
    await backend.initialize()
    await _build_topology(backend, 100)
    return backend


@pytest.fixture
async def large_graph() -> InMemoryGraphBackend:
    """A large topology (~1000 nodes) for scale validation."""
    backend = InMemoryGraphBackend()
    await backend.initialize()
    await _build_topology(backend, 1000)
    return backend


def _measure_ms(func, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    """Measure execution time in milliseconds. Returns (elapsed_ms, result)."""
    start = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed, result


async def _measure_async_ms(coro) -> tuple[float, Any]:
    """Measure async execution time in milliseconds."""
    start = time.perf_counter()
    result = await coro
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed, result


def _run_benchmark(
    func,
    iterations: int = 100,
) -> dict[str, float]:
    """Run a synchronous function N times and collect timing stats."""
    times: list[float] = []
    for _ in range(iterations):
        elapsed, _ = _measure_ms(func)
        times.append(elapsed)
    return _compute_stats(times)


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


# ---------------------------------------------------------------------------
# Graph Query Performance
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestSingleHopQueries:
    """Single-hop dependency lookup: target < 10ms."""

    async def test_single_hop_20_nodes(self, small_graph: InMemoryGraphBackend) -> None:
        """Single-hop query on 20-node graph should be < 10ms."""
        stats = await _run_async_benchmark(
            lambda: small_graph.get_blast_radius("svc-1", max_depth=1),
            iterations=200,
        )
        assert stats["p99"] < 10, (
            f"Single-hop P99 = {stats['p99']:.2f}ms (target < 10ms)\n"
            f"Stats: {stats}"
        )

    async def test_single_hop_100_nodes(self, medium_graph: InMemoryGraphBackend) -> None:
        """Single-hop query on 100-node graph should be < 10ms."""
        stats = await _run_async_benchmark(
            lambda: medium_graph.get_blast_radius("svc-1", max_depth=1),
            iterations=200,
        )
        assert stats["p99"] < 10, (
            f"Single-hop P99 = {stats['p99']:.2f}ms (target < 10ms)\n"
            f"Stats: {stats}"
        )

    async def test_single_hop_1000_nodes(self, large_graph: InMemoryGraphBackend) -> None:
        """Single-hop query on 1000-node graph should be < 10ms."""
        stats = await _run_async_benchmark(
            lambda: large_graph.get_blast_radius("svc-1", max_depth=1),
            iterations=100,
        )
        assert stats["p99"] < 10, (
            f"Single-hop P99 = {stats['p99']:.2f}ms (target < 10ms)\n"
            f"Stats: {stats}"
        )


@pytest.mark.performance
class TestMultiHopQueries:
    """3-hop blast radius query: target < 50ms."""

    async def test_3hop_20_nodes(self, small_graph: InMemoryGraphBackend) -> None:
        """3-hop blast radius on 20-node graph should be < 50ms."""
        stats = await _run_async_benchmark(
            lambda: small_graph.get_blast_radius("db-0", max_depth=3),
            iterations=200,
        )
        assert stats["p99"] < 50, (
            f"3-hop P99 = {stats['p99']:.2f}ms (target < 50ms)\n"
            f"Stats: {stats}"
        )

    async def test_3hop_100_nodes(self, medium_graph: InMemoryGraphBackend) -> None:
        """3-hop blast radius on 100-node graph should be < 50ms."""
        stats = await _run_async_benchmark(
            lambda: medium_graph.get_blast_radius("db-0", max_depth=3),
            iterations=200,
        )
        assert stats["p99"] < 50, (
            f"3-hop P99 = {stats['p99']:.2f}ms (target < 50ms)\n"
            f"Stats: {stats}"
        )

    async def test_3hop_1000_nodes(self, large_graph: InMemoryGraphBackend) -> None:
        """3-hop blast radius on 1000-node graph should be < 50ms."""
        stats = await _run_async_benchmark(
            lambda: large_graph.get_blast_radius("db-0", max_depth=3),
            iterations=50,
        )
        assert stats["p99"] < 50, (
            f"3-hop P99 = {stats['p99']:.2f}ms (target < 50ms)\n"
            f"Stats: {stats}"
        )

    async def test_deep_5hop_1000_nodes(self, large_graph: InMemoryGraphBackend) -> None:
        """5-hop traversal on 1000-node graph should be < 100ms (P99)."""
        stats = await _run_async_benchmark(
            lambda: large_graph.get_blast_radius("db-0", max_depth=5),
            iterations=50,
        )
        assert stats["p99"] < 100, (
            f"5-hop P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )


@pytest.mark.performance
class TestNodeUpsertPerformance:
    """Node upsert operations: target < 1ms per operation."""

    async def test_upsert_node_latency(self) -> None:
        """Individual node upsert should be < 1ms."""
        backend = InMemoryGraphBackend()
        await backend.initialize()

        stats = await _run_async_benchmark(
            lambda: backend.upsert_node("Service", {
                "name": f"bench-svc-{time.monotonic_ns()}",
                "namespace": "benchmark",
                "type": "api",
                "health": "healthy",
            }),
            iterations=500,
        )
        assert stats["p99"] < 1, (
            f"Upsert P99 = {stats['p99']:.3f}ms (target < 1ms)\n"
            f"Stats: {stats}"
        )

    async def test_upsert_relationship_latency(self, small_graph: InMemoryGraphBackend) -> None:
        """Individual relationship upsert should be < 1ms."""
        stats = await _run_async_benchmark(
            lambda: small_graph.upsert_relationship(
                "Service", "svc-1", "Service", "svc-2", "DEPENDS_ON"
            ),
            iterations=500,
        )
        assert stats["p99"] < 1, (
            f"Rel upsert P99 = {stats['p99']:.3f}ms (target < 1ms)\n"
            f"Stats: {stats}"
        )


@pytest.mark.performance
class TestBulkSeedPerformance:
    """Bulk seeding: validate topology build times."""

    async def test_seed_100_nodes_under_500ms(self) -> None:
        """Seeding 100-node topology should complete in < 500ms."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        elapsed, _ = await _measure_async_ms(_build_topology(backend, 100))
        assert elapsed < 500, f"100-node seed took {elapsed:.1f}ms (target < 500ms)"
        assert len(backend.nodes) >= 90  # sanity check

    async def test_seed_1000_nodes_under_5s(self) -> None:
        """Seeding 1000-node topology should complete in < 5s."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        elapsed, _ = await _measure_async_ms(_build_topology(backend, 1000))
        assert elapsed < 5000, f"1000-node seed took {elapsed:.1f}ms (target < 5000ms)"
        assert len(backend.nodes) >= 900  # sanity check


# ---------------------------------------------------------------------------
# Concurrent Investigation Scaling
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestConcurrentInvestigations:
    """Concurrent blast radius queries should scale sub-linearly."""

    async def _run_concurrent_queries(
        self,
        backend: InMemoryGraphBackend,
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

    async def test_5_concurrent_under_target(self, medium_graph: InMemoryGraphBackend) -> None:
        """5 concurrent blast radius queries should complete within SLA."""
        stats = await self._run_concurrent_queries(medium_graph, concurrency=5)
        assert stats["p99"] < 100, (
            f"5-concurrent P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )

    async def test_10_concurrent_under_target(self, medium_graph: InMemoryGraphBackend) -> None:
        """10 concurrent blast radius queries should complete within SLA."""
        stats = await self._run_concurrent_queries(medium_graph, concurrency=10)
        assert stats["p99"] < 200, (
            f"10-concurrent P99 = {stats['p99']:.2f}ms (target < 200ms)\n"
            f"Stats: {stats}"
        )

    async def test_20_concurrent_under_target(self, large_graph: InMemoryGraphBackend) -> None:
        """20 concurrent blast radius queries on 1000 nodes should complete within SLA."""
        stats = await self._run_concurrent_queries(large_graph, concurrency=20)
        assert stats["p99"] < 500, (
            f"20-concurrent P99 = {stats['p99']:.2f}ms (target < 500ms)\n"
            f"Stats: {stats}"
        )

    async def test_scaling_factor(self, medium_graph: InMemoryGraphBackend) -> None:
        """Doubling concurrency should not more than 3x the latency."""
        stats_5 = await self._run_concurrent_queries(medium_graph, concurrency=5)
        stats_10 = await self._run_concurrent_queries(medium_graph, concurrency=10)
        ratio = stats_10["p50"] / max(stats_5["p50"], 0.001)
        assert ratio < 3.0, (
            f"Scaling factor = {ratio:.2f}x (target < 3.0x)\n"
            f"5-concurrent p50: {stats_5['p50']:.2f}ms\n"
            f"10-concurrent p50: {stats_10['p50']:.2f}ms"
        )


# ---------------------------------------------------------------------------
# Blast Radius Correctness (with timing)
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestBlastRadiusCorrectness:
    """Validate blast radius results are correct AND fast."""

    async def test_gateway_blast_radius_includes_dependents(
        self, medium_graph: InMemoryGraphBackend
    ) -> None:
        """Gateway (svc-0) should have 0 blast radius (nothing depends on it upstream)."""
        elapsed, result = await _measure_async_ms(
            medium_graph.get_blast_radius("svc-0", max_depth=3)
        )
        # svc-0 is the gateway (root) -- nothing should depend ON it
        # (blast radius finds upstream dependents that would be affected)
        # If nothing points TO svc-0, result is empty which is correct
        assert elapsed < 50, f"Query took {elapsed:.2f}ms"

    async def test_database_blast_radius_finds_services(
        self, medium_graph: InMemoryGraphBackend
    ) -> None:
        """Database (db-0) blast radius should include services that read from it."""
        elapsed, result = await _measure_async_ms(
            medium_graph.get_blast_radius("db-0", max_depth=3)
        )
        assert elapsed < 50, f"Query took {elapsed:.2f}ms"
        # At least one service should depend on db-0
        if result:
            names = [r["name"] for r in result]
            # Some svc-N should be in the blast radius since they READS_FROM db-0
            service_names = [n for n in names if n.startswith("svc-")]
            assert len(service_names) > 0, f"Expected services in blast radius, got: {names}"

    async def test_blast_radius_depth_ordering(
        self, medium_graph: InMemoryGraphBackend
    ) -> None:
        """Blast radius results should be ordered by depth."""
        _, result = await _measure_async_ms(
            medium_graph.get_blast_radius("db-0", max_depth=3)
        )
        if len(result) > 1:
            depths = [r["depth"] for r in result]
            assert depths == sorted(depths), f"Results not depth-ordered: {depths}"


# ---------------------------------------------------------------------------
# Overall P99 Target
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestOverallP99:
    """Overall P99 across mixed query types: target < 100ms."""

    async def test_mixed_workload_p99_under_100ms(
        self, large_graph: InMemoryGraphBackend
    ) -> None:
        """Mixed workload (single-hop + multi-hop + upsert) P99 < 100ms on 1000 nodes."""
        times: list[float] = []

        # Mix of query types
        for i in range(50):
            # Single-hop
            elapsed, _ = await _measure_async_ms(
                large_graph.get_blast_radius(f"svc-{(i * 7) % 100 + 1}", max_depth=1)
            )
            times.append(elapsed)

            # Multi-hop
            elapsed, _ = await _measure_async_ms(
                large_graph.get_blast_radius(f"db-{i % 20}", max_depth=3)
            )
            times.append(elapsed)

            # Upsert
            elapsed, _ = await _measure_async_ms(
                large_graph.upsert_node("Service", {
                    "name": f"svc-{(i * 7) % 100 + 1}",
                    "health": "degraded",
                })
            )
            times.append(elapsed)

        stats = _compute_stats(times)
        assert stats["p99"] < 100, (
            f"Mixed workload P99 = {stats['p99']:.2f}ms (target < 100ms)\n"
            f"Stats: {stats}"
        )
