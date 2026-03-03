"""
Unit tests for knowledge graph query functions.

Tests graph operations from ARCHITECTURE.md Section 5:
- Dependency lookups (1-hop)
- Blast radius calculation (multi-hop)
- Node upsert/update operations

Uses InMemoryGraphBackend directly -- no FalkorDBLite required.
"""

from __future__ import annotations

import pytest

from triagebot.graph.backend import InMemoryGraphBackend


@pytest.fixture
async def graph() -> InMemoryGraphBackend:
    """Seeded InMemoryGraphBackend for graph query tests.

    Topology:
        gateway -> checkout-api -> cart-db
                                -> payment-api -> payments-db
                                -> redis-cache
                -> order-api   -> order-events (queue)
                               -> orders-db
    """
    backend = InMemoryGraphBackend()
    await backend.initialize()

    # Nodes
    await backend.upsert_node("Service", {"name": "gateway", "type": "gateway", "health": "healthy"})
    await backend.upsert_node("Service", {"name": "checkout-api", "type": "api", "health": "healthy"})
    await backend.upsert_node("Service", {"name": "payment-api", "type": "api", "health": "healthy"})
    await backend.upsert_node("Service", {"name": "order-api", "type": "api", "health": "healthy"})
    await backend.upsert_node("Database", {"name": "cart-db", "engine": "postgres", "health": "healthy"})
    await backend.upsert_node("Database", {"name": "payments-db", "engine": "postgres", "health": "healthy"})
    await backend.upsert_node("Database", {"name": "orders-db", "engine": "postgres", "health": "healthy"})
    await backend.upsert_node("Cache", {"name": "redis-cache", "engine": "redis", "health": "healthy"})
    await backend.upsert_node("Queue", {"name": "order-events", "type": "sqs", "health": "healthy"})

    # Relationships
    await backend.upsert_relationship("Service", "gateway", "Service", "checkout-api", "DEPENDS_ON")
    await backend.upsert_relationship("Service", "gateway", "Service", "order-api", "DEPENDS_ON")
    await backend.upsert_relationship("Service", "checkout-api", "Database", "cart-db", "READS_FROM")
    await backend.upsert_relationship("Service", "checkout-api", "Service", "payment-api", "DEPENDS_ON")
    await backend.upsert_relationship("Service", "checkout-api", "Cache", "redis-cache", "CACHES_IN")
    await backend.upsert_relationship("Service", "payment-api", "Database", "payments-db", "READS_FROM")
    await backend.upsert_relationship("Service", "order-api", "Database", "orders-db", "READS_FROM")
    await backend.upsert_relationship("Service", "order-api", "Queue", "order-events", "PUBLISHES_TO")

    return backend


@pytest.mark.unit
class TestDependencyLookup:
    """Test single-hop dependency queries."""

    def test_sample_graph_has_expected_services(self, sample_dependency_graph: dict) -> None:
        """Fixture should contain the expected service topology."""
        service_names = [s["name"] for s in sample_dependency_graph["services"]]
        assert "checkout-api" in service_names
        assert "payment-api" in service_names
        assert "gateway" in service_names

    def test_sample_graph_has_expected_relationships(self, sample_dependency_graph: dict) -> None:
        """Fixture should contain the expected relationship types."""
        rel_types = {r["type"] for r in sample_dependency_graph["relationships"]}
        assert "DEPENDS_ON" in rel_types
        assert "READS_FROM" in rel_types

    async def test_find_direct_dependents_of_database(self, graph: InMemoryGraphBackend) -> None:
        """Blast radius depth=1 of cart-db should include checkout-api."""
        result = await graph.get_blast_radius("cart-db", max_depth=1)
        names = [r["name"] for r in result]
        assert "checkout-api" in names

    async def test_direct_dependents_excludes_unrelated(self, graph: InMemoryGraphBackend) -> None:
        """Blast radius of cart-db should NOT include order-api."""
        result = await graph.get_blast_radius("cart-db", max_depth=1)
        names = [r["name"] for r in result]
        assert "order-api" not in names


