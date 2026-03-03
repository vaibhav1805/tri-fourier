"""Trifourier API server — entry point for FastAPI.

This is the Docker CMD entry point. It re-exports the real app from
trifourier.api.server so the Dockerfile CMD works without changes.
"""

from trifourier.api.server import app  # noqa: F401
