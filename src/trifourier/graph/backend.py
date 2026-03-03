"""Graph backend abstraction and FalkorDBLite implementation.

Provides a clean interface for graph operations that can be backed by
FalkorDBLite (embedded) or FalkorDB server (production scale-up).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import structlog

from trifourier.graph.schema import NodeType, RelationType

logger = structlog.get_logger()


class GraphBackend(ABC):
    """Abstract graph backend interface."""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the graph database connection and schema."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the graph database connection."""
        ...

    @abstractmethod
    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a Cypher query and return results."""
        ...

    @abstractmethod
    async def upsert_node(
        self, label: str, properties: dict[str, Any], key: str = "name"
    ) -> None:
        """Create or update a node."""
        ...

    @abstractmethod
    async def upsert_relationship(
        self,
        from_label: str,
        from_key: str,
        to_label: str,
        to_key: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Create or update a relationship between nodes."""
        ...

    @abstractmethod
    async def get_blast_radius(
        self, node_name: str, max_depth: int = 3
    ) -> list[dict[str, Any]]:
        """Get all nodes affected if the given node goes down."""
        ...


class InMemoryGraphBackend(GraphBackend):
    """In-memory graph backend for development and testing.

    Stores nodes and relationships in dictionaries. Useful when
    FalkorDBLite is not available (e.g., CI, unit tests).
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}  # key: "Label:name"
        self._relationships: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        logger.info("graph.inmemory.initialized")

    async def close(self) -> None:
        self._nodes.clear()
        self._relationships.clear()

    def _node_key(self, label: str, name: str) -> str:
        return f"{label}:{name}"

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        # Simplified query executor for basic patterns
        # Real implementation uses FalkorDB Cypher engine
        logger.debug("graph.inmemory.query", cypher=cypher, params=params)
        return []

    async def upsert_node(
        self, label: str, properties: dict[str, Any], key: str = "name"
    ) -> None:
        name = properties.get(key, "unknown")
        node_key = self._node_key(label, name)
        if node_key in self._nodes:
            self._nodes[node_key].update(properties)
        else:
            self._nodes[node_key] = {"_label": label, **properties}
        logger.debug("graph.inmemory.upsert_node", label=label, name=name)

    async def upsert_relationship(
        self,
        from_label: str,
        from_key: str,
        to_label: str,
        to_key: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        rel = {
            "from": self._node_key(from_label, from_key),
            "to": self._node_key(to_label, to_key),
            "type": rel_type,
            "properties": properties or {},
        }
        # Update existing or add new
        for i, existing in enumerate(self._relationships):
            if (
                existing["from"] == rel["from"]
                and existing["to"] == rel["to"]
                and existing["type"] == rel["type"]
            ):
                self._relationships[i] = rel
                return
        self._relationships.append(rel)
        logger.debug("graph.inmemory.upsert_rel", rel_type=rel_type, from_key=from_key, to_key=to_key)

    async def get_blast_radius(
        self, node_name: str, max_depth: int = 3
    ) -> list[dict[str, Any]]:
        """BFS traversal to find all nodes that depend on the given node."""
        affected: list[dict[str, Any]] = []
        visited: set[str] = set()
        # Find all node keys matching the name
        start_keys = {k for k in self._nodes if k.endswith(f":{node_name}")}

        queue: list[tuple[str, int]] = [(k, 0) for k in start_keys]
        while queue:
            current, depth = queue.pop(0)
            if current in visited or depth > max_depth:
                continue
            visited.add(current)

            # Find relationships pointing TO this node (dependents)
            if depth < max_depth:
                for rel in self._relationships:
                    if rel["to"] == current and rel["from"] not in visited:
                        from_node = self._nodes.get(rel["from"], {})
                        affected.append({
                            "name": from_node.get("name", rel["from"]),
                            "type": from_node.get("_label", "unknown"),
                            "depth": depth + 1,
                            "relationship": rel["type"],
                        })
                        queue.append((rel["from"], depth + 1))

        return affected

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return dict(self._nodes)

    @property
    def relationships(self) -> list[dict[str, Any]]:
        return list(self._relationships)


class FalkorDBLiteBackend(GraphBackend):
    """FalkorDBLite embedded graph backend.

    Uses FalkorDBLite subprocess for zero-config embedded graph database.
    Requires Python 3.12+ and falkordblite package.
    """

    def __init__(self, data_dir: str = "/app/data/graph") -> None:
        self._data_dir = data_dir
        self._db: Any = None
        self._graph: Any = None

    async def initialize(self) -> None:
        try:
            from redislite import FalkorDB

            import os
            os.makedirs(self._data_dir, exist_ok=True)
            db_path = os.path.join(self._data_dir, "falkordb.rdb")
            self._db = FalkorDB(db_path)
            self._graph = self._db.select_graph("trifourier")
            logger.info("graph.falkordb.initialized", data_dir=self._data_dir)

            # Create indexes for common lookups
            for label in ["Service", "Database", "Queue", "Cache", "Incident"]:
                try:
                    self._graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.name)")
                except Exception:
                    pass  # Index may already exist

        except ImportError:
            logger.warning("graph.falkordb.not_installed", msg="Falling back to in-memory graph")
            raise
        except Exception as e:
            logger.error("graph.falkordb.init_failed", error=str(e))
            raise

    async def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
            self._graph = None

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._graph is None:
            raise RuntimeError("Graph not initialized")
        result = self._graph.query(cypher, params or {})
        # FalkorDB headers are [type_code, name] pairs
        headers = [h[1] if isinstance(h, list) else h for h in result.header]
        return [dict(zip(headers, row)) for row in result.result_set]

    async def upsert_node(
        self, label: str, properties: dict[str, Any], key: str = "name"
    ) -> None:
        if self._graph is None:
            raise RuntimeError("Graph not initialized")
        name = properties.get(key, "unknown")
        props_str = ", ".join(f"n.{k} = ${k}" for k in properties)
        cypher = f"MERGE (n:{label} {{{key}: ${key}}}) SET {props_str}"
        self._graph.query(cypher, properties)

    async def upsert_relationship(
        self,
        from_label: str,
        from_key: str,
        to_label: str,
        to_key: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if self._graph is None:
            raise RuntimeError("Graph not initialized")
        props = properties or {}
        props_str = ""
        if props:
            set_parts = ", ".join(f"r.{k} = ${k}" for k in props)
            props_str = f" SET {set_parts}"
        cypher = (
            f"MATCH (a:{from_label} {{name: $from_name}}), "
            f"(b:{to_label} {{name: $to_name}}) "
            f"MERGE (a)-[r:{rel_type}]->(b){props_str}"
        )
        params = {"from_name": from_key, "to_name": to_key, **props}
        self._graph.query(cypher, params)

    async def get_blast_radius(
        self, node_name: str, max_depth: int = 3
    ) -> list[dict[str, Any]]:
        if self._graph is None:
            raise RuntimeError("Graph not initialized")
        cypher = (
            f"MATCH path = (s {{name: $node_name}})<-[*1..{max_depth}]-(affected) "
            "RETURN affected.name AS name, labels(affected)[0] AS type, "
            "length(path) AS depth ORDER BY depth"
        )
        result = self._graph.query(cypher, {"node_name": node_name})
        headers = [h[1] if isinstance(h, list) else h for h in result.header]
        return [dict(zip(headers, row)) for row in result.result_set]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_backend: GraphBackend | None = None


async def get_graph_backend() -> GraphBackend:
    """Get or create the graph backend singleton."""
    global _backend
    if _backend is not None:
        return _backend

    from trifourier.config.settings import get_settings

    settings = get_settings()

    if settings.graph_backend == "falkordb_lite":
        try:
            backend = FalkorDBLiteBackend(data_dir=settings.graph_data_dir)
            await backend.initialize()
            _backend = backend
            return _backend
        except (ImportError, Exception):
            logger.warning("graph.fallback_to_inmemory")

    backend = InMemoryGraphBackend()
    await backend.initialize()
    _backend = backend
    return _backend
