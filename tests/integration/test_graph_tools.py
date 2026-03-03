"""
Integration tests: Knowledge graph -> tool integration.

Tests that graph query results correctly feed into agent tools
and that tools can write back to the graph.
"""

import pytest


@pytest.mark.integration
class TestGraphQueryTools:
    """Test graph query functions with actual graph operations."""

    @pytest.mark.skip(reason="Awaiting graph backend + tools implementation")
    def test_query_dependency_graph_returns_results(self, sample_dependency_graph):
        """query_dependency_graph tool should return formatted results."""
        pass

    @pytest.mark.skip(reason="Awaiting graph backend + tools implementation")
    def test_get_blast_radius_traverses_multi_hop(self, sample_dependency_graph):
        """get_blast_radius should follow multi-hop dependency chains."""
        pass

    @pytest.mark.skip(reason="Awaiting graph backend + tools implementation")
    def test_get_service_context_includes_relationships(self, sample_dependency_graph):
        """get_service_context should include dependency relationships."""
        pass


@pytest.mark.integration
class TestGraphIngestion:
    """Test graph data ingestion from external sources."""

    @pytest.mark.skip(reason="Awaiting graph ingestion pipeline implementation")
    def test_k8s_service_discovery_populates_graph(self, mock_kubernetes):
        """K8s API data should create Service nodes in the graph."""
        pass

    @pytest.mark.skip(reason="Awaiting graph ingestion pipeline implementation")
    def test_upsert_is_idempotent(self):
        """Repeated ingestion should update, not duplicate nodes."""
        pass
