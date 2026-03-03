"""AutoTriage API server — entry point for FastAPI.

This is the Docker CMD entry point. It re-exports the real app from
triagebot.api.server so the Dockerfile CMD works without changes.
"""

from triagebot.api.server import app  # noqa: F401