@pytest.mark.unit
class TestBlastRadius:
    """Test multi-hop blast radius calculation."""

    async def test_blast_radius_returns_depth_ordered_results(self, graph: InMemoryGraphBackend) -> None:
        """Results should be ordered by traversal depth."""
        result = await graph.get_blast_radius("cart-db", max_depth=3)
        if len(result) > 1:
            depths = [r["depth"] for r in result]
            assert depths == sorted(depths), f"Not depth-ordered: {depths}"

    async def test_blast_radius_respects_max_depth(self, graph: InMemoryGraphBackend) -> None:
        """Should not return nodes beyond max_depth."""
        result = await graph.get_blast_radius("cart-db", max_depth=1)
        assert all(r["depth"] <= 1 for r in result), f"Found depth > 1: {result}"

    async def test_blast_radius_of_leaf_node_is_empty(self, graph: InMemoryGraphBackend) -> None:
        """A node with no dependents should have empty blast radius."""
        result = await graph.get_blast_radius("gateway", max_depth=3)
        # gateway is root -- nothing depends ON it
        assert len(result) == 0, f"Expected empty blast radius for gateway, got: {result}"

    async def test_blast_radius_includes_transitive_dependencies(self, graph: InMemoryGraphBackend) -> None:
        """cart-db blast radius should reach gateway (cart-db <- checkout-api <- gateway)."""
        result = await graph.get_blast_radius("cart-db", max_depth=3)
        names = [r["name"] for r in result]
        assert "checkout-api" in names, f"Missing checkout-api: {names}"
        assert "gateway" in names, f"Missing gateway: {names}"

    async def test_blast_radius_of_payments_db(self, graph: InMemoryGraphBackend) -> None:
        """payments-db blast radius: payment-api -> checkout-api -> gateway."""
        result = await graph.get_blast_radius("payments-db", max_depth=3)
        names = [r["name"] for r in result]
        assert "payment-api" in names
        assert "checkout-api" in names
        assert "gateway" in names


@pytest.mark.unit
class TestNodeOperations:
    """Test node upsert and update operations."""

    async def test_upsert_service_creates_new_node(self) -> None:
        """Upserting a new service should create it."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        await backend.upsert_node("Service", {"name": "new-svc", "health": "healthy"})
        assert "Service:new-svc" in backend.nodes

    async def test_upsert_service_updates_existing_node(self) -> None:
        """Upserting an existing service should update it, not duplicate."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        await backend.upsert_node("Service", {"name": "svc-a", "health": "healthy"})
        await backend.upsert_node("Service", {"name": "svc-a", "health": "degraded"})
        assert len([k for k in backend.nodes if "svc-a" in k]) == 1
        assert backend.nodes["Service:svc-a"]["health"] == "degraded"

    async def test_upsert_relationship_creates_edge(self) -> None:
        """Upserting a relationship should create the edge."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        await backend.upsert_node("Service", {"name": "a"})
        await backend.upsert_node("Service", {"name": "b"})
        await backend.upsert_relationship("Service", "a", "Service", "b", "DEPENDS_ON")
        assert len(backend.relationships) == 1
        assert backend.relationships[0]["type"] == "DEPENDS_ON"

    async def test_upsert_relationship_is_idempotent(self) -> None:
        """Upserting the same relationship twice should not duplicate it."""
        backend = InMemoryGraphBackend()
        await backend.initialize()
        await backend.upsert_node("Service", {"name": "a"})
        await backend.upsert_node("Service", {"name": "b"})
        await backend.upsert_relationship("Service", "a", "Service", "b", "DEPENDS_ON")
        await backend.upsert_relationship("Service", "a", "Service", "b", "DEPENDS_ON")
        assert len(backend.relationships) == 1
