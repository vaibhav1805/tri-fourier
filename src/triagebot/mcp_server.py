"""MCP Server for TriageBot knowledge graph queries.

Exposes two tools via Model Context Protocol (MCP):
- /query_graph: Run Cypher queries against the knowledge graph
- /blast_radius: Calculate blast radius for a given service

Integrates with FastAPI for startup, but runs as a standalone
MCP server that can be consumed by any MCP-compatible client.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Tool implementations (called by MCP handlers and orchestrator)
# ---------------------------------------------------------------------------


async def query_graph(
    query_type: str,
    service_name: str | None = None,
    cypher: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query the knowledge graph for service topology and dependencies.

    Args:
        query_type: One of 'service_dependencies', 'service_health',
                    'recent_incidents', 'all_services', or 'custom'.
        service_name: Service name for predefined queries.
        cypher: Custom Cypher query (only when query_type='custom').
        params: Parameters for the Cypher query.

    Returns:
        Dict with 'results' list and metadata.
    """
    from triagebot.graph.backend import get_graph_backend
    from triagebot.graph.schema import CYPHER_QUERIES

    try:
        graph = await get_graph_backend()

        if query_type == "custom" and cypher:
            rows = await graph.query(cypher, params or {})
        elif query_type in CYPHER_QUERIES and service_name:
            query_params: dict[str, Any] = {"service_name": service_name}
            if params:
                query_params.update(params)
            rows = await graph.query(CYPHER_QUERIES[query_type], query_params)
        else:
            return {
                "error": f"Unknown query_type '{query_type}' or missing service_name",
                "results": [],
                "available_types": list(CYPHER_QUERIES.keys()),
            }

        # Serialize results (handle non-serializable graph objects)
        serialized = []
        for row in rows:
            clean_row = {}
            for k, v in row.items():
                if hasattr(v, "properties"):
                    clean_row[k] = dict(v.properties)
                else:
                    clean_row[k] = v
            serialized.append(clean_row)

        return {
            "query_type": query_type,
            "service_name": service_name,
            "results": serialized,
            "count": len(serialized),
        }

    except Exception as e:
        logger.error("mcp.query_graph.failed", error=str(e), query_type=query_type)
        return {
            "error": str(e),
            "query_type": query_type,
            "results": [],
        }


async def blast_radius(
    service_name: str,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Calculate the blast radius for a given service.

    Finds all services that would be affected if the given service
    goes down, traversing the dependency graph up to max_depth.

    Args:
        service_name: Name of the service to analyze.
        max_depth: Maximum traversal depth (1-5).

    Returns:
        Dict with affected services, total count, and risk assessment.
    """
    from triagebot.graph.backend import get_graph_backend
    from triagebot.graph.schema import CYPHER_QUERIES

    max_depth = max(1, min(5, max_depth))

    try:
        graph = await get_graph_backend()

        # Get blast radius (nodes that depend on this service)
        affected = await graph.get_blast_radius(service_name, max_depth=max_depth)

        # Get direct dependencies (what this service depends on)
        deps = await graph.query(
            CYPHER_QUERIES["service_dependencies"],
            {"service_name": service_name},
        )

        # Get service health
        health_rows = await graph.query(
            CYPHER_QUERIES["service_health"],
            {"service_name": service_name},
        )
        service_health = health_rows[0] if health_rows else {"health": "unknown"}

        # Risk assessment
        affected_count = len(affected)
        if affected_count >= 10:
            risk_level = "critical"
        elif affected_count >= 5:
            risk_level = "high"
        elif affected_count >= 2:
            risk_level = "medium"
        else:
            risk_level = "low"

        # Group affected by depth
        by_depth: dict[int, list[str]] = {}
        for a in affected:
            depth = a.get("depth", 1)
            by_depth.setdefault(depth, []).append(a.get("name", "unknown"))

        return {
            "service_name": service_name,
            "service_health": service_health.get("health", "unknown"),
            "affected_services": [
                {
                    "name": a.get("name", "unknown"),
                    "type": a.get("type", "unknown"),
                    "depth": a.get("depth", 1),
                }
                for a in affected
            ],
            "affected_count": affected_count,
            "risk_level": risk_level,
            "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
            "direct_dependencies": [
                {
                    "name": d.get("name", "unknown"),
                    "type": d.get("type", "unknown"),
                    "relationship": d.get("relationship", "UNKNOWN"),
                }
                for d in deps
            ],
            "max_depth": max_depth,
        }

    except Exception as e:
        logger.error("mcp.blast_radius.failed", error=str(e), service=service_name)
        return {
            "service_name": service_name,
            "affected_services": [],
            "affected_count": 0,
            "risk_level": "unknown",
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# MCP Server setup (FastMCP)
# ---------------------------------------------------------------------------


def create_mcp_server() -> Any:
    """Create and configure the MCP server with graph tools.

    Returns the MCP server instance, or None if fastmcp is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("triagebot-graph")

        @mcp.tool()
        async def mcp_query_graph(
            query_type: str,
            service_name: str = "",
            cypher: str = "",
        ) -> str:
            """Query the TriageBot knowledge graph for service topology.

            Args:
                query_type: Query type - one of 'service_dependencies', 'service_health',
                           'recent_incidents', 'all_services', or 'custom'.
                service_name: Service name (required for predefined queries).
                cypher: Custom Cypher query (only when query_type='custom').
            """
            result = await query_graph(
                query_type=query_type,
                service_name=service_name or None,
                cypher=cypher or None,
            )
            return json.dumps(result, indent=2, default=str)

        @mcp.tool()
        async def mcp_blast_radius(
            service_name: str,
            max_depth: int = 3,
        ) -> str:
            """Calculate blast radius for a service in the dependency graph.

            Shows all services that would be impacted if the given service
            goes down, with risk assessment.

            Args:
                service_name: Name of the service to analyze.
                max_depth: Maximum traversal depth (1-5, default 3).
            """
            result = await blast_radius(
                service_name=service_name,
                max_depth=max_depth,
            )
            return json.dumps(result, indent=2, default=str)

        logger.info("mcp.server_created", tools=["query_graph", "blast_radius"])
        return mcp

    except ImportError:
        logger.warning("mcp.fastmcp_not_installed", msg="MCP server disabled")
        return None


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------


_mcp_server: Any = None


def get_mcp_server() -> Any:
    """Get or create the MCP server singleton."""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = create_mcp_server()
    return _mcp_server


def register_mcp_routes(app: Any) -> None:
    """Register MCP-equivalent REST routes on the FastAPI app.

    This provides direct HTTP access to graph tools for clients that
    don't support MCP protocol. The actual MCP server runs separately.
    """
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        return

    @app.post("/api/graph/query")
    async def api_query_graph(request: dict[str, Any]) -> dict[str, Any]:
        """REST endpoint for graph queries (mirrors MCP query_graph tool)."""
        return await query_graph(
            query_type=request.get("query_type", ""),
            service_name=request.get("service_name"),
            cypher=request.get("cypher"),
            params=request.get("params"),
        )

    @app.post("/api/graph/blast-radius")
    async def api_blast_radius(request: dict[str, Any]) -> dict[str, Any]:
        """REST endpoint for blast radius (mirrors MCP blast_radius tool)."""
        return await blast_radius(
            service_name=request.get("service_name", ""),
            max_depth=request.get("max_depth", 3),
        )

    logger.info("mcp.rest_routes_registered", routes=["/api/graph/query", "/api/graph/blast-radius"])
