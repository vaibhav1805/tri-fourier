"""Knowledge graph schema definitions (ARCHITECTURE.md Section 5).

Defines node types, relationship types, and Cypher query templates
for the service dependency knowledge graph.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------


class NodeType(StrEnum):
    SERVICE = "Service"
    DATABASE = "Database"
    QUEUE = "Queue"
    CACHE = "Cache"
    LOAD_BALANCER = "LoadBalancer"
    KUBERNETES_RESOURCE = "KubernetesResource"
    AWS_RESOURCE = "AWSResource"
    INCIDENT = "Incident"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class ServiceNode(BaseModel):
    name: str
    namespace: str = "default"
    service_type: str = "api"  # api, worker, cron, gateway
    health: HealthStatus = HealthStatus.UNKNOWN
    version: str = ""
    replicas: int = 1
    owner_team: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatabaseNode(BaseModel):
    name: str
    engine: str = "postgres"  # postgres, mysql, redis, mongo, dynamodb
    host: str = ""
    port: int = 5432
    health: HealthStatus = HealthStatus.UNKNOWN
    version: str = ""
    replicas: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueueNode(BaseModel):
    name: str
    queue_type: str = "sqs"  # sqs, kafka, rabbitmq, sns
    depth: int = 0
    dlq_depth: int = 0
    health: HealthStatus = HealthStatus.UNKNOWN
    metadata: dict[str, Any] = Field(default_factory=dict)


class CacheNode(BaseModel):
    name: str
    engine: str = "redis"  # redis, memcached, elasticache
    hit_rate: float = 0.0
    health: HealthStatus = HealthStatus.UNKNOWN
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Relationship types
# ---------------------------------------------------------------------------


class RelationType(StrEnum):
    DEPENDS_ON = "DEPENDS_ON"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    PUBLISHES_TO = "PUBLISHES_TO"
    CONSUMES_FROM = "CONSUMES_FROM"
    CACHES_IN = "CACHES_IN"
    ROUTES_THROUGH = "ROUTES_THROUGH"
    RUNS_ON = "RUNS_ON"
    USES = "USES"
    AFFECTS = "AFFECTS"
    CAUSED_BY = "CAUSED_BY"


class Relationship(BaseModel):
    from_name: str
    from_type: NodeType
    to_name: str
    to_type: NodeType
    rel_type: RelationType
    properties: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cypher query templates
# ---------------------------------------------------------------------------

CYPHER_QUERIES = {
    "service_dependencies": """
        MATCH (s:Service {name: $service_name})-[r]->(dep)
        RETURN dep.name AS name, labels(dep)[0] AS type, type(r) AS relationship,
               dep.health AS health
    """,
    "blast_radius": """
        MATCH path = (s {name: $node_name})<-[*1..$max_depth]-(affected)
        RETURN affected.name AS name, labels(affected)[0] AS type,
               length(path) AS depth
        ORDER BY depth
    """,
    "service_dependents": """
        MATCH (dep)<-[r]-(s:Service {name: $service_name})
        RETURN s.name AS name, type(r) AS relationship
    """,
    "service_health": """
        MATCH (s:Service {name: $service_name})
        RETURN s.name AS name, s.health AS health, s.replicas AS replicas,
               s.version AS version
    """,
    "recent_incidents": """
        MATCH (s:Service {name: $service_name})<-[:AFFECTS]-(i:Incident)
        WHERE i.start_time > $since
        RETURN i.id AS id, i.title AS title, i.severity AS severity,
               i.status AS status
        ORDER BY i.start_time DESC
    """,
    "all_services": """
        MATCH (s:Service)
        RETURN s.name AS name, s.namespace AS namespace, s.health AS health,
               s.replicas AS replicas
        ORDER BY s.name
    """,
}
