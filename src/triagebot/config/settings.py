"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """TriageBot configuration."""

    model_config = {"env_prefix": "TRIAGEBOT_", "env_file": ".env"}

    # LLM Configuration
    model_provider: str = "anthropic"
    model_id: str = "us.anthropic.claude-sonnet-4-6-v1:0"

    # Graph Database
    graph_backend: str = "falkordb_lite"
    graph_data_dir: str = "data/graph"
    graph_host: str = "localhost"
    graph_port: int = 6379

    # Prometheus
    prometheus_url: str = "http://localhost:9090"

    # API Server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Slack Integration
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_signing_secret: str = ""

    # Safety Controls
    auto_remediation_threshold: float = 0.9
    approval_threshold: float = 0.7
    max_blast_radius: int = 5
    dry_run: bool = True

    # Agent Limits
    max_agent_turns: int = 30
    agent_timeout_seconds: int = 300

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
