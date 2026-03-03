"""Seed the knowledge graph with sample service topology.

Run: python -m scripts.seed_graph

Creates a realistic e-commerce service dependency graph for testing
the triage investigation workflow.
"""

from __future__ import annotations

import asyncio

from triagebot.graph.backend import get_graph_backend


async def seed() -> None:
    """Populate the graph with sample data."""
    graph = await get_graph_backend()

    # -- Services --
    services = [
        {"name": "api-gateway", "namespace": "production", "type": "gateway", "health": "healthy", "version": "v1.2.0", "replicas": 3},
        {"name": "checkout-api", "namespace": "production", "type": "api", "health": "degraded", "version": "v2.3.1", "replicas": 3},
        {"name": "payment-api", "namespace": "production", "type": "api", "health": "healthy", "version": "v1.8.0", "replicas": 2},
        {"name": "order-api", "namespace": "production", "type": "api", "health": "healthy", "version": "v3.1.0", "replicas": 3},
        {"name": "notification-svc", "namespace": "production", "type": "worker", "health": "healthy", "version": "v1.0.4", "replicas": 2},
        {"name": "inventory-api", "namespace": "production", "type": "api", "health": "healthy", "version": "v2.0.0", "replicas": 2},
    ]
    for s in services:
        await graph.upsert_node("Service", s)

    # -- Databases --
    databases = [
        {"name": "cart-db", "engine": "postgres", "host": "cart-db.internal", "port": 5432, "health": "degraded", "version": "15.4", "replicas": 2},
        {"name": "payments-db", "engine": "postgres", "host": "payments-db.internal", "port": 5432, "health": "healthy", "version": "15.4", "replicas": 2},
        {"name": "orders-db", "engine": "postgres", "host": "orders-db.internal", "port": 5432, "health": "healthy", "version": "15.4", "replicas": 2},
        {"name": "inventory-db", "engine": "postgres", "host": "inventory-db.internal", "port": 5432, "health": "healthy", "version": "15.4", "replicas": 1},
    ]
    for d in databases:
        await graph.upsert_node("Database", d)

    # -- Caches --
    caches = [
        {"name": "session-cache", "engine": "redis", "hit_rate": 0.95, "health": "healthy"},
        {"name": "product-cache", "engine": "redis", "hit_rate": 0.88, "health": "healthy"},
    ]
    for c in caches:
        await graph.upsert_node("Cache", c)

    # -- Queues --
    queues = [
        {"name": "order-events", "type": "sqs", "depth": 12, "dlq_depth": 0, "health": "healthy"},
        {"name": "notification-events", "type": "sqs", "depth": 3, "dlq_depth": 0, "health": "healthy"},
    ]
    for q in queues:
        await graph.upsert_node("Queue", q)

    # -- Relationships --
    rels = [
        ("Service", "api-gateway", "Service", "checkout-api", "DEPENDS_ON"),
        ("Service", "api-gateway", "Service", "order-api", "DEPENDS_ON"),
        ("Service", "api-gateway", "Service", "inventory-api", "DEPENDS_ON"),
        ("Service", "checkout-api", "Database", "cart-db", "READS_FROM"),
        ("Service", "checkout-api", "Service", "payment-api", "DEPENDS_ON"),
        ("Service", "checkout-api", "Cache", "session-cache", "CACHES_IN"),
        ("Service", "checkout-api", "Service", "inventory-api", "DEPENDS_ON"),
        ("Service", "payment-api", "Database", "payments-db", "READS_FROM"),
        ("Service", "order-api", "Database", "orders-db", "READS_FROM"),
        ("Service", "order-api", "Queue", "order-events", "PUBLISHES_TO"),
        ("Service", "notification-svc", "Queue", "order-events", "CONSUMES_FROM"),
        ("Service", "notification-svc", "Queue", "notification-events", "PUBLISHES_TO"),
        ("Service", "inventory-api", "Database", "inventory-db", "READS_FROM"),
        ("Service", "inventory-api", "Cache", "product-cache", "CACHES_IN"),
    ]
    for from_label, from_key, to_label, to_key, rel_type in rels:
        await graph.upsert_relationship(from_label, from_key, to_label, to_key, rel_type)

    print(f"Seeded graph: {len(services)} services, {len(databases)} databases, "
          f"{len(caches)} caches, {len(queues)} queues, {len(rels)} relationships")


if __name__ == "__main__":
    asyncio.run(seed())
