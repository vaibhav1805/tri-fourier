"""
Unit tests for the FastAPI health check endpoint.

Tests the /health endpoint defined in src/api/main.py.
This is the first runnable test -- validates the API skeleton works.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app


@pytest.mark.unit
class TestHealthEndpoint:
    """Test the /health endpoint."""

    @pytest.fixture
    def client(self):
        """Create an async test client for the FastAPI app."""
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_health_returns_200(self, client):
        """GET /health should return 200 OK."""
        response = await client.get("/health")
        assert response.status_code == 200

    async def test_health_returns_ok_status(self, client):
        """GET /health should return {"status": "ok"}."""
        response = await client.get("/health")
        data = response.json()
        assert data == {"status": "ok"}

    async def test_health_content_type_is_json(self, client):
        """GET /health should return application/json."""
        response = await client.get("/health")
        assert "application/json" in response.headers["content-type"]


@pytest.mark.unit
class TestAppMetadata:
    """Test the FastAPI application configuration."""

    def test_app_title(self):
        """App title should be 'Trifourier'."""
        assert app.title == "Trifourier"

    def test_app_version(self):
        """App version should be set."""
        assert app.version == "0.1.0"

    def test_app_has_health_route(self):
        """App should have a /health route registered."""
        routes = [r.path for r in app.routes]
        assert "/health" in routes
